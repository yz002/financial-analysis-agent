"""
POST /v1/ask driven through FastAPI's TestClient with no database: every DB session is a fake
(FakeSession below), and the few queries that need real SQL are patched at their helper
(_lock_account, _find_request, _recent_turns). Covers the Phase D session 5 rules that only
show up at the route: the order of replay/load/charge/run/persist, that no session is open
across run_agent, the 429 row's missing request_id, the in-lock re-check, the IntegrityError
backstop, the conditional completion, the time-budget 504, and that a bound statement owned by
another account never reaches the agent.
"""

import itertools
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

import app.main as app_main
from app.gating import AskGateDecision
from db.models import Account, Conversation, CsvStatement, Turn, UsageEvent
from src.agent import csv_session
from src.agent.agent import AgentTimeBudgetExceeded

ACCOUNT_ID = uuid.uuid4()
_ids = itertools.count(1)


class _Result:
    def __init__(self, rowcount=1, scalar=None):
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeSession:
    """Just enough of a SQLAlchemy Session for the /v1/ask code paths, tracking whether it's
    open and whether it holds an uncommitted transaction."""

    def __init__(self, world):
        self.world = world
        self.added = []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.in_transaction = False
        self.closed = False
        world.sessions.append(self)

    def add(self, obj):
        self.in_transaction = True
        self.added.append(obj)

    def flush(self):
        self.in_transaction = True
        for obj in self.added:
            if isinstance(obj, UsageEvent) and self.world.flush_error is not None:
                raise self.world.flush_error
            if getattr(obj, "id", None) is None:
                obj.id = next(_ids) if isinstance(obj, UsageEvent) else uuid.uuid4()

    def commit(self):
        self.in_transaction = False
        self.commits += 1

    def rollback(self):
        self.in_transaction = False
        self.rollbacks += 1

    def close(self):
        self.in_transaction = False
        self.closed = True

    def execute(self, statement):
        self.in_transaction = True
        self.executed.append(statement)
        return _Result(rowcount=self.world.update_rowcount)

    def get(self, model, key):
        self.in_transaction = True
        return self.world.rows.get((model, key))


class World:
    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.rows = {}
        self.flush_error = None
        self.update_rowcount = 1
        self.find_request_results = []  # returned by successive _find_request calls
        self.locks = []

    def open_sessions(self):
        return [s for s in self.sessions if not s.closed]


def _allowed_gate(session, account, now):
    return AskGateDecision(
        tier="free", allowed=True, questions_used=0, cap=5, resets_at=now,
        reject_outcome=None, reject_error=None, prompt_byo_key=False, prompt_upgrade=False,
    )


def _rejected_gate(session, account, now):
    return AskGateDecision(
        tier="free", allowed=False, questions_used=5, cap=5, resets_at=now + timedelta(hours=1),
        reject_outcome="rejected_daily_cap", reject_error="daily_cap_reached",
        prompt_byo_key=False, prompt_upgrade=True,
    )


def _result(question, figure_check=None):
    return {
        "question": question, "final_answer": "Answer.", "hit_iteration_cap": False,
        "iterations_used": 1, "stop_reason": "end_turn",
        "figure_check": figure_check or {"figures": []}, "tool_calls": [],
    }


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(app_main, "get_session", lambda: FakeSession(w))
    monkeypatch.setattr(app_main, "_lock_account", lambda session, account_id: w.locks.append(session))
    monkeypatch.setattr(
        app_main,
        "_find_request",
        lambda session, account_id, request_id: (
            w.find_request_results.pop(0) if w.find_request_results else None
        ),
    )
    monkeypatch.setattr(app_main, "evaluate_ask_gate", _allowed_gate)
    app_main.app.dependency_overrides[app_main.get_current_account] = lambda: Account(
        id=ACCOUNT_ID, byo_key_id=None
    )
    yield w
    app_main.app.dependency_overrides.clear()


client = TestClient(app_main.app)


def _ask(**body):
    return client.post("/v1/ask", json={"question": "What was revenue?", **body})


def _compile(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --- no session open across run_agent ------------------------------------------------------


def test_no_session_is_open_or_in_a_transaction_while_run_agent_runs(world, monkeypatch):
    seen = {}

    def run_agent(question, **kwargs):
        seen["open"] = [s for s in world.open_sessions()]
        seen["in_transaction"] = [s for s in world.sessions if s.in_transaction]
        seen["lock_session_committed"] = all(s.commits >= 1 and s.closed for s in world.locks)
        return _result(question)

    monkeypatch.setattr(app_main, "run_agent", run_agent)
    resp = _ask(request_id=str(uuid.uuid4()))

    assert resp.status_code == 200, resp.text
    assert seen["open"] == []
    assert seen["in_transaction"] == []
    assert seen["lock_session_committed"] is True  # the account lock was released before the run
    assert world.open_sessions() == []  # and nothing is left open afterwards


def test_the_in_progress_row_carries_the_request_id_and_fingerprint(world, monkeypatch):
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: _result(question))
    request_id = uuid.uuid4()
    _ask(request_id=str(request_id))

    [row] = [o for s in world.sessions for o in s.added if isinstance(o, UsageEvent)]
    assert row.outcome == "in_progress"
    assert row.request_id == request_id
    assert row.request_fingerprint == app_main.request_fingerprint("What was revenue?", None, None)


# --- 429 rows and the in-lock re-check -----------------------------------------------------


def test_a_429_row_never_carries_the_request_id_and_the_id_works_again_later(world, monkeypatch):
    ran = []
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: ran.append(question) or _result(question))
    request_id = str(uuid.uuid4())

    monkeypatch.setattr(app_main, "evaluate_ask_gate", _rejected_gate)
    resp = _ask(request_id=request_id)
    assert resp.status_code == 429
    [rejected] = [o for s in world.sessions for o in s.added if isinstance(o, UsageEvent)]
    assert rejected.outcome == "rejected_daily_cap"
    assert rejected.request_id is None
    assert rejected.request_fingerprint is None

    # After the cap resets, the same request_id runs as a fresh question (nothing stored under it).
    monkeypatch.setattr(app_main, "evaluate_ask_gate", _allowed_gate)
    resp = _ask(request_id=request_id)
    assert resp.status_code == 200, resp.text
    assert ran == ["What was revenue?"]


def test_a_request_that_appears_while_waiting_for_the_lock_gets_the_replay_answer(world, monkeypatch):
    gate_calls, ran = [], []
    monkeypatch.setattr(app_main, "evaluate_ask_gate", lambda *a: gate_calls.append(a) or _allowed_gate(*a))
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: ran.append(question))
    fingerprint = app_main.request_fingerprint("What was revenue?", None, None)
    running = UsageEvent(
        id=99, account_id=ACCOUNT_ID, occurred_at=datetime.now(timezone.utc), outcome="in_progress",
        request_id=uuid.uuid4(), request_fingerprint=fingerprint,
    )
    # First lookup (replay, before loading) finds nothing; the re-check inside the lock finds it.
    world.find_request_results = [None, running]

    resp = _ask(request_id=str(running.request_id))

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "answer_in_progress"}
    assert gate_calls == []  # never a 429
    assert ran == []
    [lock_session] = world.locks
    assert lock_session.commits >= 1 and lock_session.closed  # lock released
    assert not any(isinstance(o, UsageEvent) for s in world.sessions for o in s.added)


def test_unique_index_violation_on_insert_is_a_409_not_a_500(world, monkeypatch):
    ran = []
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: ran.append(question))
    world.flush_error = IntegrityError("INSERT", {}, Exception("duplicate key"))

    resp = _ask(request_id=str(uuid.uuid4()))

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "answer_in_progress"}
    assert ran == []
    [lock_session] = world.locks
    assert lock_session.rollbacks == 1 and lock_session.closed


# --- replay states that only show up at the route ------------------------------------------


def test_unexpected_replay_state_logs_metadata_only_and_answers_failed(world, monkeypatch, caplog):
    ran = []
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: ran.append(question))
    question = "SECRET-QUESTION-TEXT?"
    odd = UsageEvent(
        id=7, account_id=ACCOUNT_ID, occurred_at=datetime.now(timezone.utc), outcome="answered",
        turn_id=None, request_id=uuid.uuid4(),
        request_fingerprint=app_main.request_fingerprint(question, None, None),
    )
    world.find_request_results = [odd]

    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = client.post("/v1/ask", json={"question": question, "request_id": str(odd.request_id)})

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "answer_failed"}
    assert ran == []
    [record] = [r for r in caplog.records if r.name == "app.main"]
    assert "usage_event_id=7" in record.getMessage()
    assert "SECRET-QUESTION-TEXT" not in record.getMessage()


def test_finished_request_replays_exactly_what_was_stored(world, monkeypatch):
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: pytest.fail("no rerun"))
    conversation_id, turn_id = uuid.uuid4(), uuid.uuid4()
    stored_citations = [{"figure_index": 0, "status": "traced", "matches": []}]
    world.rows[(Turn, turn_id)] = Turn(
        id=turn_id, conversation_id=conversation_id, question="What was revenue?",
        final_answer="Stored answer.", hit_iteration_cap=False, figure_check={"figures": []},
        tool_calls=[{"tool_name": "get_csv_statement", "is_error": False}], citations=stored_citations,
    )
    world.rows[(Conversation, conversation_id)] = Conversation(id=conversation_id, account_id=ACCOUNT_ID)
    done = UsageEvent(
        id=8, account_id=ACCOUNT_ID, occurred_at=datetime.now(timezone.utc), outcome="answered",
        turn_id=turn_id, request_id=uuid.uuid4(),
        request_fingerprint=app_main.request_fingerprint("What was revenue?", None, None),
    )
    world.find_request_results = [done]

    resp = _ask(request_id=str(done.request_id))

    assert resp.status_code == 200
    assert resp.json() == {
        "conversation_id": str(conversation_id),
        "turn_id": str(turn_id),
        "final_answer": "Stored answer.",
        "hit_iteration_cap": False,
        "figure_check": {"figures": []},
        "citations": stored_citations,
        "tool_calls_summary": [{"tool_name": "get_csv_statement", "is_error": False}],
    }
    assert world.locks == []  # a replay never takes the lock


# --- time budget and late completion --------------------------------------------------------


def test_time_budget_is_a_structured_504_and_the_row_moves_to_error(world, monkeypatch):
    def run_agent(question, **kwargs):
        raise AgentTimeBudgetExceeded(2701.0)

    monkeypatch.setattr(app_main, "run_agent", run_agent)
    resp = _ask(request_id=str(uuid.uuid4()))

    assert resp.status_code == 504
    assert resp.json() == {"detail": {"error": "answer_time_budget_exceeded"}}
    failed = [stmt for s in world.sessions for stmt in s.executed if "outcome='error'" in _compile(stmt)]
    assert len(failed) == 1
    assert "usage_events.outcome = 'in_progress'" in _compile(failed[0])  # conditional transition


def test_a_run_that_finishes_after_being_marked_lost_rolls_back_and_warns(world, monkeypatch, caplog):
    question = "SECRET-QUESTION-TEXT?"
    monkeypatch.setattr(app_main, "run_agent", lambda q, **kwargs: _result(q))
    world.update_rowcount = 0  # the conditional IN_PROGRESS -> DONE finds the row already failed

    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = client.post("/v1/ask", json={"question": question, "request_id": str(uuid.uuid4())})

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "answer_failed"}
    persist = next(s for s in world.sessions if any(isinstance(o, Turn) for o in s.added))
    assert persist.rollbacks == 1
    assert persist.commits == 0  # no orphan Turn or Conversation
    [record] = [r for r in caplog.records if r.name == "app.main"]
    assert "usage_event_id=" in record.getMessage() and "elapsed_ms=" in record.getMessage()
    assert "SECRET-QUESTION-TEXT" not in record.getMessage()


def test_transitions_are_conditional_on_in_progress():
    failed = _compile(app_main._failed_transition(5))
    done = _compile(app_main._completion_transition(5, uuid.uuid4(), "answered"))
    for sql in (failed, done):
        assert "usage_events.id = 5" in sql
        assert "usage_events.outcome = 'in_progress'" in sql


# --- binding -------------------------------------------------------------------------------


def test_bound_statement_owned_by_another_account_never_reaches_the_agent(world, monkeypatch):
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: pytest.fail("must not run"))
    monkeypatch.setattr(app_main, "_recent_turns", lambda session, conversation_id: [])
    conversation_id, statement_id = uuid.uuid4(), uuid.uuid4()
    world.rows[(Conversation, conversation_id)] = Conversation(
        id=conversation_id, account_id=ACCOUNT_ID,
        csv_context_id=statement_id, bound_csv_context_id=statement_id,
    )
    world.rows[(CsvStatement, statement_id)] = CsvStatement(
        id=statement_id, account_id=uuid.uuid4(),  # someone else's
        status="confirmed", statement_data=[{"period_end": "2024-03-31"}],
        statement_attrs={"entity_name": "Not Yours", "csv_source": {"scale": "ones", "currency": None}},
    )
    seen_csv = []
    monkeypatch.setattr(
        csv_session, "set_active_csv_with_token", lambda df: seen_csv.append(df) or pytest.fail("no data")
    )

    resp = _ask(conversation_id=str(conversation_id))

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_needs_reconfirm"}
    assert seen_csv == []
    assert world.locks == []  # refused before the charge


def test_csv_turn_fallback_prefilters_every_turn_with_jsonb_containment():
    compiled = app_main._csv_tool_turns(uuid.uuid4()).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("SELECT turns.tool_calls")
    assert sql.count("turns.tool_calls @>") == 2
    assert "LIMIT" not in sql  # every turn, not just the replayed window
    contained = [v for v in compiled.params.values() if isinstance(v, list)]
    assert [{"tool_name": "get_csv_statement"}] in contained
    assert [{"tool_name": "get_csv_ratios"}] in contained


class _ScalarsResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return iter(self._values)


def _unbound_conversation_world(world, monkeypatch, csv_turn_tool_calls):
    """An existing conversation with no recorded binding whose CSV-tool turns are
    `csv_turn_tool_calls` (what _csv_tool_turns would return)."""
    monkeypatch.setattr(app_main, "_recent_turns", lambda session, conversation_id: [])
    conversation_id = uuid.uuid4()
    world.rows[(Conversation, conversation_id)] = Conversation(
        id=conversation_id, account_id=ACCOUNT_ID, csv_context_id=None, bound_csv_context_id=None,
    )
    original_execute = FakeSession.execute

    def execute(self, statement):
        if "SELECT turns.tool_calls" in str(statement):
            return _ScalarsResult(csv_turn_tool_calls)
        return original_execute(self, statement)

    monkeypatch.setattr(FakeSession, "execute", execute)
    return conversation_id


def test_unbound_conversation_whose_csv_lookup_found_nothing_keeps_working(world, monkeypatch):
    no_statement = {
        "iteration": 1, "tool_name": "get_csv_statement", "tool_input": {}, "is_error": False,
        "tool_result": '{"business_name": null, "error_type": "data_unavailable", "error": "No CSV"}',
    }
    conversation_id = _unbound_conversation_world(world, monkeypatch, [[no_statement]])
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: _result(question))

    resp = _ask(conversation_id=str(conversation_id))

    assert resp.status_code == 200, resp.text


def test_unbound_conversation_that_got_statement_data_is_needs_reconfirm(world, monkeypatch):
    with_data = {
        "iteration": 1, "tool_name": "get_csv_statement", "tool_input": {}, "is_error": False,
        "tool_result": '{"business_name": "Old Co", "cadence": "quarterly", "periods": []}',
    }
    conversation_id = _unbound_conversation_world(world, monkeypatch, [[with_data]])
    monkeypatch.setattr(app_main, "run_agent", lambda question, **kwargs: pytest.fail("must not run"))

    resp = _ask(conversation_id=str(conversation_id))

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_needs_reconfirm"}
    assert world.locks == []  # refused before the charge


# --- counting and logging ------------------------------------------------------------------


def test_in_progress_counts_toward_the_question_caps():
    from app import gating

    assert "in_progress" in gating._COUNTED_OUTCOMES
    captured = []

    class _Session:
        def execute(self, statement):
            captured.append(statement)
            return type("R", (), {"scalar_one": lambda self: 0})()

    gating._count_usage_events(_Session(), ACCOUNT_ID, datetime.now(timezone.utc), None)
    compiled = captured[0].compile(dialect=postgresql.dialect())
    counted = [v for v in compiled.params.values() if isinstance(v, (list, tuple))]
    assert any("in_progress" in values for values in counted)


def test_agent_logger_has_its_own_handler_and_does_not_propagate():
    agent_logger = logging.getLogger(app_main.AGENT_LOGGER_NAME)
    assert agent_logger.propagate is False
    assert agent_logger.level == logging.INFO
    # pytest attaches its own capture handlers too; app.main adds exactly one stream handler.
    assert len([h for h in agent_logger.handlers if type(h) is logging.StreamHandler]) == 1



# --- the statement's names reach the figure check --------------------------------------------


def test_the_bound_statements_names_are_passed_as_excluded_phrases(world, monkeypatch):
    statement_id = uuid.uuid4()
    world.rows[(CsvStatement, statement_id)] = CsvStatement(
        id=statement_id, account_id=ACCOUNT_ID, status="confirmed",
        statement_data=[{"period_end": "2024-03-31"}],
        statement_attrs={"entity_name": "Studio 54", "csv_source": {"scale": "thousands", "currency": None}},
        raw_columns={"columns": [], "data_rows": [], "source": {"sheet_name": "P&L (000s)", "file_name": "Spike File"}},
    )
    monkeypatch.setattr(app_main, "statement_from_records", lambda data, attrs: object())
    seen = {}

    def run_agent(question, **kwargs):
        seen.update(kwargs)
        return _result(question)

    monkeypatch.setattr(app_main, "run_agent", run_agent)
    resp = _ask(csv_context_id=str(statement_id))

    assert resp.status_code == 200, resp.text
    assert seen["excluded_phrases"] == ["P&L (000s)", "Spike File", "Studio 54"]


def test_no_statement_means_no_excluded_phrases(world, monkeypatch):
    seen = {}

    def run_agent(question, **kwargs):
        seen.update(kwargs)
        return _result(question)

    monkeypatch.setattr(app_main, "run_agent", run_agent)
    assert _ask().status_code == 200
    assert seen["excluded_phrases"] == []

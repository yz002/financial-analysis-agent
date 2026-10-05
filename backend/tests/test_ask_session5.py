"""
DB-backed /v1/ask tests for Phase D session 5 (EXTENSION_INTEGRATION.md SS6, amended session 5):
statement binding, legacy statements, request_id replay, and in-progress rows counting toward
the cap.

Like every file in backend/tests/, these hit the database in backend/.env -- today that's the
shared PRODUCTION database (backend/DEPLOYMENT.md, "Known risk / backlog"). They create their
own accounts through the auth_session fixture and delete them (cascading to every row written
here) on teardown, but a crashed run can leave rows behind. Don't run them without the owner's
go-ahead. The same rules are unit-tested without a database in backend/tests_unit/.

app.main.run_agent is always monkeypatched -- no Anthropic calls.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.gating import FREE_DAILY_CAP
from app.main import app
from db.base import get_session

client = TestClient(app)


def _fake_result(question: str, tool_calls: list[dict] | None = None) -> dict:
    return {
        "question": question,
        "final_answer": "Answer.",
        "hit_iteration_cap": False,
        "iterations_used": 1,
        "stop_reason": "end_turn",
        "figure_check": {"figures": []},
        "tool_calls": tool_calls or [],
    }


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []

    def run_agent(question, prior_messages=None, prior_tool_calls=None, client=None):
        calls.append(question)
        return _fake_result(question)

    monkeypatch.setattr(app_main, "run_agent", run_agent)
    return calls


def _confirmed_statement(headers: dict, name: str) -> str:
    resp = client.post(
        "/v1/csv/parse",
        json={
            "rows": [["Quarter Ending", "Total Revenue"], ["2024-03-31", "100"], ["2024-06-30", "110"]],
            "filename": f"{name}.csv",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    csv_context_id = resp.json()["csv_context_id"]
    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={
            "mapping": {"Quarter Ending": "period_end", "Total Revenue": "revenue"},
            "entity_name": name,
            "scale": "ones",
        },
        headers=headers,
    )
    assert resp.json()["confirmed"] is True, resp.text
    return csv_context_id


def _ask(headers, question="Q?", **fields):
    return client.post("/v1/ask", json={"question": question, **fields}, headers=headers)


def _sql(statement: str, **params):
    session = get_session()
    try:
        result = session.execute(text(statement), params)
        rows = result.fetchall() if result.returns_rows else None
        session.commit()
        return rows
    finally:
        session.close()


def _usage_outcomes(account_id: str) -> list[str]:
    return [r[0] for r in _sql("SELECT outcome FROM usage_events WHERE account_id = :id", id=account_id)]


# --- binding -------------------------------------------------------------------------------


def test_new_conversation_is_bound_to_its_statement(auth_session, fake_agent):
    account_id, headers = auth_session()
    statement = _confirmed_statement(headers, "Bound Co")

    resp = _ask(headers, csv_context_id=statement)
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]
    [(bound, fk)] = _sql(
        "SELECT bound_csv_context_id, csv_context_id FROM conversations WHERE id = :id", id=conversation_id
    )
    assert str(bound) == str(fk) == statement

    # A later turn may omit the statement: the bound one is used.
    resp = _ask(headers, "Follow-up?", conversation_id=conversation_id)
    assert resp.status_code == 200, resp.text


def test_a_different_statement_on_a_later_turn_is_a_409_and_costs_nothing(auth_session, fake_agent):
    account_id, headers = auth_session()
    first = _confirmed_statement(headers, "First Co")
    second = _confirmed_statement(headers, "Second Co")
    conversation_id = _ask(headers, csv_context_id=first).json()["conversation_id"]
    charged_before = len(_usage_outcomes(account_id))

    resp = _ask(headers, "Switch?", conversation_id=conversation_id, csv_context_id=second)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_mismatch"}
    assert len(_usage_outcomes(account_id)) == charged_before


def test_a_statement_on_an_unbound_conversation_is_a_409(auth_session, fake_agent):
    _, headers = auth_session()
    statement = _confirmed_statement(headers, "Late Co")
    conversation_id = _ask(headers).json()["conversation_id"]  # ticker-only, unbound

    resp = _ask(headers, conversation_id=conversation_id, csv_context_id=statement)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_mismatch"}


def test_bound_statement_deleted_is_needs_reconfirm(auth_session, fake_agent):
    _, headers = auth_session()
    statement = _confirmed_statement(headers, "Gone Co")
    conversation_id = _ask(headers, csv_context_id=statement).json()["conversation_id"]
    _sql("DELETE FROM csv_statements WHERE id = :id", id=statement)  # FK nulls csv_context_id

    resp = _ask(headers, "Still there?", conversation_id=conversation_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_needs_reconfirm"}


def test_pre_0005_conversation_with_a_csv_call_in_turn_one_of_six_is_needs_reconfirm(
    auth_session, monkeypatch
):
    """The EXISTS fallback checks every turn, not just the 3 replayed ones."""
    _, headers = auth_session()
    csv_call = {
        "iteration": 1, "tool_name": "get_csv_statement", "tool_input": {},
        "tool_result": "{}", "is_error": False,
    }
    results = iter([_fake_result("Q1", [csv_call])] + [_fake_result(f"Q{i}") for i in range(2, 7)])
    monkeypatch.setattr(
        app_main, "run_agent", lambda question, prior_messages=None, prior_tool_calls=None: next(results)
    )
    conversation_id = _ask(headers, "Q1").json()["conversation_id"]
    for i in range(2, 7):
        assert _ask(headers, f"Q{i}", conversation_id=conversation_id).status_code == 200
    # Simulate a conversation created before 0005 whose statement is gone.
    _sql(
        "UPDATE conversations SET bound_csv_context_id = NULL, csv_context_id = NULL WHERE id = :id",
        id=conversation_id,
    )

    resp = _ask(headers, "Q7", conversation_id=conversation_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_needs_reconfirm"}


# --- legacy statements ---------------------------------------------------------------------


def test_legacy_statement_without_scale_is_needs_reconfirm_and_costs_nothing(auth_session, fake_agent):
    account_id, headers = auth_session()
    statement = _confirmed_statement(headers, "Legacy Co")
    _sql(
        "UPDATE csv_statements SET statement_attrs = statement_attrs #- '{csv_source,scale}' WHERE id = :id",
        id=statement,
    )

    resp = _ask(headers, csv_context_id=statement)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "statement_needs_reconfirm"}
    assert _usage_outcomes(account_id) == []
    assert fake_agent == []


# --- request_id replay ---------------------------------------------------------------------


def test_finished_request_replays_the_stored_response_without_charging(auth_session, fake_agent):
    account_id, headers = auth_session()
    request_id = str(uuid.uuid4())

    first = _ask(headers, "Replay me", request_id=request_id)
    assert first.status_code == 200, first.text
    second = _ask(headers, "Replay me", request_id=request_id)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert fake_agent == ["Replay me"]  # ran once
    assert _usage_outcomes(account_id) == ["answered"]  # charged once


def test_reused_request_id_with_a_different_question_is_a_422(auth_session, fake_agent):
    _, headers = auth_session()
    request_id = str(uuid.uuid4())
    assert _ask(headers, "Original", request_id=request_id).status_code == 200

    resp = _ask(headers, "Different", request_id=request_id)
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"error": "request_id_reused"}


def _seed_request(account_id: str, request_id: str, question: str, outcome: str, age: timedelta):
    from app.ask_rules import request_fingerprint

    _sql(
        "INSERT INTO usage_events (account_id, occurred_at, outcome, request_id, request_fingerprint) "
        "VALUES (:a, :t, :o, :r, :f)",
        a=account_id, t=datetime.now(timezone.utc) - age, o=outcome, r=request_id,
        f=request_fingerprint(question, None, None),
    )


def test_running_request_is_answer_in_progress(auth_session, fake_agent):
    account_id, headers = auth_session()
    request_id = str(uuid.uuid4())
    _seed_request(account_id, request_id, "Slow", "in_progress", timedelta(minutes=5))

    resp = _ask(headers, "Slow", request_id=request_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "answer_in_progress"}
    assert fake_agent == []


def test_stale_running_request_is_lost_and_moved_to_error(auth_session, fake_agent):
    account_id, headers = auth_session()
    request_id = str(uuid.uuid4())
    _seed_request(account_id, request_id, "Killed", "in_progress", timedelta(minutes=61))

    resp = _ask(headers, "Killed", request_id=request_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "answer_lost"}
    assert _usage_outcomes(account_id) == ["error"]
    # Afterwards it's simply failed.
    assert _ask(headers, "Killed", request_id=request_id).json()["detail"] == {"error": "answer_failed"}


def test_rejected_429_row_never_holds_the_request_id(auth_session, fake_agent):
    account_id, headers = auth_session()
    for _ in range(FREE_DAILY_CAP):
        _sql(
            "INSERT INTO usage_events (account_id, occurred_at, outcome) VALUES (:a, now(), 'answered')",
            a=account_id,
        )
    request_id = str(uuid.uuid4())

    resp = _ask(headers, "Over the cap", request_id=request_id)
    assert resp.status_code == 429
    [(stored_request_id,)] = _sql(
        "SELECT request_id FROM usage_events WHERE account_id = :a AND outcome = 'rejected_daily_cap'",
        a=account_id,
    )
    assert stored_request_id is None


def test_in_progress_rows_count_toward_the_cap(auth_session, fake_agent):
    account_id, headers = auth_session()
    for _ in range(FREE_DAILY_CAP - 2):
        _sql(
            "INSERT INTO usage_events (account_id, occurred_at, outcome) VALUES (:a, now(), 'answered')",
            a=account_id,
        )
    for _ in range(2):
        _sql(
            "INSERT INTO usage_events (account_id, occurred_at, outcome) VALUES (:a, now(), 'in_progress')",
            a=account_id,
        )

    resp = _ask(headers, "One more")
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "daily_cap_reached"

"""
Tests for Phase B session 6: run_agent's new prior_messages parameter and the backend-side
reconstruction of raw prior-turn message blocks that seeds it (design doc SS3, SS6 step 6).

Like test_csv_endpoints.py / test_csv_session_concurrency.py, the /v1/ask integration tests here
hit a real reachable Postgres via DATABASE_URL (backend/.env) -- turns.tool_calls's JSONB column
has no sqlite-compatible substitute. app.main.run_agent is monkeypatched the same way those files
already do, never a real Anthropic API call.

test_build_prior_messages_round_trip_fidelity is a pure unit test of app.history.build_prior_messages
against in-memory Turn instances -- no DB needed, since the function only reads attributes off
whatever it's given.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.history import build_prior_messages
from app.main import app
from db.base import get_session
from db.models import Turn

client = TestClient(app)


def _ask(headers: dict, question: str, conversation_id: str | None = None):
    body = {"question": question}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return client.post("/v1/ask", json=body, headers=headers)


def _fake_result(question: str, final_answer: str, tool_calls: list[dict] | None = None) -> dict:
    return {
        "question": question,  # main.py stores result["question"] verbatim, like the real run_agent
        "final_answer": final_answer,
        "hit_iteration_cap": False,
        "iterations_used": 1,
        "stop_reason": "end_turn",
        "figure_check": {},
        "tool_calls": tool_calls or [],
    }


# --- pure unit test of the reconstruction logic --------------------------------------------


def test_build_prior_messages_round_trip_fidelity():
    turn_id_1 = uuid.uuid4()
    turn_id_2 = uuid.uuid4()

    # Turn 1: a single-call iteration, then a multi-call iteration (mirrors the root suite's
    # test_csv_and_ticker_tool_calls_in_one_round shape -- two tool_use blocks in one round).
    turn_1 = Turn(
        id=turn_id_1,
        question="What was MSFT revenue last quarter?",
        final_answer="MSFT revenue was $X.",
        tool_calls=[
            {
                "iteration": 1,
                "tool_name": "get_financial_statement",
                "tool_input": {"ticker": "MSFT"},
                "tool_result": '{"revenue": "X"}',
                "is_error": False,
            },
            {
                "iteration": 2,
                "tool_name": "get_csv_ratios",
                "tool_input": {"ratio_names": ["net_margin"]},
                "tool_result": '{"tool": "get_csv_ratios"}',
                "is_error": False,
            },
            {
                "iteration": 2,
                "tool_name": "get_ratios",
                "tool_input": {"ticker": "MSFT", "ratio_names": ["net_margin"]},
                "tool_result": '{"tool": "get_ratios"}',
                "is_error": True,
            },
        ],
    )

    # Turn 2: no tool calls at all -- the model answered directly.
    turn_2 = Turn(
        id=turn_id_2,
        question="Thanks, that's all I needed.",
        final_answer="You're welcome!",
        tool_calls=[],
    )

    messages = build_prior_messages([turn_1, turn_2])

    # Turn 1: user, assistant(1 tool_use), user(1 tool_result), assistant(2 tool_use),
    # user(2 tool_result), assistant(final_answer). Turn 2: user, assistant(final_answer).
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    # Must end on an assistant turn so a freshly-appended user question keeps valid alternation.
    assert messages[-1]["role"] == "assistant"

    assert messages[0] == {"role": "user", "content": "What was MSFT revenue last quarter?"}

    round_1_assistant = messages[1]
    round_1_user = messages[2]
    assert len(round_1_assistant["content"]) == 1
    assert len(round_1_user["content"]) == 1
    tool_use_1 = round_1_assistant["content"][0]
    tool_result_1 = round_1_user["content"][0]
    assert tool_use_1["type"] == "tool_use"
    assert tool_use_1["name"] == "get_financial_statement"
    assert tool_use_1["input"] == {"ticker": "MSFT"}
    assert tool_result_1["type"] == "tool_result"
    assert tool_result_1["tool_use_id"] == tool_use_1["id"]
    assert tool_result_1["content"] == '{"revenue": "X"}'
    assert tool_result_1["is_error"] is False

    round_2_assistant = messages[3]
    round_2_user = messages[4]
    assert len(round_2_assistant["content"]) == 2
    assert len(round_2_user["content"]) == 2
    for tool_use, tool_result, expected_name, expected_result, expected_is_error in zip(
        round_2_assistant["content"],
        round_2_user["content"],
        ["get_csv_ratios", "get_ratios"],
        ['{"tool": "get_csv_ratios"}', '{"tool": "get_ratios"}'],
        [False, True],
    ):
        assert tool_use["type"] == "tool_use"
        assert tool_use["name"] == expected_name
        assert tool_result["type"] == "tool_result"
        # Every tool_result's tool_use_id matches a tool_use id from the immediately
        # preceding assistant message, 1:1, in order.
        assert tool_result["tool_use_id"] == tool_use["id"]
        assert tool_result["content"] == expected_result
        assert tool_result["is_error"] is expected_is_error

    # All fabricated tool_use ids across the whole reconstruction are distinct.
    all_ids = [
        block["id"]
        for m in messages
        if m["role"] == "assistant" and isinstance(m["content"], list)
        for block in m["content"]
    ]
    assert len(all_ids) == len(set(all_ids))

    assert messages[5] == {"role": "assistant", "content": "MSFT revenue was $X."}

    # Turn 2: no tool calls -- collapses to just the user question and final_answer.
    assert messages[6] == {"role": "user", "content": "Thanks, that's all I needed."}
    assert messages[7] == {"role": "assistant", "content": "You're welcome!"}


# --- /v1/ask integration: seeding, conversation continuation, 404s -------------------------


def test_ask_with_conversation_id_uses_seeded_history(auth_session, monkeypatch):
    _, headers = auth_session()

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: _fake_result(question, "MSFT revenue was $61.9 billion."),
    )
    resp_1 = _ask(headers, "What was MSFT revenue last quarter?")
    assert resp_1.status_code == 200, resp_1.text
    conversation_id = resp_1.json()["conversation_id"]
    assert conversation_id

    captured = {}

    def fake_run_agent_2(question, prior_messages=None):
        captured["prior_messages"] = prior_messages
        return _fake_result(question, "It grew 12% YoY.")

    monkeypatch.setattr(app_main, "run_agent", fake_run_agent_2)
    resp_2 = _ask(headers, "How did that compare YoY?", conversation_id=conversation_id)
    assert resp_2.status_code == 200, resp_2.text

    prior_messages = captured["prior_messages"]
    assert prior_messages, "expected non-empty prior_messages seeded from turn 1"
    assert prior_messages[0] == {"role": "user", "content": "What was MSFT revenue last quarter?"}
    assert prior_messages[-1] == {"role": "assistant", "content": "MSFT revenue was $61.9 billion."}

    # Same conversation continued, not a new one.
    assert resp_2.json()["conversation_id"] == conversation_id

    session = get_session()
    try:
        last_turn_at = session.execute(
            text("SELECT last_turn_at FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        ).scalar()
        turn_count = session.execute(
            text("SELECT COUNT(*) FROM turns WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar()
        assert turn_count == 2
        assert last_turn_at is not None
    finally:
        session.close()


def test_ask_with_invalid_conversation_id_404s(auth_session, monkeypatch):
    _, headers = auth_session()
    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: pytest.fail(
            "run_agent must not be called for an invalid conversation_id"
        ),
    )

    resp = _ask(headers, "Anything.", conversation_id="not-a-uuid")
    assert resp.status_code == 404, resp.text

    resp = _ask(headers, "Anything.", conversation_id=str(uuid.uuid4()))
    assert resp.status_code == 404, resp.text


def test_ask_with_foreign_conversation_id_404s(auth_session, monkeypatch):
    _, owner_headers = auth_session()
    _, other_headers = auth_session()

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: _fake_result(question, "Answer."),
    )
    resp = _ask(owner_headers, "What was MSFT revenue last quarter?")
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: pytest.fail(
            "run_agent must not be called for a foreign conversation_id"
        ),
    )
    resp = _ask(other_headers, "Follow-up.", conversation_id=conversation_id)
    assert resp.status_code == 404, resp.text

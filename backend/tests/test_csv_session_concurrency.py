"""
Concurrency regression test for the csv_session.py ContextVar reimplementation
(Phase B session 5; see the design doc's SS1 "Replacing csv_session.py's global registry" and
SS6 step 5). Before this session, src/agent/csv_session.py held the active CSV-derived statement
in a single process-global -- under real concurrent /v1/ask requests for two different installs,
one install's tool calls could read the other's data. This file proves the ContextVar-based fix
actually closes that race: two /v1/ask calls, for two different installs each with their own
confirmed CSV, are fired from real threads at the same time and forced (via a threading.Barrier)
to be simultaneously mid-flight inside run_agent -- a sequential pair of calls would not exercise
the race this session exists to fix.

Like test_csv_endpoints.py, this hits a real reachable Postgres via DATABASE_URL (backend/.env).
app.main.run_agent is monkeypatched to a fake that never calls the real Anthropic API; it instead
reports what src.agent.csv_session.get_active_csv() sees from inside its own request's context,
both before and after synchronizing with the other concurrent request via the barrier.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from db.base import get_session
from src.agent import csv_session

client = TestClient(app)


@pytest.fixture
def install_ids():
    """Tracks install_ids created by a test; deletes them (cascading to csv_statements) after."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    session = get_session()
    try:
        session.execute(
            text("DELETE FROM installs WHERE install_id = ANY(:ids)"),
            {"ids": ids},
        )
        session.commit()
    finally:
        session.close()


def _new_install_id(install_ids: list[str]) -> str:
    install_id = str(uuid.uuid4())
    install_ids.append(install_id)
    return install_id


def _rows_for(entity_label: str) -> list[list[str]]:
    return [
        ["Quarter Ending", "Total Revenue"],
        ["2024-01-01", "100000"],
        ["2024-04-01", "110000"],
    ]


def _create_confirmed_csv(install_id: str, entity_name: str) -> str:
    """Runs the real /v1/csv/parse -> /confirm flow (skipping propose-mapping, which confirm
    doesn't require) and returns the resulting confirmed csv_context_id."""
    resp = client.post(
        "/v1/csv/parse",
        json={"rows": _rows_for(entity_name), "filename": f"{entity_name}.csv"},
        headers={"X-Install-Id": install_id},
    )
    assert resp.status_code == 200, resp.text
    csv_context_id = resp.json()["csv_context_id"]
    assert csv_context_id

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={
            "mapping": {"Quarter Ending": "period_end", "Total Revenue": "revenue"},
            "entity_name": entity_name,
        },
        headers={"X-Install-Id": install_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed"] is True, body
    return csv_context_id


def _ask(install_id: str, csv_context_id: str, question: str = "How is revenue trending?"):
    return client.post(
        "/v1/ask",
        json={"question": question, "csv_context_id": csv_context_id},
        headers={"X-Install-Id": install_id},
    )


# --- the concurrency regression test ------------------------------------------------------


def test_concurrent_ask_calls_never_cross_installs_csv_data(install_ids, monkeypatch):
    install_a = _new_install_id(install_ids)
    install_b = _new_install_id(install_ids)
    csv_a = _create_confirmed_csv(install_a, "Alpha Bakery LLC")
    csv_b = _create_confirmed_csv(install_b, "Beta Hardware Co")

    barrier = threading.Barrier(2, timeout=10)

    def fake_run_agent(question, prior_messages=None):
        # Read this request's context-local active CSV once immediately...
        before_df = csv_session.get_active_csv()
        before_entity = before_df.attrs["entity_name"] if before_df is not None else None

        # ...then block until both simulated concurrent requests are guaranteed to be mid-flight
        # inside run_agent at the same instant -- the whole point of this test.
        barrier.wait()
        time.sleep(0.05)

        # ...and read it again after the other request has had every chance to interfere.
        after_df = csv_session.get_active_csv()
        after_entity = after_df.attrs["entity_name"] if after_df is not None else None

        return {
            "question": question,
            "final_answer": f"before={before_entity} after={after_entity}",
            "hit_iteration_cap": False,
            "iterations_used": 1,
            "stop_reason": "end_turn",
            "figure_check": {},
            "tool_calls": [],
        }

    monkeypatch.setattr(app_main, "run_agent", fake_run_agent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_ask, install_a, csv_a)
        future_b = pool.submit(_ask, install_b, csv_b)
        resp_a = future_a.result(timeout=15)
        resp_b = future_b.result(timeout=15)

    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text

    answer_a = resp_a.json()["final_answer"]
    answer_b = resp_b.json()["final_answer"]

    # Each request's context saw only its own install's entity name, both before and after the
    # barrier -- never the other install's, even though both requests were provably overlapping
    # inside run_agent at the same time.
    assert answer_a == "before=Alpha Bakery LLC after=Alpha Bakery LLC", answer_a
    assert answer_b == "before=Beta Hardware Co after=Beta Hardware Co", answer_b


# --- non-concurrent refusal paths (design doc item 3) --------------------------------------


def test_ask_with_unconfirmed_csv_context_id_is_refused(install_ids, monkeypatch):
    install_id = _new_install_id(install_ids)
    resp = client.post(
        "/v1/csv/parse",
        json={"rows": _rows_for("Gamma Consulting"), "filename": "gamma.csv"},
        headers={"X-Install-Id": install_id},
    )
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]  # never confirmed

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: pytest.fail("run_agent must not be called when the CSV context is refused"),
    )

    resp = _ask(install_id, csv_context_id)
    assert resp.status_code == 404, resp.text


def test_ask_with_foreign_csv_context_id_is_refused(install_ids, monkeypatch):
    owner_id = _new_install_id(install_ids)
    other_id = _new_install_id(install_ids)
    csv_context_id = _create_confirmed_csv(owner_id, "Delta Retail")

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: pytest.fail("run_agent must not be called when the CSV context is refused"),
    )

    resp = _ask(other_id, csv_context_id)
    assert resp.status_code == 404, resp.text


def test_ask_with_nonexistent_csv_context_id_is_refused(install_ids, monkeypatch):
    install_id = _new_install_id(install_ids)

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: pytest.fail("run_agent must not be called when the CSV context is refused"),
    )

    resp = _ask(install_id, str(uuid.uuid4()))
    assert resp.status_code == 404, resp.text

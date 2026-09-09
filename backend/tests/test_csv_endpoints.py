"""
Integration tests for the CSV pipeline endpoints (/v1/csv/parse,
/v1/csv/{id}/propose-mapping, /v1/csv/{id}/confirm) added in Phase B session 4.

Unlike src/'s root tests/ suite, these hit a real reachable Postgres via
DATABASE_URL (backend/.env) -- per the design doc's "paid tier from day one,
including for this session's own testing" mandate, csv_statements' JSONB
columns have no sqlite-compatible substitute. Every test creates its own
install(s) and deletes them (cascading to any csv_statements rows) in
teardown, so the dev DB isn't left with test debris.

The Anthropic call inside propose-mapping is mocked by monkeypatching
src.data.csv_ingest.anthropic.Anthropic, the same MagicMock-scripted-response
idiom tests/test_csv_ingest.py already uses -- never a real API call.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from db.base import get_session

import src.data.csv_ingest as csv_ingest  # noqa: E402 -- app.main's import sets up sys.path

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


def _text_response(text_body):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text_body)])


def _mock_anthropic_client_returning(monkeypatch, mappings: list[dict]):
    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(
        return_value=_text_response(json.dumps({"mappings": mappings}))
    )
    monkeypatch.setattr(csv_ingest.anthropic, "Anthropic", MagicMock(return_value=mock_client))


_SAMPLE_ROWS = [
    ["Quarter Ending", "Total Revenue", "Net Income"],
    ["2024-01-01", "100000", "12000"],
    ["2024-04-01", "110000", "13000"],
]


def _parse(install_id: str, rows=None, filename="Sheet1!A1:C3.csv"):
    return client.post(
        "/v1/csv/parse",
        json={"rows": rows if rows is not None else _SAMPLE_ROWS, "filename": filename},
        headers={"X-Install-Id": install_id},
    )


# --- full round trip ---------------------------------------------------------------------


def test_full_parse_propose_confirm_round_trip(install_ids, monkeypatch):
    install_id = _new_install_id(install_ids)

    resp = _parse(install_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["parse_error"] is None
    assert body["columns"] == ["Quarter Ending", "Total Revenue", "Net Income"]
    assert body["sample_rows"] == _SAMPLE_ROWS[1:]
    csv_context_id = body["csv_context_id"]
    assert csv_context_id

    _mock_anthropic_client_returning(
        monkeypatch,
        [
            {"csv_column": "Quarter Ending", "proposed_role": "period_end", "rationale": "dates"},
            {"csv_column": "Total Revenue", "proposed_role": "revenue", "rationale": "revenue"},
            {"csv_column": "Net Income", "proposed_role": "net_income", "rationale": "net income"},
        ],
    )
    resp = client.post(
        f"/v1/csv/{csv_context_id}/propose-mapping", headers={"X-Install-Id": install_id}
    )
    assert resp.status_code == 200
    proposal = resp.json()
    assert proposal["note"] is None
    by_col = {p["csv_column"]: p["proposed_role"] for p in proposal["proposal"]}
    assert by_col == {
        "Quarter Ending": "period_end",
        "Total Revenue": "revenue",
        "Net Income": "net_income",
    }

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={
            "mapping": {
                "Quarter Ending": "period_end",
                "Total Revenue": "revenue",
                "Net Income": "net_income",
            },
            "entity_name": "Test Bakery LLC",
        },
        headers={"X-Install-Id": install_id},
    )
    assert resp.status_code == 200
    confirmed = resp.json()
    assert confirmed["confirmed"] is True
    assert confirmed["errors"] == []
    assert confirmed["cadence"] == "quarterly"
    assert "total_assets" in confirmed["concepts_unavailable"]
    assert "revenue" not in confirmed["concepts_unavailable"]

    session = get_session()
    try:
        row = session.execute(
            text("SELECT status, entity_name, statement_data FROM csv_statements WHERE id = :id"),
            {"id": csv_context_id},
        ).fetchone()
        assert row.status == "confirmed"
        assert row.entity_name == "Test Bakery LLC"
        assert len(row.statement_data) == 2
    finally:
        session.close()


# --- Sheets-only date contract -----------------------------------------------------------


def test_confirm_rejects_serial_number_in_period_column(install_ids):
    install_id = _new_install_id(install_ids)
    rows = [
        ["Quarter Ending", "Total Revenue"],
        ["45292", "100000"],
        ["45383", "110000"],
    ]
    resp = _parse(install_id, rows=rows)
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={
            "mapping": {"Quarter Ending": "period_end", "Total Revenue": "revenue"},
            "entity_name": "Test Bakery LLC",
        },
        headers={"X-Install-Id": install_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is False
    assert any("45292" in e for e in body["errors"])

    session = get_session()
    try:
        status = session.execute(
            text("SELECT status FROM csv_statements WHERE id = :id"), {"id": csv_context_id}
        ).scalar()
        assert status == "unconfirmed"
    finally:
        session.close()


# --- cross-install isolation --------------------------------------------------------------


def test_cross_install_access_is_refused(install_ids):
    owner_id = _new_install_id(install_ids)
    other_id = _new_install_id(install_ids)

    resp = _parse(owner_id)
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]

    resp = client.post(
        f"/v1/csv/{csv_context_id}/propose-mapping", headers={"X-Install-Id": other_id}
    )
    assert resp.status_code == 404

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={"mapping": {"Quarter Ending": "period_end"}, "entity_name": "Nope"},
        headers={"X-Install-Id": other_id},
    )
    assert resp.status_code == 404

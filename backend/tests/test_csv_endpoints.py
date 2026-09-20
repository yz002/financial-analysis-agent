"""
Integration tests for the CSV pipeline endpoints (/v1/csv/parse,
/v1/csv/{id}/propose-mapping, /v1/csv/{id}/confirm) added in Phase B session 4.

Unlike src/'s root tests/ suite, these hit a real reachable Postgres via
DATABASE_URL (backend/.env) -- per the design doc's "paid tier from day one,
including for this session's own testing" mandate, csv_statements' JSONB
columns have no sqlite-compatible substitute. Every test authenticates via
the shared auth_session fixture (conftest.py), which deletes the account(s)
it creates (cascading to any csv_statements rows) in teardown, so the dev DB
isn't left with test debris.

The Anthropic call inside propose-mapping is mocked by monkeypatching
src.data.csv_ingest.anthropic.Anthropic, the same MagicMock-scripted-response
idiom tests/test_csv_ingest.py already uses -- never a real API call.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from db.base import get_session

import src.data.csv_ingest as csv_ingest  # noqa: E402 -- app.main's import sets up sys.path

client = TestClient(app)


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


def _parse(headers: dict, rows=None, filename="Sheet1!A1:C3.csv"):
    return client.post(
        "/v1/csv/parse",
        json={"rows": rows if rows is not None else _SAMPLE_ROWS, "filename": filename},
        headers=headers,
    )


# --- full round trip ---------------------------------------------------------------------


def test_full_parse_propose_confirm_round_trip(auth_session, monkeypatch):
    _, headers = auth_session()

    resp = _parse(headers)
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
    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
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
        headers=headers,
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


def test_confirm_rejects_serial_number_in_period_column(auth_session):
    _, headers = auth_session()
    rows = [
        ["Quarter Ending", "Total Revenue"],
        ["45292", "100000"],
        ["45383", "110000"],
    ]
    resp = _parse(headers, rows=rows)
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={
            "mapping": {"Quarter Ending": "period_end", "Total Revenue": "revenue"},
            "entity_name": "Test Bakery LLC",
        },
        headers=headers,
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


# --- cross-account isolation --------------------------------------------------------------


def test_cross_account_access_is_refused(auth_session):
    _, owner_headers = auth_session()
    _, other_headers = auth_session()

    resp = _parse(owner_headers)
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]

    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=other_headers)
    assert resp.status_code == 404

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={"mapping": {"Quarter Ending": "period_end"}, "entity_name": "Nope"},
        headers=other_headers,
    )
    assert resp.status_code == 404


# --- propose-mapping error responses must not leak exception detail ----------------------
#
# Session 10's security hardening pass fixed this leak for /v1/ask's equivalent
# anthropic.APIError and generic-exception handlers (see test_byo_key.py's
# test_ask_generic_api_error_does_not_leak_exception_detail /
# test_ask_generic_exception_does_not_leak_exception_detail) but never checked
# propose-mapping's own two handlers, which EXTENSION_INTEGRATION.md's writeup surfaced
# as still doing `detail=f"...: {e}"`. Fixed alongside that document; these tests are the
# regression check.


def _httpx_request():
    # anthropic's exception constructors type-hint request/response as httpx2, not httpx --
    # use the real type (matching test_byo_key.py's own idiom) rather than relying on the
    # hint going unenforced.
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_propose_mapping_api_error_does_not_leak_exception_detail(auth_session, monkeypatch):
    _, headers = auth_session()
    resp = _parse(headers)
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]

    marker = "internal-anthropic-error-detail-should-not-leak"

    def _raise_api_error(raw, roles):
        raise anthropic.APIError(marker, _httpx_request(), body=None)

    monkeypatch.setattr(app_main, "generate_mapping_proposal", _raise_api_error)

    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    assert resp.status_code == 502, resp.text
    assert marker not in resp.text
    assert resp.json()["detail"] == "Anthropic API error."


def test_propose_mapping_generic_exception_does_not_leak_exception_detail(auth_session, monkeypatch):
    _, headers = auth_session()
    resp = _parse(headers)
    assert resp.status_code == 200
    csv_context_id = resp.json()["csv_context_id"]

    marker = "internal-mapping-failure-detail-should-not-leak"

    def _raise_generic_error(raw, roles):
        raise RuntimeError(marker)

    monkeypatch.setattr(app_main, "generate_mapping_proposal", _raise_generic_error)

    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    assert resp.status_code == 500, resp.text
    assert marker not in resp.text
    assert resp.json()["detail"] == "propose_mapping failed unexpectedly."

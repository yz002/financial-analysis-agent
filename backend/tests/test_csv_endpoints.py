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


# --- Phase D session 3b contract amendment: source, size caps --------------------------------

_SOURCE = {
    "platform": "google_sheets",
    "sheet_name": "P&L",
    "range": "A3:C5",
    "file_name": "FA Spike Test",
    "modified_at": None,
}


def _count_csv_rows(account_id: str) -> int:
    session = get_session()
    try:
        return session.execute(
            text("SELECT count(*) FROM csv_statements WHERE account_id = :id"), {"id": account_id}
        ).scalar()
    finally:
        session.close()


def test_source_is_stored_and_confirmed_provenance_cites_the_cell(auth_session):
    _, headers = auth_session()
    resp = client.post(
        "/v1/csv/parse",
        json={"rows": _SAMPLE_ROWS, "filename": "FA Spike Test — P&L", "source": _SOURCE},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["parse_error"] is None
    csv_context_id = resp.json()["csv_context_id"]

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={
            "mapping": {
                "Quarter Ending": "period_end", "Total Revenue": "revenue", "Net Income": "net_income",
            },
            "entity_name": "Spike Co",
        },
        headers=headers,
    )
    assert resp.json()["confirmed"] is True, resp.text

    session = get_session()
    try:
        raw_columns, attrs = session.execute(
            text("SELECT raw_columns, statement_attrs FROM csv_statements WHERE id = :id"),
            {"id": csv_context_id},
        ).one()
    finally:
        session.close()
    assert raw_columns["source"] == _SOURCE
    # Header in row 3, so the first data row is row 4; Total Revenue is the range's 2nd column.
    assert attrs["csv_provenance"]["revenue"]["2024-01-01"]["source_cell"] == "'P&L'!B4"
    assert attrs["csv_provenance"]["net_income"]["2024-04-01"]["source_cell"] == "'P&L'!C5"


def test_source_range_that_does_not_match_the_rows_is_refused_in_band(auth_session):
    account_id, headers = auth_session()
    resp = client.post(
        "/v1/csv/parse",
        json={"rows": _SAMPLE_ROWS, "filename": "f", "source": {**_SOURCE, "range": "A3:C9"}},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["csv_context_id"] is None
    assert "were sent" in resp.json()["parse_error"]
    assert _count_csv_rows(account_id) == 0


def test_malformed_source_range_is_a_422(auth_session):
    _, headers = auth_session()
    resp = client.post(
        "/v1/csv/parse",
        json={"rows": _SAMPLE_ROWS, "filename": "f", "source": {**_SOURCE, "range": "Sheet1!A1"}},
        headers=headers,
    )
    assert resp.status_code == 422


def test_non_string_cell_is_still_a_422(auth_session):
    _, headers = auth_session()
    resp = _parse(headers, rows=[["Quarter Ending", "Total Revenue"], ["2024-01-01", 100000]])
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"][0] == "body"


def test_malformed_json_body_is_a_422(auth_session):
    _, headers = auth_session()
    resp = client.post(
        "/v1/csv/parse",
        content=b"{not json",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_empty_header_is_refused_in_band(auth_session):
    _, headers = auth_session()
    resp = _parse(headers, rows=[["Quarter Ending", ""], ["2024-01-01", "1"]])
    assert resp.status_code == 200
    assert "empty header" in resp.json()["parse_error"]


def test_total_cell_cap_is_refused_in_band(auth_session):
    account_id, headers = auth_session()
    columns = 100
    rows = [[f"c{i}" for i in range(columns)]] + [["1"] * columns] * 500  # 50,100 cells
    resp = _parse(headers, rows=rows)
    assert resp.status_code == 200
    assert resp.json()["csv_context_id"] is None
    assert "50000" in resp.json()["parse_error"]
    assert _count_csv_rows(account_id) == 0


def _oversize_body() -> bytes:
    body = json.dumps({"rows": [["Note"], ["x" * 900]] * 2400, "filename": "big"}).encode()
    assert len(body) > app_main.MAX_CSV_PARSE_BODY_BYTES
    return body


def test_body_over_the_cap_is_refused_in_band_via_content_length(auth_session):
    account_id, headers = auth_session()
    resp = client.post(
        "/v1/csv/parse",
        content=_oversize_body(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["csv_context_id"] is None
    assert "too large" in resp.json()["parse_error"]
    assert _count_csv_rows(account_id) == 0


def test_chunked_body_over_the_cap_is_refused_in_band(auth_session):
    """A generator body goes out chunked, with no Content-Length header, so this exercises the
    streamed byte count rather than the header check."""
    account_id, headers = auth_session()
    body = _oversize_body()

    def chunks():
        for i in range(0, len(body), 64 * 1024):
            yield body[i : i + 64 * 1024]

    resp = client.post(
        "/v1/csv/parse",
        content=chunks(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert "too large" in resp.json()["parse_error"]
    assert _count_csv_rows(account_id) == 0


def test_missing_auth_is_401_even_with_an_oversize_body():
    resp = client.post(
        "/v1/csv/parse", content=_oversize_body(), headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401


# --- Phase D session 4: propose/confirm amendments ------------------------------------------

_FULL_MAPPING = {"Quarter Ending": "period_end", "Total Revenue": "revenue", "Net Income": "net_income"}
_GOOD_PROPOSAL = [
    {"csv_column": "Quarter Ending", "proposed_role": "period_end", "rationale": "dates"},
    {"csv_column": "Total Revenue", "proposed_role": "revenue", "rationale": "revenue"},
    {"csv_column": "Net Income", "proposed_role": "net_income", "rationale": "net income"},
]


def _parse_with_source(headers, rows=None):
    resp = client.post(
        "/v1/csv/parse",
        json={"rows": rows or _SAMPLE_ROWS, "filename": "FA Spike Test — P&L", "source": _SOURCE},
        headers=headers,
    )
    assert resp.json()["parse_error"] is None, resp.text
    return resp.json()["csv_context_id"]


def _confirm(headers, csv_context_id, **overrides):
    body = {"mapping": _FULL_MAPPING, "entity_name": "Spike Co", "scale": "ones", **overrides}
    return client.post(f"/v1/csv/{csv_context_id}/confirm", json=body, headers=headers)


def _count_proposal_events(account_id: str) -> int:
    session = get_session()
    try:
        return session.execute(
            text(
                "SELECT count(*) FROM usage_events "
                "WHERE account_id = :id AND outcome = 'mapping_proposal'"
            ),
            {"id": account_id},
        ).scalar()
    finally:
        session.close()


def _expire(csv_context_id: str) -> None:
    session = get_session()
    try:
        session.execute(
            text("UPDATE csv_statements SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": csv_context_id},
        )
        session.commit()
    finally:
        session.close()


def test_propose_is_idempotent_per_context_and_recorded_once(auth_session, monkeypatch):
    account_id, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(
        return_value=_text_response(json.dumps({"mappings": _GOOD_PROPOSAL}))
    )
    monkeypatch.setattr(csv_ingest.anthropic, "Anthropic", MagicMock(return_value=mock_client))

    first = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    second = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert mock_client.messages.create.call_count == 1
    assert _count_proposal_events(account_id) == 1


def test_unusable_model_output_is_not_cached(auth_session, monkeypatch):
    account_id, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(return_value=_text_response("not json"))
    monkeypatch.setattr(csv_ingest.anthropic, "Anthropic", MagicMock(return_value=mock_client))

    for _ in range(2):
        resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["note"] is not None
    assert mock_client.messages.create.call_count == 2
    assert _count_proposal_events(account_id) == 2


def test_proposals_never_count_toward_the_question_cap(auth_session, monkeypatch):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    _mock_anthropic_client_returning(monkeypatch, _GOOD_PROPOSAL)
    assert client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers).status_code == 200
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["questions_today"] == 0


def test_proposal_cap_is_429_and_deleting_the_csv_rows_does_not_reset_it(auth_session, monkeypatch):
    from app.gating import MAPPING_PROPOSAL_DAILY_CAP

    account_id, headers = auth_session()
    session = get_session()
    try:
        for _ in range(MAPPING_PROPOSAL_DAILY_CAP):
            session.execute(
                text(
                    "INSERT INTO usage_events (account_id, occurred_at, outcome) "
                    "VALUES (:id, now() - interval '1 hour', 'mapping_proposal')"
                ),
                {"id": account_id},
            )
        session.commit()
    finally:
        session.close()
    _mock_anthropic_client_returning(monkeypatch, _GOOD_PROPOSAL)

    csv_context_id = _parse_with_source(headers)
    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "mapping_cap_reached"
    assert resp.json()["detail"]["resets_at"]

    # What the retention cron does to expired unconfirmed rows: the count must survive it.
    session = get_session()
    try:
        session.execute(text("DELETE FROM csv_statements WHERE account_id = :id"), {"id": account_id})
        session.commit()
    finally:
        session.close()
    csv_context_id = _parse_with_source(headers)
    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    assert resp.status_code == 429


def test_expired_context_is_404_for_propose_and_confirm(auth_session, monkeypatch):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    _expire(csv_context_id)
    _mock_anthropic_client_returning(monkeypatch, _GOOD_PROPOSAL)
    assert client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers).status_code == 404
    assert _confirm(headers, csv_context_id).status_code == 404


def test_confirmed_context_is_immutable(auth_session, monkeypatch):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    assert _confirm(headers, csv_context_id).json()["confirmed"] is True

    resp = _confirm(headers, csv_context_id, mapping={**_FULL_MAPPING, "Net Income": "unmapped"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "csv context already confirmed"
    _mock_anthropic_client_returning(monkeypatch, _GOOD_PROPOSAL)
    assert client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers).status_code == 409


def test_blank_entity_name_is_a_422(auth_session):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    assert _confirm(headers, csv_context_id, entity_name="   ").status_code == 422


def test_unknown_column_is_an_in_band_error_not_a_500(auth_session):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    resp = _confirm(headers, csv_context_id, mapping={"Nope": "period_end", "Total Revenue": "revenue"})
    assert resp.status_code == 200
    assert resp.json()["confirmed"] is False
    assert any("Nope" in e for e in resp.json()["errors"])


def test_scale_and_currency_are_applied_and_stored(auth_session):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers)
    resp = _confirm(headers, csv_context_id, scale="thousands", currency="USD")
    assert resp.json()["confirmed"] is True
    assert (resp.json()["scale"], resp.json()["currency"]) == ("thousands", "USD")

    session = get_session()
    try:
        data, attrs = session.execute(
            text("SELECT statement_data, statement_attrs FROM csv_statements WHERE id = :id"),
            {"id": csv_context_id},
        ).one()
    finally:
        session.close()
    assert data[0]["revenue"] == 100000000.0  # "100000" in thousands
    assert attrs["csv_source"]["scale"] == "thousands"
    assert attrs["csv_source"]["currency"] == "USD"


_ROWS_WITH_DIV0 = [
    ["Quarter Ending", "Total Revenue", "Net Income"],
    ["2024-01-01", "100000", "#DIV/0!"],
    ["2024-04-01", "110000", "13000"],
]


def test_unparsed_cells_need_an_acknowledgement_bound_to_what_was_shown(auth_session):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers, rows=_ROWS_WITH_DIV0)

    first = _confirm(headers, csv_context_id).json()
    assert first["confirmed"] is False
    assert first["requires_acknowledgement"] is True
    assert first["unparsed_cells"] == [
        {
            "cell": "'P&L'!C4", "source_row": 0, "column": "Net Income", "role": "net_income",
            "period_end": "2024-01-01", "value": "#DIV/0!",
        }
    ]
    fingerprint = first["ack_fingerprint"]
    assert fingerprint

    # Accepting without the fingerprint isn't enough.
    resp = _confirm(headers, csv_context_id, accept_unparsed_cells=True).json()
    assert resp["requires_acknowledgement"] is True and resp["confirmed"] is False

    confirmed = _confirm(
        headers, csv_context_id, accept_unparsed_cells=True, ack_fingerprint=fingerprint
    ).json()
    assert confirmed["confirmed"] is True, confirmed
    assert confirmed["unparsed_cells"][0]["cell"] == "'P&L'!C4"


def test_acknowledgement_for_a_different_scale_or_mapping_is_not_accepted(auth_session):
    _, headers = auth_session()
    csv_context_id = _parse_with_source(headers, rows=_ROWS_WITH_DIV0)
    fingerprint = _confirm(headers, csv_context_id).json()["ack_fingerprint"]

    # Same cells, but the person changed the scale since acknowledging: ask again.
    resp = _confirm(
        headers, csv_context_id, scale="thousands", accept_unparsed_cells=True,
        ack_fingerprint=fingerprint,
    ).json()
    assert resp["confirmed"] is False
    assert resp["requires_acknowledgement"] is True
    assert resp["ack_fingerprint"] != fingerprint

    # A different mapping (Net Income no longer mapped) means a different, here empty, list:
    # nothing to acknowledge, so it confirms.
    resp = _confirm(
        headers, csv_context_id, mapping={**_FULL_MAPPING, "Net Income": "unmapped"},
        accept_unparsed_cells=True, ack_fingerprint=fingerprint,
    ).json()
    assert resp["confirmed"] is True
    assert resp["unparsed_cells"] == []

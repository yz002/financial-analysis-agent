"""
Tests for src/data/csv_ingest.py: structural CSV parsing and the LLM mapping-proposal call.
Fully offline -- parse_csv touches no network at all, and propose_mapping's Anthropic client
is always a MagicMock scripted with a canned response (mirroring tests/test_agent.py's
mocking idiom), never a real API call.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2 as httpx
import pandas as pd
import pytest

from src.analysis.csv_statement import MAPPABLE_ROLES
from src.data.csv_ingest import RawCsv, parse_csv, propose_mapping


def _httpx_request():
    # anthropic>=1.0's exception constructors type-hint request/response as httpx2, not httpx --
    # use the real type here rather than relying on the hint going unenforced (see NOTES.md).
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


# --- parse_csv ---------------------------------------------------------------------------


def test_valid_csv_parses():
    text = b"Date,Revenue\n2024-01-01,100\n2024-04-01,120\n"
    raw, error = parse_csv(text, "sample.csv", uploaded_at=datetime(2025, 1, 1))
    assert error is None
    assert raw is not None
    assert list(raw.df.columns) == ["Date", "Revenue"]
    assert len(raw.df) == 2
    assert raw.filename == "sample.csv"
    assert raw.uploaded_at == datetime(2025, 1, 1)


def test_bad_encoding_refuses():
    raw, error = parse_csv(b"\xff\xfe\x00invalid", "bad.csv")
    assert raw is None
    assert "UTF-8" in error


def test_empty_file_refuses():
    raw, error = parse_csv(b"", "empty.csv")
    assert raw is None
    assert error is not None


def test_header_only_no_data_rows_refuses():
    raw, error = parse_csv(b"Date,Revenue\n", "headeronly.csv")
    assert raw is None
    assert "data row" in error.lower()


def test_duplicate_headers_refuse():
    raw, error = parse_csv(b"Date,Revenue,Revenue\n2024-01-01,100,100\n", "dup.csv")
    assert raw is None
    assert "duplicate" in error.lower()


def test_malformed_csv_refuses():
    # An unterminated quote makes this unparseable as a table, not just "unusual".
    raw, error = parse_csv(b'Date,Revenue\n"2024-01-01,100\n', "malformed.csv")
    assert raw is None
    assert error is not None


# --- propose_mapping -----------------------------------------------------------------------


def _text_response(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _client_returning(text):
    client = MagicMock()
    client.messages.create = MagicMock(return_value=_text_response(text))
    return client


def _sample_raw():
    df = pd.DataFrame(
        {
            "Date": ["2024-01-01", "2024-04-01"],
            "Total Revenue": ["100", "120"],
            "Notes": ["ok", "ok"],
        }
    )
    return RawCsv(df=df, filename="sample.csv", uploaded_at=datetime(2025, 1, 1))


def test_propose_mapping_valid_json():
    proposal_json = json.dumps(
        {
            "mappings": [
                {"csv_column": "Date", "proposed_role": "period_end", "rationale": "looks like dates"},
                {"csv_column": "Total Revenue", "proposed_role": "revenue", "rationale": "revenue header"},
                {"csv_column": "Notes", "proposed_role": "unmapped", "rationale": "free text"},
            ]
        }
    )
    client = _client_returning(proposal_json)
    proposal = propose_mapping(_sample_raw(), MAPPABLE_ROLES, client=client)

    assert proposal.note is None
    by_col = {c.csv_column: c.proposed_role for c in proposal.columns}
    assert by_col == {"Date": "period_end", "Total Revenue": "revenue", "Notes": "unmapped"}
    rationale_by_col = {c.csv_column: c.rationale for c in proposal.columns}
    assert rationale_by_col["Total Revenue"] == "revenue header"


def test_propose_mapping_malformed_json_degrades_to_unmapped():
    client = _client_returning("not json at all")
    proposal = propose_mapping(_sample_raw(), MAPPABLE_ROLES, client=client)

    assert proposal.note is not None
    assert [c.proposed_role for c in proposal.columns] == ["unmapped", "unmapped", "unmapped"]
    assert [c.csv_column for c in proposal.columns] == ["Date", "Total Revenue", "Notes"]


def test_propose_mapping_missing_column_defaults_to_unmapped():
    # The model only mentions one of the three columns; the other two must still appear,
    # defaulted to unmapped, never silently dropped from the proposal.
    proposal_json = json.dumps(
        {"mappings": [{"csv_column": "Total Revenue", "proposed_role": "revenue", "rationale": "x"}]}
    )
    client = _client_returning(proposal_json)
    proposal = propose_mapping(_sample_raw(), MAPPABLE_ROLES, client=client)

    by_col = {c.csv_column: c.proposed_role for c in proposal.columns}
    assert by_col == {"Date": "unmapped", "Total Revenue": "revenue", "Notes": "unmapped"}


def test_propose_mapping_unknown_role_falls_back_to_unmapped():
    proposal_json = json.dumps(
        {
            "mappings": [
                {"csv_column": "Date", "proposed_role": "made_up_role", "rationale": "x"},
                {"csv_column": "Total Revenue", "proposed_role": "revenue", "rationale": "x"},
                {"csv_column": "Notes", "proposed_role": "unmapped", "rationale": "x"},
            ]
        }
    )
    client = _client_returning(proposal_json)
    proposal = propose_mapping(_sample_raw(), MAPPABLE_ROLES, client=client)

    by_col = {c.csv_column: c.proposed_role for c in proposal.columns}
    assert by_col["Date"] == "unmapped"
    assert by_col["Total Revenue"] == "revenue"


def test_propose_mapping_api_error_propagates():
    client = MagicMock()
    client.messages.create = MagicMock(side_effect=anthropic.APIConnectionError(request=_httpx_request()))

    with pytest.raises(anthropic.APIConnectionError):
        propose_mapping(_sample_raw(), MAPPABLE_ROLES, client=client)

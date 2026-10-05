"""
Figure-level citations for a run_agent answer (EXTENSION_INTEGRATION.md SS6 /v1/ask, amended
Phase D session 5): for each figure guardrails.check_figures found in the answer, where it came
from -- a spreadsheet cell, the cells and formula behind a derived value, an SEC filing fact, or
another tool's output.

Built entirely from what the run already has: check_figures' per-figure report, the tool results
the model saw (this run's and the replayed earlier turns'), and optionally the statement's
stored raw rows. Nothing here computes a financial figure. It finds and labels the tool values
that were computed upstream, in Python, and copies the cell text exactly as it was read.

Three rules this module exists to keep:
- A derived value (a margin, a growth rate, a trailing-twelve-month sum) is never cited as a
  single cell. It's cited as its formula plus each input's own source, including the prior
  period a growth rate compares against and every quarter of a TTM window.
- A figure that matches more than one distinct source is "ambiguous", and every source is
  listed. Picking one silently would claim a provenance nobody established.
- `read_value` is the cell's content exactly as it was sent to /v1/csv/parse (the underlying
  value, not the sheet's formatting), taken from the stored raw rows. It's never recomputed
  from the value in ones, and it's left null rather than guessed whenever the stored rows
  can't be shown to be the same cell.
"""

import json
import re

from . import guardrails
from ..data.sheet_ingest import cell_reference, parse_a1_range

# The formula behind each ratio tools.get_ratios/get_csv_ratios returns, as display text for
# the citation -- the computation itself is ratios.py's. Keyed by ratio name; roa/roe depend on
# whether the row's numerator is the trailing-twelve-month sum (see _ratio_formula).
_RATIO_FORMULAS = {
    "gross_margin": "gross_profit / revenue",
    "operating_margin": "operating_income / revenue",
    "net_margin": "net_income / revenue",
    "revenue_growth_qoq": "(revenue - revenue_prior) / revenue_prior",
    "revenue_growth_yoy": "(revenue - revenue_prior) / revenue_prior",
    "earnings_growth_qoq": "(net_income - net_income_prior) / net_income_prior",
    "earnings_growth_yoy": "(net_income - net_income_prior) / net_income_prior",
    "free_cash_flow": "operating_cash_flow - capex",
    "debt_to_assets": "total_liabilities / total_assets",
    "current_ratio": "current_assets / current_liabilities",
}
_RETURN_RATIO_DENOMINATORS = {"roa": "total_assets", "roe": "stockholders_equity"}
_TTM_ROLE = "net_income_ttm"
_TTM_FORMULA = "sum of net_income for this quarter and the 3 quarters before it"

_PATH_TOKEN_RE = re.compile(r"\[(\d+)\]|([^.\[\]]+)")


def _parse_path(path: str) -> list:
    """guardrails' json_path ("ratios.gross_margin[3].inputs.revenue") as tokens, with list
    indexes as ints: ["ratios", "gross_margin", 3, "inputs", "revenue"]."""
    return [int(index) if index else key for index, key in _PATH_TOKEN_RE.findall(path)]


def _payloads(tool_calls: list[dict], prior_tool_calls: list[dict] | None) -> dict:
    """Each tool call's decoded result, keyed (turn_id, tool_call_index) exactly as
    guardrails.collect_tool_values indexes them -- prior calls are grouped per turn, in order of
    first appearance, and indexed within their own turn."""
    def decode(call):
        try:
            return json.loads(call.get("tool_result") or "")
        except (json.JSONDecodeError, TypeError):
            return None

    payloads = {(None, i): decode(call) for i, call in enumerate(tool_calls)}
    by_turn: dict = {}
    for call in prior_tool_calls or []:
        by_turn.setdefault(call.get("turn_id"), []).append(call)
    for turn_id, calls in by_turn.items():
        for i, call in enumerate(calls):
            payloads[(turn_id, i)] = decode(call)
    return payloads


def _read_value(entry: dict, statement_raw: dict | None) -> str | None:
    """The stored raw text of the cell `entry` cites, or None when it can't be shown to be that
    cell. statement_raw is csv_statements.raw_columns: {"columns": [...headers], "data_rows":
    [[str, ...], ...] (header excluded, 0-based, the same index as source_row), "source": the
    spreadsheet range, if any}. When the entry names a cell, the address recomputed from the
    stored range must match it -- a value from some other statement is never quoted."""
    if not statement_raw or "source_row" not in entry or "source_column" not in entry:
        return None
    columns = statement_raw.get("columns") or []
    data_rows = statement_raw.get("data_rows") or []
    row, column = entry["source_row"], entry["source_column"]
    if column not in columns or not isinstance(row, int) or not 0 <= row < len(data_rows):
        return None
    col_idx = columns.index(column)
    source = statement_raw.get("source")
    cited_cell = entry.get("source_cell")
    if source or cited_cell:
        origin = parse_a1_range(source["range"]) if source else None
        if origin is None or cited_cell is None:
            return None
        if cell_reference(source["sheet_name"], origin[0] + 1 + row, origin[1] + col_idx) != cited_cell:
            return None
    value = data_rows[row][col_idx]
    return None if value is None else str(value)


def _source(entry, concept: str, period_end, payload: dict, tool_name, statement_raw):
    """(kind, source) for one concept value's provenance entry: a spreadsheet cell, an SEC
    filing fact, or (None, None) when it carries neither."""
    if not isinstance(entry, dict):
        return None, None
    if "source_row" in entry or "source_cell" in entry:
        return "cell", {
            "cell": entry.get("source_cell"),
            "concept": concept,
            "column": entry.get("source_column"),
            "period_end": period_end,
            "read_value": _read_value(entry, statement_raw),
            "sheet_scale": entry.get("sheet_scale"),
        }
    if tool_name in ("get_financial_statement", "get_ratios") and "tag" in entry:
        source = {
            "ticker": payload.get("ticker"),
            "concept": concept,
            "tag": entry.get("tag"),
            "filed": entry.get("filed"),
            "period_end": period_end,
            "is_derived": entry.get("is_derived"),
        }
        if entry.get("derivation_method"):
            source["derivation_method"] = entry["derivation_method"]
        return "filing", source
    return None, None


def _ratio_formula(name: str, inputs: dict) -> str | None:
    if name in _RETURN_RATIO_DENOMINATORS:
        numerator = _TTM_ROLE if _TTM_ROLE in inputs else "net_income"
        return f"{numerator} / {_RETURN_RATIO_DENOMINATORS[name]}"
    return _RATIO_FORMULAS.get(name)


def _ttm_computation(row: dict, payload, tool_name, statement_raw) -> dict:
    """The trailing-twelve-month numerator as a derived value: one input per quarter summed.
    Quarterly values aren't in a ratio row, only their provenance, so each input's value is
    null and its source carries the cell (whose read_value shows what was read)."""
    window = (row.get("provenance") or {}).get(_TTM_ROLE) or []
    inputs = []
    for entry in window:
        _kind, source = _source(
            entry, "net_income", entry.get("period_end"), payload, tool_name, statement_raw
        )
        inputs.append({"role": "net_income", "value": None, "source": source})
    return {
        "name": _TTM_ROLE,
        "formula": _TTM_FORMULA,
        "period_end": row.get("period_end"),
        "inputs": inputs,
    }


def _input_source(role: str, row: dict, payload, tool_name, statement_raw) -> dict | None:
    """The source of one ratio input: the prior period's own provenance for a growth rate's
    `{concept}_prior`, otherwise the row period's provenance for that concept."""
    provenance = row.get("provenance") or {}
    entry = provenance.get(role)
    if role.endswith("_prior"):
        concept = role.removesuffix("_prior")
        period_end = entry.get("period_end") if isinstance(entry, dict) else None
    else:
        concept, period_end = role, row.get("period_end")
    _kind, source = _source(entry, concept, period_end, payload, tool_name, statement_raw)
    return source


def _ratio_computation(name: str, row: dict, payload, tool_name, statement_raw) -> dict:
    inputs_out = []
    for role, value in (row.get("inputs") or {}).items():
        if role.endswith("_reason"):
            continue
        if role == _TTM_ROLE:
            inputs_out.append(
                {
                    "role": role,
                    "value": value,
                    "source": None,
                    "computation": _ttm_computation(row, payload, tool_name, statement_raw),
                }
            )
            continue
        inputs_out.append(
            {
                "role": role,
                "value": value,
                "source": _input_source(role, row, payload, tool_name, statement_raw),
            }
        )
    return {
        "name": name,
        "formula": _ratio_formula(name, row.get("inputs") or {}),
        "period_end": row.get("period_end"),
        "inputs": inputs_out,
    }


def _entity(payload: dict):
    return payload.get("ticker") or payload.get("business_name")


def _key(kind: str, source: dict, entity) -> tuple:
    """Two matches with the same key are the same provenance -- e.g. one cell reached both
    through get_csv_statement and through a ratio's inputs -- and count once."""
    if kind == "cell":
        address = source.get("cell") or (source.get("column"), source.get("period_end"))
        return ("cell", entity, source.get("concept"), address)
    return ("filing", entity, source.get("concept"), source.get("period_end"), source.get("tag"))


def _describe(hit: dict, payloads: dict, statement_raw) -> tuple[tuple, dict]:
    """(dedupe_key, match) for one tool value check_figures matched."""
    turn_id, index = hit["turn_id"], hit["tool_call_index"]
    tool_name = hit["tool_name"]
    payload = payloads.get((turn_id, index)) or {}
    tokens = _parse_path(hit["json_path"])
    base = {"value": hit["value"], "turn_id": turn_id}

    try:
        # periods[i].<concept>.value -- one statement value.
        if len(tokens) == 4 and tokens[0] == "periods" and tokens[3] == "value":
            period = payload["periods"][tokens[1]]
            concept = tokens[2]
            kind, source = _source(
                period[concept], concept, period.get("period_end"), payload, tool_name, statement_raw
            )
            if kind is not None:
                return _key(kind, source, _entity(payload)), {**base, "kind": kind, "source": source}

        # ratios.<name>[i].value -- a derived value.
        if len(tokens) == 4 and tokens[0] == "ratios" and tokens[3] == "value":
            name = tokens[1]
            row = payload["ratios"][name][tokens[2]]
            computation = _ratio_computation(name, row, payload, tool_name, statement_raw)
            key = ("derived", _entity(payload), name, row.get("period_end"))
            return key, {**base, "kind": "derived", "computation": computation}

        # ratios.<name>[i].inputs.<role> -- one of a ratio's inputs.
        if len(tokens) == 5 and tokens[0] == "ratios" and tokens[3] == "inputs":
            row = payload["ratios"][tokens[1]][tokens[2]]
            role = tokens[4]
            if role == _TTM_ROLE:
                computation = _ttm_computation(row, payload, tool_name, statement_raw)
                key = ("derived", _entity(payload), _TTM_ROLE, row.get("period_end"))
                return key, {**base, "kind": "derived", "computation": computation}
            source = _input_source(role, row, payload, tool_name, statement_raw)
            if source is not None:
                kind = "cell" if "cell" in source else "filing"
                return _key(kind, source, _entity(payload)), {**base, "kind": kind, "source": source}
    except (KeyError, IndexError, TypeError):
        pass  # an unexpected shape is cited as a plain tool value below, never guessed at

    source = {"tool_name": tool_name, "json_path": hit["json_path"]}
    return ("tool", turn_id, index, hit["json_path"]), {**base, "kind": "tool", "source": source}


def build_citations(
    figure_check: dict,
    tool_calls: list[dict],
    prior_tool_calls: list[dict] | None = None,
    statement_raw: dict | None = None,
) -> list[dict]:
    """One citation per figure in figure_check["figures"], in the same order.

    `tool_calls`/`prior_tool_calls` must be exactly what check_figures was given (see
    run_agent), so the same values are found. `statement_raw` is the bound statement's
    csv_statements.raw_columns, for `read_value`; without it every read_value is null.

    status: "traced" (one distinct source), "ambiguous" (several distinct sources, all listed,
    none chosen), "weak" (only a coincidence-prone whole-number match), "untraced" (none). The
    last two have no matches. traced/weak/untraced follow check_figures' own verdict; this only
    adds where the value came from.
    """
    tool_values = guardrails.collect_tool_values(tool_calls, prior_tool_calls)
    payloads = _payloads(tool_calls, prior_tool_calls)

    citations = []
    for index, figure in enumerate(figure_check.get("figures") or []):
        citation = {
            "figure_index": index,
            "raw_text": figure["raw_text"],
            "start": figure["start"],
            "end": figure["end"],
        }
        if figure.get("weak_match"):
            citations.append({**citation, "status": "weak", "matches": []})
            continue
        if not figure.get("traced"):
            citations.append({**citation, "status": "untraced", "matches": []})
            continue

        matches, seen = [], set()
        hits = guardrails.find_all_matches(
            figure["normalized_value"], figure["precision_ndigits"], tool_values
        )
        for hit in hits:
            key, match = _describe(hit, payloads, statement_raw)
            if key not in seen:
                seen.add(key)
                matches.append(match)
        if not matches:
            citations.append({**citation, "status": "untraced", "matches": []})
            continue
        status = "traced" if len(matches) == 1 else "ambiguous"
        citations.append({**citation, "status": status, "matches": matches})
    return citations

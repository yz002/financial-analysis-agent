"""
Streamlit UI for the FP&A copilot (Phase 5). Pure display/orchestration around
`run_agent`: every number shown here is read verbatim from `run_agent`'s
return dict, never recomputed or reformatted -- the no-model-arithmetic rule
(see CLAUDE.md) extends to this layer as "no UI-layer arithmetic either."

Run with `streamlit run src/app/main.py` from the repo root.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import altair as alt
import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# `streamlit run` executes this file directly rather than via `python -m`, so it never adds the
# repo root to sys.path the way a normal package-relative invocation would -- without this, the
# `from src...` import below raises ModuleNotFoundError even when launched from the repo root.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import csv_session  # noqa: E402 -- see comment above
from src.agent.agent import run_agent  # noqa: E402 -- import after sys.path/load_dotenv setup above
from src.analysis import csv_statement  # noqa: E402 -- see comment above
from src.data import csv_ingest  # noqa: E402 -- see comment above
from src.data.cik_lookup import get_company_name  # noqa: E402 -- see comment above

EXAMPLE_QUESTIONS = [
    "What was Nvidia's revenue and gross margin last quarter?",
    "How much free cash flow did Microsoft generate in FY2025?",
    "How has Apple's operating margin trended over the last 8 quarters?",
    "Is revenue growth accelerating or decelerating for a given company?",
    "Flag anything unusual in Ford's most recent 10-Q.",
    "Compare AMD and Nvidia's margin trends — who's better positioned?",
    "Project Costco's revenue for the next two quarters and explain the assumptions.",
]

# The 13 concepts tracked by src/analysis/statements.py's get_statement(), in the same
# order get_financial_statement's JSON carries them. liabilities_noncurrent is a rarely-
# populated fallback input for total_liabilities (see statements._derive_total_liabilities)
# rather than a concept most tickers report directly -- included here anyway for schema
# consistency with the tool's output.
ALL_CONCEPTS = [
    "revenue", "gross_profit", "operating_income", "net_income",
    "operating_cash_flow", "capex", "total_assets", "total_liabilities",
    "cash", "stockholders_equity", "current_assets", "current_liabilities",
    "liabilities_noncurrent",
]

MAX_CHARTS = 4

# Ratios from src/analysis/ratios.py whose "value" is a rate/fraction conventionally read as a
# percentage (margins, growth rates, debt-to-assets, roa/roe). free_cash_flow is a dollar amount
# (no division involved), so it's grouped with ALL_CONCEPTS below instead. current_ratio is the
# only ratio left ungrouped -- a dimensionless multiple (e.g. "1.5"), not a percentage.
PERCENT_RATIOS = {
    "gross_margin", "operating_margin", "net_margin",
    "revenue_growth_qoq", "revenue_growth_yoy",
    "earnings_growth_qoq", "earnings_growth_yoy",
    "roa", "roe", "debt_to_assets",
}
DOLLAR_SERIES = set(ALL_CONCEPTS) | {"free_cash_flow"}


def _dollar_scale(values: list[float]) -> tuple[float, str]:
    """Pick the largest whole unit (B/M/K) that keeps at least one digit before the decimal
    point, so an axis never shows bare, hard-to-read raw dollar figures like 90007000000."""
    max_abs = max((abs(v) for v in values), default=0)
    if max_abs >= 1e9:
        return 1e9, "$B"
    if max_abs >= 1e6:
        return 1e6, "$M"
    if max_abs >= 1e3:
        return 1e3, "$K"
    return 1.0, "$"


def _mentions(name: str, text: str) -> int | None:
    """Return the char offset of `name`'s first mention in `text` (matching either the raw
    underscored form or its space-joined variant), or None if it isn't mentioned at all."""
    for candidate in (name.replace("_", " "), name):
        m = re.search(r"\b" + re.escape(candidate) + r"\b", text, re.IGNORECASE)
        if m:
            return m.start()
    return None


def build_charts(question: str, final_answer: str, tool_calls: list[dict]) -> list[dict]:
    """Build up to MAX_CHARTS (entity, concept/ratio) time series from this run's own
    get_financial_statement/get_ratios/get_csv_statement/get_csv_ratios tool results -- never a
    fresh fetch. Plotting every concept/ratio unfiltered would be a wall of charts (a single
    statement call alone carries 12 concepts), so candidates are ranked: anything named in the
    question or answer text first (in order of first mention), then revenue plus whatever
    ratios were actually requested via a get_ratios/get_csv_ratios ratio_names argument -- the
    model already told us what it cared about when it made that call. Unranked candidates are
    dropped rather than padding the list.
    """
    candidates = []
    seen = set()
    requested_ratio_names = []

    for tc in tool_calls:
        if tc.get("is_error"):
            continue
        try:
            data = json.loads(tc["tool_result"])
        except (json.JSONDecodeError, TypeError):
            continue

        if tc["tool_name"] in ("get_financial_statement", "get_csv_statement"):
            # get_csv_statement has no "ticker" -- it identifies the business via
            # "business_name" instead (see src/agent/tools.py; a CSV upload's human-supplied
            # name is not a real ticker, so it's kept out of the "ticker" field entirely to
            # avoid get_company_name below ever mistaking it for one). Reusing the same
            # internal "ticker" candidate-dict key for both is just this function's own uid/
            # grouping key, not anything exposed outside build_charts.
            is_csv = tc["tool_name"] == "get_csv_statement"
            ticker = data.get("business_name", "?") if is_csv else data.get("ticker", "?")
            for concept in ALL_CONCEPTS:
                points = {
                    row["period_end"]: row[concept]["value"]
                    for row in data.get("periods", [])
                    if isinstance(row.get(concept), dict) and row[concept].get("value") is not None
                }
                uid = (ticker, concept)
                if len(points) >= 2 and uid not in seen:
                    seen.add(uid)
                    candidates.append(
                        {
                            "name": concept, "ticker": ticker, "points": points,
                            "source": "csv" if is_csv else "edgar",
                        }
                    )

        elif tc["tool_name"] in ("get_ratios", "get_csv_ratios"):
            is_csv = tc["tool_name"] == "get_csv_ratios"
            ticker = data.get("business_name", "?") if is_csv else data.get("ticker", "?")
            for ratio_name, rows in data.get("ratios", {}).items():
                points = {r["period_end"]: r["value"] for r in rows if r.get("value") is not None}
                uid = (ticker, ratio_name)
                if len(points) >= 2 and uid not in seen:
                    seen.add(uid)
                    candidates.append(
                        {
                            "name": ratio_name, "ticker": ticker, "points": points,
                            "source": "csv" if is_csv else "edgar",
                        }
                    )
            for name in (tc.get("tool_input") or {}).get("ratio_names") or []:
                if name not in requested_ratio_names:
                    requested_ratio_names.append(name)

    text = f"{question}\n{final_answer}"
    mentioned = sorted(
        ((_mentions(c["name"], text), c) for c in candidates if _mentions(c["name"], text) is not None),
        key=lambda pair: pair[0],
    )
    ranked = [c for _, c in mentioned]

    if len(ranked) < MAX_CHARTS:
        ranked_ids = {(c["ticker"], c["name"]) for c in ranked}
        for name in ["revenue", *requested_ratio_names]:
            for c in candidates:
                uid = (c["ticker"], c["name"])
                if c["name"] == name and uid not in ranked_ids:
                    ranked.append(c)
                    ranked_ids.add(uid)
            if len(ranked) >= MAX_CHARTS:
                break

    charts = []
    for c in ranked[:MAX_CHARTS]:
        df = pd.DataFrame({"period_end": pd.to_datetime(list(c["points"].keys())),
                            "value": list(c["points"].values())})
        df = df.sort_values("period_end").reset_index(drop=True)
        # Never call get_company_name for a CSV-sourced candidate: c["ticker"] is a
        # human-supplied business name here, not a real ticker, and get_company_name's plain
        # dict lookup would silently mislabel it if it happened to collide with a real ticker
        # symbol (e.g. a business literally named "IBM") -- degrading gracefully for an
        # *unrecognized* string isn't the same as being safe to call on a non-ticker one.
        company = c["ticker"] if c["source"] == "csv" else (get_company_name(c["ticker"]) or c["ticker"])
        kind = "percent" if c["name"] in PERCENT_RATIOS else "dollar" if c["name"] in DOLLAR_SERIES else "ratio"
        charts.append({"title": f"{company} — {c['name']}", "df": df, "kind": kind})
    return charts


def _escape_markdown_dollars(text: str) -> str:
    """st.markdown treats "$"/"$$" as a LaTeX (KaTeX) math delimiter, so financial prose with
    two or more dollar amounts silently renders everything between them as italic math instead
    of plain text (e.g. "Price $216.39 and market cap $5.241T" turns the text between the two
    amounts into a math expression) -- confirmed by a live run. Streamlit's markdown has no
    option to disable math rendering, so every "$" from model/tool text must be escaped as "\\$"
    before display; Streamlit still renders "\\$" as a literal dollar sign."""
    return text.replace("$", r"\$")


def render_answer(result: dict) -> None:
    st.subheader("Answer")
    if result["hit_iteration_cap"]:
        st.warning(
            f"This run hit the {result['iterations_used']}-iteration safety cap before the "
            "agent reached a natural stopping point. The answer below may be incomplete -- "
            "try a narrower question or run it again."
        )
    st.markdown(_escape_markdown_dollars(result["final_answer"]))


_JSON_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _json_path_parent(payload, json_path: str):
    """Walk `json_path` (as produced by guardrails._walk_json_numbers, e.g.
    "periods[3].revenue.value" or "ratios.gross_margin[2].provenance.revenue.value") to the
    parent object containing its final key, so a caller can look up sibling fields next to the
    matched leaf (e.g. source_file/source_row/source_column/uploaded_at). Returns None if the
    path can't be resolved (malformed path, or the parent isn't a dict)."""
    tokens = [
        m.group(1) if m.group(1) is not None else int(m.group(2))
        for m in _JSON_PATH_TOKEN_RE.finditer(json_path)
    ]
    obj = payload
    try:
        for token in tokens[:-1]:
            obj = obj[token]
    except (KeyError, IndexError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


_CSV_CITATION_FIELDS = ("source_file", "source_row", "source_column", "uploaded_at")


def _figure_citation_caption(result: dict, match: dict) -> str:
    """The default caption (`via {tool_name} -> {json_path} = {matched_value}`) is an opaque
    JSON path for a human to read. When the matched leaf's sibling fields include all four CSV
    citation fields (see src/agent/tools.py's get_csv_statement/get_csv_ratios), render a real
    citation instead -- which uploaded file, row, and column the figure came from. Purely
    structural (checks field presence, not tool_name): a get_csv_ratios match on a *ratio's
    own* computed value has no single source cell and correctly falls back to the default
    caption, while a match on one of that ratio's *inputs* (which does carry the four fields,
    under provenance.{concept}) gets the rich citation exactly like a direct
    get_csv_statement match would, with no tool-specific branching needed."""
    default = f"via `{match['tool_name']}` → `{match['json_path']}` = {match['matched_value']}"
    tool_calls = result.get("tool_calls") or []
    idx = match["tool_call_index"]
    if not (0 <= idx < len(tool_calls)):
        return default
    try:
        payload = json.loads(tool_calls[idx]["tool_result"])
    except (json.JSONDecodeError, TypeError):
        return default
    parent = _json_path_parent(payload, match["json_path"])
    if parent is None or not all(f in parent for f in _CSV_CITATION_FIELDS):
        return default
    return (
        f'via uploaded file "{parent["source_file"]}", row {parent["source_row"]}, column '
        f'"{parent["source_column"]}" (uploaded {parent["uploaded_at"]}) = {match["matched_value"]}'
    )


def render_figure_check(result: dict) -> None:
    fc = result["figure_check"]
    st.subheader("Figure check")
    if fc["figures_checked"] == 0:
        st.info("No numeric figures were found in the answer to check.")
        return
    if fc["all_traced"]:
        st.success(f"All {fc['figures_checked']} figures in the answer traced back to tool data.")
    else:
        st.warning(
            f"{fc['figures_untraced']} of {fc['figures_checked']} figures could not be traced "
            "back to tool data -- treat those numbers with caution."
        )
    cols = st.columns(3)
    cols[0].metric("Figures checked", fc["figures_checked"])
    cols[1].metric("Traced", fc["figures_traced"])
    cols[2].metric("Untraced", fc["figures_untraced"])

    for fig in fc["figures"]:
        with st.container(border=True):
            icon = "✅" if fig["traced"] else ("🟡" if fig.get("weak_match") else "⚠️")
            st.markdown(f"{icon} **{_escape_markdown_dollars(fig['raw_text'])}**")
            if fig["match"] is not None:
                caption = _figure_citation_caption(result, fig["match"])
                if fig.get("weak_match"):
                    caption += " -- whole-number match only, could be coincidental"
                st.caption(caption)
            else:
                st.caption("Not traced to any tool result.")


def render_tool_calls(result: dict) -> None:
    st.subheader("Tool calls")
    if not result["tool_calls"]:
        st.caption("The agent answered without calling any tools.")
        return
    for tc in result["tool_calls"]:
        icon = "❌" if tc["is_error"] else "✅"
        with st.expander(f"{icon} Iteration {tc['iteration']} — {tc['tool_name']}"):
            st.markdown("**Input:**")
            st.json(tc["tool_input"])
            st.markdown("**Result:**")
            try:
                parsed = json.loads(tc["tool_result"])
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if parsed is not None:
                st.json(parsed)
                if tc["is_error"]:
                    st.error(f"[{parsed.get('error_type', 'unknown')}] {parsed.get('error', '')}")
            else:
                st.code(tc["tool_result"])


def _altair_chart(df: pd.DataFrame, kind: str) -> alt.Chart:
    """A line+point chart with markers only at the real period_end dates -- both the axis ticks
    and the marks. Quarterly filings are irregularly and widely spaced (roughly every 3 months),
    so a continuous line drawn against an evenly-ticked daily/weekly axis (st.line_chart's
    default over a datetime index) visually implies filed data between periods that was never
    reported -- a real grounding concern for this project, not just a cosmetic one. Restricting
    axis ticks to exactly the plotted dates, plus explicit point markers, makes clear that only
    those dates are real, filed periods."""
    tick_dates = df["period_end"].tolist()
    df = df.copy()

    if kind == "percent":
        y_axis = alt.Axis(title=None, format=".1%")
        tooltip_value = alt.Tooltip("value:Q", title="Value", format=".1%")
    elif kind == "dollar":
        scale, suffix = _dollar_scale(df["value"].tolist())
        df["value"] = df["value"] / scale
        y_axis = alt.Axis(title=f"USD ({suffix})")
        tooltip_value = alt.Tooltip("value:Q", title=f"Value ({suffix})", format=",.2f")
    else:
        y_axis = alt.Axis(title=None)
        tooltip_value = alt.Tooltip("value:Q", title="Value", format=",.2f")

    return (
        alt.Chart(df)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                "period_end:T",
                axis=alt.Axis(values=tick_dates, title=None, format="%b %Y", labelAngle=-40),
            ),
            y=alt.Y("value:Q", axis=y_axis),
            tooltip=[alt.Tooltip("period_end:T", title="Period end", format="%Y-%m-%d"), tooltip_value],
        )
    )


def _run_agent_or_error(question: str) -> tuple[dict | None, str | None]:
    """Run the agent and translate any exception into a plain-English message -- run_agent
    itself doesn't catch anthropic API errors (see src/agent/agent.py), so the UI must. Returns
    (result, error_message); exactly one of the two is non-None. Pure Python, no Streamlit
    calls, so the error paths are testable by mocking run_agent directly."""
    try:
        return run_agent(question), None
    except anthropic.AuthenticationError:
        return None, (
            "Anthropic API authentication failed. Check that ANTHROPIC_API_KEY is set "
            "in your .env file and is valid."
        )
    except anthropic.APIConnectionError:
        return None, "Could not reach the Anthropic API -- check your network connection and try again."
    except anthropic.APIError as e:
        return None, f"Anthropic API error: {e}"
    except Exception as e:  # noqa: BLE001 -- last-resort catch so the UI never shows a raw traceback
        return None, f"Unexpected error while running the agent: {e}"


def render_charts(result: dict) -> None:
    st.subheader("Charts")
    charts = build_charts(result["question"], result["final_answer"], result["tool_calls"])
    if not charts:
        st.caption("No time-series data available to chart for this question.")
        return
    for chart in charts:
        st.markdown(f"**{chart['title']}**")
        st.altair_chart(_altair_chart(chart["df"], chart["kind"]), use_container_width=True)


_CSV_STATE_KEYS = [
    "csv_file_hash", "csv_raw", "csv_parse_error",
    "csv_proposal", "csv_proposal_error", "csv_mapping_reused",
    "csv_normalized", "csv_normalize_errors", "csv_normalize_warnings",
]


def _reset_csv_state() -> None:
    """Clear every csv_* session-state key -- called whenever a genuinely new file is
    uploaded (detected by content hash, see render_csv_upload_section), so a prior file's
    parse result, mapping proposal, and normalized frame never leak into the next one. Also
    clears each per-column mapping widget's own key (csv_role_<column>) -- those persist
    independently of the keys above, and a new file's columns (even a same-named one, e.g.
    both files having a "Date" column) must start from this file's own LLM proposal, not a
    stale selection left over from the previous upload."""
    for key in _CSV_STATE_KEYS:
        st.session_state[key] = None
    for key in [k for k in st.session_state if k.startswith("csv_role_")]:
        del st.session_state[key]
    csv_session.set_active_csv(None)


def _propose_mapping_or_error(raw) -> tuple[csv_ingest.MappingProposal | None, str | None]:
    """propose_mapping, with the same exception-to-plain-English-message translation
    _run_agent_or_error gives run_agent -- csv_ingest.propose_mapping doesn't catch Anthropic
    API errors itself (same reasoning as run_agent: a genuine API failure is the UI's job to
    translate, not the function's to swallow), so this does it once for the CSV flow. A
    malformed-but-successful model response isn't an exception here -- it's a normal
    MappingProposal with every column defaulted to "unmapped" and a `.note` explaining why
    (see propose_mapping's docstring), so it doesn't come through this error path at all."""
    try:
        return csv_ingest.propose_mapping(raw, csv_statement.MAPPABLE_ROLES), None
    except anthropic.AuthenticationError:
        return None, (
            "Anthropic API authentication failed. Check that ANTHROPIC_API_KEY is set "
            "in your .env file and is valid."
        )
    except anthropic.APIConnectionError:
        return None, "Could not reach the Anthropic API -- check your network connection and try again."
    except anthropic.APIError as e:
        return None, f"Anthropic API error: {e}"
    except Exception as e:  # noqa: BLE001 -- last-resort catch so the UI never shows a raw traceback
        return None, f"Unexpected error while proposing a mapping: {e}"


def _sample_value_strings(series: pd.Series, n: int = 3) -> list[str]:
    """Stringify the first n values of a column for the mapping-review caption. Skips
    .astype(str) -- under pandas 3's default string dtype it leaves a missing cell as an actual
    float/NA rather than stringifying it, which crashes the caller's ', '.join(...) on a mixed
    list. pd.isna() catches every missing-value representation (None, float('nan'), pd.NA)."""
    return ["(blank)" if pd.isna(v) else str(v) for v in series.head(n).tolist()]


def _normalize_business_key(name: str) -> str:
    return name.strip().casefold()


def _find_remembered_mapping(raw, entity_name: str, remembered: dict) -> dict | None:
    """Returns the remembered {column: role} mapping for entity_name if one exists AND
    raw's column set exactly matches what it was recorded against (order-independent --
    the mapping is a column-name-keyed dict, so column order doesn't matter); None
    otherwise (no name yet, no memory for this name, or the columns changed), in which
    case the caller must fall back to the normal propose-button + LLM flow rather than
    reuse a mapping that might not even apply."""
    key = _normalize_business_key(entity_name)
    if not key:
        return None
    entry = remembered.get(key)
    if entry is None or entry["columns"] != frozenset(raw.df.columns):
        return None
    return entry["mapping"]


def _proposal_from_mapping(raw, mapping: dict) -> csv_ingest.MappingProposal:
    """Synthesizes a MappingProposal from a remembered mapping so it can seed the existing
    confirmation UI exactly like an LLM proposal would, without calling the LLM."""
    columns = [
        csv_ingest.ColumnProposal(
            csv_column=col,
            proposed_role=mapping.get(col, csv_statement.UNMAPPED_ROLE),
            rationale="Reused from the mapping you confirmed earlier for this business.",
        )
        for col in raw.df.columns
    ]
    return csv_ingest.MappingProposal(columns=columns, note=None)


def _resolve_mapping_proposal(raw, entity_name: str, remembered: dict) -> csv_ingest.MappingProposal | None:
    """None means no remembered mapping applies -- the caller must run the normal
    propose-button + LLM flow. Never calls propose_mapping itself."""
    mapping = _find_remembered_mapping(raw, entity_name, remembered)
    return None if mapping is None else _proposal_from_mapping(raw, mapping)


def _remember_mapping(remembered: dict, entity_name: str, raw, mapping: dict) -> None:
    """Records (or overwrites, if this business name was seen before this session) the
    confirmed mapping so a later same-session upload for the same business can reuse it."""
    remembered[_normalize_business_key(entity_name)] = {
        "display_name": entity_name.strip(),
        "columns": frozenset(raw.df.columns),
        "mapping": dict(mapping),
    }


def render_csv_upload_section() -> None:
    """
    Upload -> LLM-proposed mapping -> human confirmation -> normalization, per the approved
    CSV-upload design doc's session 1 scope. Ends at an inspectable normalized DataFrame,
    displayed via st.dataframe below -- nothing here is wired into the question/answer flow,
    run_agent, or any render_* function below (that's session 2's scope). The confirmation
    panel is deliberately as prominent as render_figure_check's warning banner (unresolved
    violations block the confirm button, not just a subtle note), matching this project's
    grounding-first UI philosophy.

    Mapping state lives in each column's own st.selectbox widget (key=f"csv_role_{column}"),
    not a separately-tracked session-state dict -- Streamlit already persists a keyed widget's
    value across reruns, so a second copy would just be a second, driftable source of truth.
    The mapping dict passed to validate_mapping/normalize is derived fresh from those widget
    keys on every render instead.
    """
    st.subheader("Analyze your own business (CSV upload)")
    st.caption(
        "Upload a quarterly- or annual-cadence financial CSV (revenue required; net income "
        "and balance-sheet figures recommended). An LLM proposes a column mapping below, "
        "which you confirm or correct before anything is normalized -- this file isn't used "
        "to answer questions yet."
    )

    for key in _CSV_STATE_KEYS:
        if key not in st.session_state:
            st.session_state[key] = None
    if "csv_remembered_mappings" not in st.session_state:
        st.session_state.csv_remembered_mappings = {}

    uploaded_file = st.file_uploader("Upload a CSV", type="csv", key="csv_uploader")
    if uploaded_file is None:
        return

    file_bytes = uploaded_file.getvalue()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    if st.session_state.csv_file_hash != file_hash:
        _reset_csv_state()
        st.session_state.csv_file_hash = file_hash
        raw, error = csv_ingest.parse_csv(file_bytes, uploaded_file.name)
        st.session_state.csv_raw = raw
        st.session_state.csv_parse_error = error

    if st.session_state.csv_parse_error:
        st.error(st.session_state.csv_parse_error)
        return

    raw = st.session_state.csv_raw
    st.success(f"Parsed {uploaded_file.name}: {len(raw.df)} rows, {len(raw.df.columns)} columns.")

    if st.session_state.csv_proposal is None:
        reused = _resolve_mapping_proposal(
            raw, st.session_state.get("csv_entity_name") or "", st.session_state.csv_remembered_mappings
        )
        if reused is not None:
            st.session_state.csv_proposal = reused
            st.session_state.csv_mapping_reused = True

    if st.session_state.csv_proposal is None:
        if st.button("Propose column mapping", key="csv_propose_button"):
            with st.spinner("Asking the model to propose a column mapping..."):
                proposal, error = _propose_mapping_or_error(raw)
            st.session_state.csv_proposal = proposal
            st.session_state.csv_proposal_error = error
        if st.session_state.csv_proposal_error:
            st.error(st.session_state.csv_proposal_error)
        if st.session_state.csv_proposal is None:
            return

    proposal = st.session_state.csv_proposal
    if proposal.note:
        st.warning(proposal.note)
    elif st.session_state.csv_mapping_reused:
        st.info(
            "Reusing the column mapping you confirmed earlier for this business -- "
            "review and confirm below."
        )

    st.markdown("**Confirm the column mapping**")
    entity_name = st.text_input(
        "Business name (used to label results, not sent anywhere)", key="csv_entity_name"
    )

    proposal_by_column = {c.csv_column: c for c in proposal.columns}
    role_options = [csv_statement.UNMAPPED_ROLE, csv_statement.PERIOD_ROLE, *csv_statement.ALL_CONCEPTS]
    mapping = {}
    for column in raw.df.columns:
        col_proposal = proposal_by_column.get(column)
        default_role = col_proposal.proposed_role if col_proposal else csv_statement.UNMAPPED_ROLE
        widget_key = f"csv_role_{column}"
        with st.container(border=True):
            left, right = st.columns([1, 1])
            left.markdown(f"**{column}**")
            sample_values = _sample_value_strings(raw.df[column])
            left.caption(f"e.g. {', '.join(sample_values)}")
            if col_proposal and col_proposal.rationale:
                left.caption(f"Model: {col_proposal.rationale}")
            selected = right.selectbox(
                "Role",
                role_options,
                index=role_options.index(default_role) if default_role in role_options else 0,
                key=widget_key,
            )
        mapping[column] = selected

    violations = csv_statement.validate_mapping(raw, mapping)
    if violations:
        with st.container(border=True):
            st.warning("Resolve these before confirming:")
            for v in violations:
                st.markdown(f"- {v}")

    entity_name_ok = bool(entity_name and entity_name.strip())
    if not entity_name_ok:
        st.info("Enter a business name above to enable confirmation.")

    confirm_clicked = st.button(
        "Confirm mapping and normalize",
        type="primary",
        disabled=bool(violations) or not entity_name_ok,
    )
    if confirm_clicked:
        df, errors, warnings = csv_statement.normalize(raw, mapping, entity_name.strip())
        st.session_state.csv_normalized = df
        st.session_state.csv_normalize_errors = errors
        st.session_state.csv_normalize_warnings = warnings
        if df is not None and not errors:
            _remember_mapping(st.session_state.csv_remembered_mappings, entity_name, raw, mapping)

    if st.session_state.csv_normalize_errors:
        with st.container(border=True):
            st.error("Could not normalize this file:")
            for e in st.session_state.csv_normalize_errors:
                st.markdown(f"- {e}")

    if confirm_clicked and st.session_state.csv_normalized is not None:
        csv_session.set_active_csv(st.session_state.csv_normalized)

    if st.session_state.csv_normalized is not None:
        normalized = st.session_state.csv_normalized
        st.success(
            f"Normalized {len(normalized)} period(s) for {normalized.attrs['entity_name']} "
            f"({normalized.attrs['csv_source']['cadence'] or 'single-period'} cadence). "
            f"You can now ask questions about {normalized.attrs['entity_name']} below."
        )
        if st.session_state.csv_normalize_warnings:
            # Same visual weight as the mapping-violations block above (a colored st.warning
            # banner, not a muted caption) -- a dropped row is data loss the human should
            # actually notice, not a footnote.
            with st.container(border=True):
                st.warning("Notes from normalization:")
                for w in st.session_state.csv_normalize_warnings:
                    st.markdown(f"- {w}")
        st.dataframe(normalized, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="FP&A Copilot", page_icon="📊", layout="wide")

    if "question_input" not in st.session_state:
        st.session_state.question_input = ""
    if "result" not in st.session_state:
        st.session_state.result = None
    if "run_error" not in st.session_state:
        st.session_state.run_error = None

    st.title("FP&A Copilot")
    st.caption("Ask a question about a public company's financials, sourced from SEC filings.")

    render_csv_upload_section()
    st.divider()

    st.markdown("**Try an example:**")
    cols = st.columns(2)
    for i, example in enumerate(EXAMPLE_QUESTIONS):
        if cols[i % 2].button(example, key=f"example_{i}"):
            st.session_state.question_input = example

    question = st.text_input("Ask a question", key="question_input")
    run_clicked = st.button("Run", type="primary")

    if run_clicked and question.strip():
        with st.spinner(f'Running the agent on: "{question.strip()}"... this can take 30+ seconds.'):
            st.session_state.result, st.session_state.run_error = _run_agent_or_error(question.strip())

    if st.session_state.run_error:
        st.error(st.session_state.run_error)

    if st.session_state.result is not None:
        result = st.session_state.result
        render_answer(result)
        render_figure_check(result)
        render_tool_calls(result)
        render_charts(result)

    st.divider()
    st.caption(
        "This is a research and educational tool. Nothing it produces is investment advice -- "
        "verify figures against original filings before making decisions."
    )


if __name__ == "__main__":
    main()

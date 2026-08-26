"""
Streamlit UI for the FP&A copilot (Phase 5). Pure display/orchestration around
`run_agent`: every number shown here is read verbatim from `run_agent`'s
return dict, never recomputed or reformatted -- the no-model-arithmetic rule
(see CLAUDE.md) extends to this layer as "no UI-layer arithmetic either."

Run with `streamlit run src/app/main.py` from the repo root.
"""

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

from src.agent.agent import run_agent  # noqa: E402 -- import after sys.path/load_dotenv setup above
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
    """Build up to MAX_CHARTS (ticker, concept/ratio) time series from this run's own
    get_financial_statement/get_ratios tool results -- never a fresh fetch. Plotting every
    concept/ratio unfiltered would be a wall of charts (a single statement call alone carries
    12 concepts), so candidates are ranked: anything named in the question or answer text
    first (in order of first mention), then revenue plus whatever ratios were actually
    requested via get_ratios' ratio_names argument -- the model already told us what it cared
    about when it made that call. Unranked candidates are dropped rather than padding the list.
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

        if tc["tool_name"] == "get_financial_statement":
            ticker = data.get("ticker", "?")
            for concept in ALL_CONCEPTS:
                points = {
                    row["period_end"]: row[concept]["value"]
                    for row in data.get("periods", [])
                    if isinstance(row.get(concept), dict) and row[concept].get("value") is not None
                }
                uid = (ticker, concept)
                if len(points) >= 2 and uid not in seen:
                    seen.add(uid)
                    candidates.append({"name": concept, "ticker": ticker, "points": points})

        elif tc["tool_name"] == "get_ratios":
            ticker = data.get("ticker", "?")
            for ratio_name, rows in data.get("ratios", {}).items():
                points = {r["period_end"]: r["value"] for r in rows if r.get("value") is not None}
                uid = (ticker, ratio_name)
                if len(points) >= 2 and uid not in seen:
                    seen.add(uid)
                    candidates.append({"name": ratio_name, "ticker": ticker, "points": points})
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
        company = get_company_name(c["ticker"]) or c["ticker"]
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
                m = fig["match"]
                caption = f"via `{m['tool_name']}` → `{m['json_path']}` = {m['matched_value']}"
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

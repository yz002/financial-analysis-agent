"""
Pure, offline-testable scoring for a completed run_agent() result against one eval question's
checkable criteria (see evals/questions.py, evals/ground_truth.py). Nothing here makes a network
or API call -- every function takes already-produced data (a run_agent result dict, a ground-truth
dict) and returns a plain-dict verdict, which is what makes this testable with synthetic data the
same way tests/test_agent.py and tests/test_guardrails.py test the rest of the agent layer.

Two design decisions carried over from the eval plan:

1. Figure matching is *tolerance*-based, not exact-match and not the stated-precision matching
   guardrails.check_figures uses -- that check answers "is this figure grounded in some tool
   result," this one answers "does the answer contain a figure close to the specific value I
   independently computed as correct." Dollar figures use a relative tolerance (default 0.5%,
   matching the threshold statements.py already uses for Q4-subtraction reconciliation);
   percentages/ratios use an absolute tolerance in percentage points (default 0.15pp, per the
   documented NVDA 74.99%-vs-75.00% rounding case in NOTES.md). The number extraction itself
   reuses guardrails._extract_candidates/_normalize rather than re-deriving the same $/%/scale-
   word parsing.

2. "Correct" isn't just "contains the right numbers." Each question below gets the checks that
   actually apply to it -- some need a numeric match, some (Ford's anomaly question, the
   AMD/NVDA comparison, the Costco forecast) are checked structurally/for self-consistency
   instead, because there's no single right number, or no way to know what "right" is in advance.
   A question "passes" only when every one of its checks passes.
"""

import json
import re

from src.agent.guardrails import _DASH_CHARS, _extract_candidates, _normalize, check_figures

_UP_LIKE = {"up", "accelerating", "increasing"}
_DOWN_LIKE = {"down", "decelerating", "decreasing"}

_MARGIN_UP_WORDS = ("increas", "improv", "expand", "widen", "rose", "rising", "grew", "higher", "strengthen")
_MARGIN_DOWN_WORDS = (
    "decreas", "declin", "compress", "narrow", "fell", "falling", "shrank", "lower", "weaken", "erod",
)
_MARGIN_FLAT_WORDS = ("flat", "stable", "steady", "unchanged", "little changed", "roughly the same")

_ACCEL_WORDS = ("accelerat", "speeding up", "picking up", "gaining pace", "quickening")
_DECEL_WORDS = ("decelerat", "slowing", "slowed", "cooling", "losing pace", "moderating")

_UNAVAILABLE_PHRASES = (
    "not available", "doesn't report", "does not report", "not reported", "unavailable",
    "isn't reported", "is not reported", "no data", "doesn't file", "does not file",
)


# --- generic, reusable checks ------------------------------------------------------------


def check_figure_present(text: str, expected_value: float | None, tolerance_pct: float = 0.5, name: str = "figure_present") -> dict:
    """Does `text` contain a dollar-like figure within `tolerance_pct`% (relative) of
    `expected_value`?"""
    if expected_value is None:
        return {"name": name, "passed": False, "detail": "no ground-truth value available to check against"}

    tol = abs(expected_value) * (tolerance_pct / 100)
    candidates = [_normalize(m)[0] for m in _extract_candidates(text)]
    match = next((v for v in candidates if abs(v - expected_value) <= tol), None)
    passed = match is not None
    detail = (
        f"found {match:,.2f} within {tolerance_pct}% of expected {expected_value:,.2f}"
        if passed
        else f"no figure within {tolerance_pct}% of expected {expected_value:,.2f} (candidates: {candidates})"
    )
    return {"name": name, "passed": passed, "detail": detail}


def check_percent_present(text: str, expected_fraction: float | None, tolerance_pp: float = 0.15, name: str = "percent_present") -> dict:
    """Does `text` contain a percentage within `tolerance_pp` percentage points (absolute) of
    `expected_fraction` (a 0-1 fraction, e.g. gross_margin's own convention)?"""
    if expected_fraction is None:
        return {"name": name, "passed": False, "detail": "no ground-truth value available to check against"}

    tol = tolerance_pp / 100
    candidates = [_normalize(m)[0] for m in _extract_candidates(text) if m.group("percent")]
    match = next((v for v in candidates if abs(v - expected_fraction) <= tol), None)
    passed = match is not None
    detail = (
        f"found {match * 100:.2f}% within {tolerance_pp}pp of expected {expected_fraction * 100:.2f}%"
        if passed
        else f"no percentage within {tolerance_pp}pp of expected {expected_fraction * 100:.2f}% "
        f"(candidates: {[f'{v * 100:.2f}%' for v in candidates]})"
    )
    return {"name": name, "passed": passed, "detail": detail}


def check_direction(text: str, expected: str | None, up_words, down_words, flat_words=(), name: str = "direction_matches") -> dict:
    """Does `text` state a direction consistent with `expected` ("up"/"accelerating"/
    "increasing" all mean up_words; "down"/"decelerating"/"decreasing" all mean down_words;
    "flat" means flat_words or a tie)? Counts, not presence -- a real multi-paragraph trend
    answer legitimately uses both vocabularies (e.g. noting a seasonal dip, or a different
    metric's direction, alongside the overall verdict), so "any down-word anywhere" would
    false-fail a correct, nuanced "up" answer that happens to mention one dip in passing. The
    side with more matches wins; an exact tie doesn't count as either direction."""
    if expected is None:
        return {"name": name, "passed": False, "detail": "no ground-truth direction available to check against"}

    lowered = text.lower()
    up_count = sum(lowered.count(w) for w in up_words)
    down_count = sum(lowered.count(w) for w in down_words)
    flat_count = sum(lowered.count(w) for w in flat_words) if flat_words else 0

    if expected in _UP_LIKE:
        passed = up_count > down_count
    elif expected in _DOWN_LIKE:
        passed = down_count > up_count
    elif expected == "flat":
        passed = flat_count > 0 or up_count == down_count
    else:
        passed = False
    return {
        "name": name,
        "passed": passed,
        "detail": f"expected={expected}, up_word_count={up_count}, down_word_count={down_count}, flat_word_count={flat_count}",
    }


def check_period_count_mentioned(text: str, n: int, name: str = "period_count_mentioned") -> dict:
    pattern = re.compile(rf"\b{n}[\s{_DASH_CHARS}]+(?:quarter|period)s?\b", re.IGNORECASE)
    passed = bool(pattern.search(text))
    return {"name": name, "passed": passed, "detail": f"looked for a '{n} quarters/periods' style mention"}


def check_states_unavailable(text: str, required_terms=(), name: str = "states_unavailable") -> dict:
    lowered = text.lower()
    mentions_terms = all(t.lower() in lowered for t in required_terms)
    states_unavailable = any(p in lowered for p in _UNAVAILABLE_PHRASES)
    passed = mentions_terms and states_unavailable
    return {
        "name": name,
        "passed": passed,
        "detail": f"mentions_required_terms={mentions_terms}, states_unavailable={states_unavailable}",
    }


def check_tool_called(tool_calls: list[dict], tool_name: str, ticker: str | None = None, name: str | None = None) -> dict:
    name = name or f"{tool_name}_called" + (f"_{ticker}" if ticker else "")
    for call in tool_calls:
        if call.get("is_error") or call.get("tool_name") != tool_name:
            continue
        if ticker is not None:
            call_ticker = (call.get("tool_input") or {}).get("ticker")
            if not call_ticker or call_ticker.upper() != ticker.upper():
                continue
        return {
            "name": name,
            "passed": True,
            "detail": f"found a successful {tool_name} call" + (f" for {ticker}" if ticker else ""),
        }
    return {
        "name": name,
        "passed": False,
        "detail": f"no successful {tool_name} call found" + (f" for {ticker}" if ticker else ""),
    }


def check_any_tool_called(tool_calls: list[dict], tool_names, ticker: str | None = None, name: str = "tool_called") -> dict:
    for tool_name in tool_names:
        result = check_tool_called(tool_calls, tool_name, ticker=ticker, name=name)
        if result["passed"]:
            return result
    return {
        "name": name,
        "passed": False,
        "detail": f"none of {list(tool_names)} were called successfully" + (f" for {ticker}" if ticker else ""),
    }


def check_mentions_all(text: str, groups, name: str = "mentions_all_entities") -> dict:
    """Does `text` mention every entity in `groups`? Each group is a tuple of acceptable
    spellings for one entity (e.g. ("Nvidia", "NVDA")) -- a live run showed the model
    consistently referring to a company by ticker only, never its name, which is a legitimate
    stylistic choice, not a missed mention."""
    lowered = text.lower()
    missing = [g for g in groups if not any(alt.lower() in lowered for alt in g)]
    return {
        "name": name,
        "passed": not missing,
        "detail": "all entities mentioned" if not missing else f"missing: {[g[0] for g in missing]}",
    }


def check_comparative_conclusion(text: str, name: str = "comparative_conclusion") -> dict:
    keywords = (
        "better positioned", "better-positioned", "stronger", "outperform", "ahead of",
        "advantage", "favorabl", "edge over", "leads", "trails", "lags",
    )
    lowered = text.lower()
    passed = any(k in lowered for k in keywords)
    return {"name": name, "passed": passed, "detail": "found a comparative-verdict phrase" if passed else "no comparative-verdict phrase found"}


# --- question-specific checks -------------------------------------------------------------


def _ford_trace_confirms_gross_profit_unavailable(tool_calls: list[dict]) -> bool:
    for call in tool_calls:
        if call.get("tool_name") != "get_financial_statement":
            continue
        try:
            payload = json.loads(call.get("tool_result") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("ticker") != "F":
            continue
        if "gross_profit" in (payload.get("concepts_unavailable") or []):
            return True
    return False


def check_ford_gross_profit_unavailable(run_result: dict) -> dict:
    text = run_result.get("final_answer") or ""
    tool_calls = run_result.get("tool_calls") or []
    text_check = check_states_unavailable(text, required_terms=("gross profit",), name="states_gross_profit_unavailable")
    trace_confirms = _ford_trace_confirms_gross_profit_unavailable(tool_calls)
    passed = text_check["passed"] and trace_confirms
    return {
        "name": "ford_gross_profit_unavailable",
        "passed": passed,
        "detail": f"{text_check['detail']}; trace_confirms_unavailable={trace_confirms}",
    }


def check_forecast_assumptions_or_refusal(run_result: dict, ticker: str, column: str) -> dict:
    tool_calls = run_result.get("tool_calls") or []
    matches = []
    for call in tool_calls:
        if call.get("is_error") or call.get("tool_name") != "forecast_metric":
            continue
        try:
            payload = json.loads(call.get("tool_result") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("ticker") == ticker and payload.get("column") == column:
            matches.append(payload)

    if not matches:
        return {
            "name": "forecast_assumptions_or_refusal",
            "passed": False,
            "detail": f"no successful forecast_metric call found for {ticker}/{column}",
        }

    payload = matches[-1]
    text = run_result.get("final_answer") or ""

    if payload.get("forecast_available"):
        projections = [p["value"] for p in payload.get("projections", []) if p.get("value") is not None]
        any_projection_traced = any(check_figure_present(text, v)["passed"] for v in projections) if projections else False
        assumption_kw = ("assumption", "trend", "growth rate", "seasonal", "method", "fit", "project")
        mentions_assumptions = any(k in text.lower() for k in assumption_kw)
        passed = any_projection_traced and mentions_assumptions
        detail = f"forecast_available=true; projection_stated={any_projection_traced}; mentions_assumptions={mentions_assumptions}"
    else:
        explanation_kw = (
            "can't project", "cannot project", "can't forecast", "cannot forecast", "unable to",
            "not enough", "isn't enough", "is not enough", "insufficient", "isn't available",
            "not available", "refus",
        )
        mentions_explanation = any(k in text.lower() for k in explanation_kw)
        passed = mentions_explanation
        detail = f"forecast_available=false; mentions_explanation={mentions_explanation}"

    return {"name": "forecast_assumptions_or_refusal", "passed": passed, "detail": detail}


# --- per-question scorers ------------------------------------------------------------------


def _score_nvda_lookup(run_result, ground_truth):
    text = run_result.get("final_answer") or ""
    tool_calls = run_result.get("tool_calls") or []
    return [
        check_any_tool_called(tool_calls, ("get_financial_statement", "get_ratios"), ticker="NVDA", name="nvda_data_pulled"),
        check_figure_present(text, ground_truth.get("revenue"), name="revenue_figure"),
        check_percent_present(text, ground_truth.get("gross_margin"), name="gross_margin_figure"),
    ]


def _score_msft_fcf(run_result, ground_truth):
    text = run_result.get("final_answer") or ""
    tool_calls = run_result.get("tool_calls") or []
    checks = [check_any_tool_called(tool_calls, ("get_ratios",), ticker="MSFT", name="msft_ratios_pulled")]
    fcf = ground_truth.get("free_cash_flow")
    if fcf is None:
        checks.append(check_states_unavailable(text, required_terms=("cash flow",), name="fcf_unavailable_explained"))
    else:
        checks.append(check_figure_present(text, fcf, name="fcf_figure"))
    return checks


def _score_aapl_trend(run_result, ground_truth):
    text = run_result.get("final_answer") or ""
    tool_calls = run_result.get("tool_calls") or []
    return [
        check_any_tool_called(tool_calls, ("get_ratios",), ticker="AAPL", name="aapl_margin_pulled"),
        check_direction(text, ground_truth.get("direction"), _MARGIN_UP_WORDS, _MARGIN_DOWN_WORDS, _MARGIN_FLAT_WORDS),
        check_period_count_mentioned(text, 8, name="mentions_8_quarters"),
    ]


def check_handles_ambiguous_company(text: str, name: str = "handles_ambiguous_company_question") -> dict:
    """README's growth-direction question names no company. A live run showed the model
    correctly declining to guess one, instead asking which company to analyze -- a legitimate,
    arguably preferable response to a genuinely ambiguous question, not a failure to answer. This
    check accepts that path as passing (distinct from silently fabricating a verdict with no
    ticker analyzed at all, which still fails)."""
    lowered = text.lower()
    keywords = ("which company", "which ticker", "which stock", "give me a ticker", "name a ticker", "specify a company", "let me know which")
    passed = any(k in lowered for k in keywords)
    return {
        "name": name,
        "passed": passed,
        "detail": "asked which company/ticker to analyze" if passed else "no ticker analyzed and no clarifying question asked",
    }


def _score_growth_direction(run_result, ground_truth):
    ground_truth = ground_truth or {}
    text = run_result.get("final_answer") or ""
    ticker = ground_truth.get("ticker")

    if not ticker:
        return [check_handles_ambiguous_company(text)]

    checks = [{"name": "identifies_a_company_with_real_data", "passed": True, "detail": f"ticker analyzed: {ticker}"}]
    if ground_truth.get("direction"):
        checks.append(check_direction(text, ground_truth["direction"], _ACCEL_WORDS, _DECEL_WORDS))
    return checks


def _score_ford_anomaly(run_result, ground_truth):
    return [check_ford_gross_profit_unavailable(run_result)]


def _score_amd_nvda_comparison(run_result, ground_truth):
    text = run_result.get("final_answer") or ""
    tool_calls = run_result.get("tool_calls") or []
    return [
        check_any_tool_called(tool_calls, ("get_ratios",), ticker="AMD", name="amd_margins_pulled"),
        check_any_tool_called(tool_calls, ("get_ratios",), ticker="NVDA", name="nvda_margins_pulled"),
        check_mentions_all(text, (("AMD",), ("Nvidia", "NVDA"))),
        check_comparative_conclusion(text),
    ]


def _score_costco_forecast(run_result, ground_truth):
    return [check_forecast_assumptions_or_refusal(run_result, "COST", "revenue")]


_SCORERS = {
    "nvda_revenue_margin": _score_nvda_lookup,
    "msft_fy2025_fcf": _score_msft_fcf,
    "aapl_operating_margin_trend": _score_aapl_trend,
    "revenue_growth_direction": _score_growth_direction,
    "ford_10q_anomalies": _score_ford_anomaly,
    "amd_nvda_comparison": _score_amd_nvda_comparison,
    "costco_revenue_forecast": _score_costco_forecast,
}


def score_question(question, run_result: dict, ground_truth: dict | None) -> dict:
    """Run `question`'s checks against `run_result` and return {question_id, passed, checks,
    grounding}. `passed` is True only if every check passes. `grounding` is
    guardrails.check_figures's report, pulled through unchanged -- a separate, orthogonal
    number from `passed`, not folded into it (see module docstring)."""
    scorer = _SCORERS[question.id]
    checks = scorer(run_result, ground_truth or {})
    figures = check_figures(run_result)
    return {
        "question_id": question.id,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "grounding": {
            "figures_checked": figures["figures_checked"],
            "figures_traced": figures["figures_traced"],
            "all_traced": figures["all_traced"],
        },
    }

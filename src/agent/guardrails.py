"""
Structural check for the agent layer's no-arithmetic constraint (see CLAUDE.md, NOTES.md).
`SYSTEM_PROMPT` in agent.py tells the model every number in its answer must come from a tool
result, but nothing enforced that until now. `check_figures` walks a completed `run_agent`
trace, extracts the numeric figures the model's prose actually states, and verifies each one
traces back to a value that appears somewhere in that run's tool results. It flags, it doesn't
block: the answer is never modified, only reported on.

Matching is precision-aware, not a flat tolerance: a figure is checked at the precision it was
literally stated at ("$90.0 billion" only needs a tool value that rounds to 90.0B; "$90.007
billion" needs one that rounds to 90.007B). A flat percentage tolerance would let a fabricated
figure near a real one pass silently, which defeats the point of the check.
"""

import json
import math
import re

_SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_SCALE_SUFFIXES = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

# Claude's own prose doesn't stick to ASCII for dates or negative numbers, and the two uses
# don't cleanly separate by character: U+2212 (true minus) is exclusively a sign, but U+2011
# (non-breaking hyphen) turns out to be used for *both* -- as a date/quarter/form-label separator
# ("10-Q", "FQ4'24", "2026-01-30", the original Phase 4 finding) and, confirmed by the Phase 6
# eval harness auditing 84 untraced figures across 21 real runs, as a negative sign too
# ("-2.26 trailing standard deviations"). 37 of those 84 (44%, by far the largest single cause)
# were genuinely grounded figures the checker had misread as positive because U+2011 wasn't in
# SIGN_CHARS -- see NOTES.md. Including it here is still safe despite U+2011 also marking word/
# date hyphenation, because _NUMBER_RE's sign group only matches immediately adjacent to a number
# (optionally through "$") -- a hyphen elsewhere in a compound word, nowhere near a digit, was
# never at risk and still isn't.
_DASH_CHARS = "\\-\u2010\u2011\u2012\u2013\u2014"
_SIGN_CHARS = "\\-\u2212\u2011"

# One combined pattern for every prose format the agent writes ($90.0 billion, $90,007 million,
# 57,006,000,000, 45.1%, 0.451, $1.2B), so overlapping interpretations across formats never need
# manual reconciliation. Sign is checked before the dollar sign since that's how a negative
# dollar figure is conventionally written ("-$90 million"), not "$-90 million". \b right before
# the mantissa means a digit glued onto a preceding letter (e.g. the "4" inside the field name
# `q4_subtraction_value` when the model quotes it in prose) is never matched -- \b only holds
# between a non-word and a word character, and "$"/"-"/whitespace are all non-word, but a
# letter immediately before a digit is not.
_NUMBER_RE = re.compile(
    rf"""
    (?P<sign>[{_SIGN_CHARS}])?
    (?P<dollar>\$)?
    \b
    (?P<mantissa>
        \d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?   # comma-grouped: 57,006,000,000 / 90,007
      | \d+\.\d+                          # plain decimal: 90.0 / 0.451 / 45.1
      | \d+                               # plain integer: 8 / 2025
    )
    (?P<suffix>[TBMK])?                   # attached, no space: 1.2B / 5.241T
    (?:[ \t]+(?P<word>thousand|million|billion|trillion)s?\b)?
    (?P<percent>\s*%)?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Spans matching any of these are never treated as financial figures needing grounding.
_FY_YEAR_RE = re.compile(r"\bFY\s?-?\d{2,4}\b", re.IGNORECASE)
# Fiscal-quarter labels ("FQ4'24") as well as plain ones ("Q3", "Q1 2025"); the year suffix may
# be glued on with an apostrophe (ASCII or the curly Unicode one) instead of a space.
_QUARTER_RE = re.compile(r"\bF?Q[1-4](?:['\u2019]?\s?(?:FY\s?)?\d{2,4})?\b", re.IGNORECASE)
_COUNT_WORDS = r"quarters?|months?|years?|periods?|companies|days?|weeks?"
# The separator between the count and its word is whitespace ("8 quarters") or a dash
# ("12-week quarters", "16-week Q4") -- confirmed by a live run_agent trace describing Costco's
# 52/53-week retail fiscal calendar (see NOTES.md); a whitespace-only separator missed the
# hyphenated form entirely.
_PERIOD_COUNT_RE = re.compile(
    rf"(?<!\$)\b\d{{1,3}}[\s{_DASH_CHARS}]+(?:{_COUNT_WORDS})\b"
    rf"|(?<!\$)\b(?:{_COUNT_WORDS})[\s{_DASH_CHARS}]+\d{{1,3}}\b",
    re.IGNORECASE,
)
# Uses _DASH_CHARS, not a literal ASCII "-", for the same reason _ISO_DATE_RE does: Claude's
# prose renders "10-Q"/"8-K" with a non-breaking hyphen (U+2011) rather than ASCII "-" (see the
# module-docstring comment on _DASH_CHARS above), and an ASCII-only pattern here silently fails
# to exclude the form label -- letting its leading digits ("10", "8") leak through as untraced,
# ungrounded-looking figures even though they're not financial figures at all. The optional
# trailing "s" (before the final \b) covers the plural ("10-Qs filed on the dates shown") --
# confirmed missing by the Phase 6 eval harness: \b requires a non-word character immediately
# after the form label, but "Qs" has no such break between "Q" and "s", so the match failed
# outright and "10" leaked through as an untraced-looking bare integer instead.
_FORM_LABEL_RE = re.compile(rf"\b(?:10[{_DASH_CHARS}][KQ](?:/A)?s?|8[{_DASH_CHARS}]Ks?)\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(rf"\b\d{{4}}[{_DASH_CHARS}]\d{{2}}[{_DASH_CHARS}]\d{{2}}\b")
_MONTH_NAMES = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
# Longer alternatives first (Sept before Sep) so the shorter one never wins a partial match that
# then fails to consume a following "t" -- Python's re backtracks through alternation regardless,
# but ordering makes the intent explicit rather than relying on that.
_MONTH_ABBREV = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
# Natural-language dates. The year is optional and the month/day separator may be a dash instead
# of whitespace -- confirmed by a live run_agent trace comparing retailers with non-calendar
# fiscal years: quarter-end dates were stated with the year established once elsewhere in the
# sentence ("Walmart: fiscal year ends Jan 31; quarters end Apr 30 / Jul 31 / Oct 31") and, later
# in the same answer, re-stated dash-joined with an abbreviated month ("Jul-31", "Apr-30") -- the
# latter form also has a second failure mode: _NUMBER_RE's sign character matches that dash,
# misreading "Jul-31" as the negative number "-31" unless the whole token is excluded first.
_NL_DATE_RE = re.compile(
    rf"\b(?:{_MONTH_NAMES}|{_MONTH_ABBREV})[{_DASH_CHARS}\s]\d{{1,2}}(?:st|nd|rd|th)?"
    rf"(?:,?\s+\d{{4}})?\b",
    re.IGNORECASE,
)
# A markdown numbered list/heading marker ("**4. Liquidity keeps eroding.**", "### 3. Trend") --
# the leading ordinal isn't a financial figure, though any real figure later on the same line
# still is and is left alone.
_LIST_ORDINAL_RE = re.compile(r"^[ \t]*#{0,3}[ \t]*\*{0,2}\d{1,2}\.[ \t]", re.MULTILINE)

_EXCLUSION_PATTERNS = (
    _FY_YEAR_RE,
    _QUARTER_RE,
    _PERIOD_COUNT_RE,
    _FORM_LABEL_RE,
    _ISO_DATE_RE,
    _NL_DATE_RE,
    _LIST_ORDINAL_RE,
)


def _excluded_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for pattern in _EXCLUSION_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _overlaps(span: tuple[int, int], excluded: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(s < end and start < e for s, e in excluded)


def _is_bare_year(m: re.Match) -> bool:
    """A plain 4-digit token with no $, %, scale word/suffix, comma, or decimal point."""
    if m.group("dollar") or m.group("percent") or m.group("word") or m.group("suffix"):
        return False
    mantissa = m.group("mantissa")
    if "," in mantissa or "." in mantissa or len(mantissa) != 4:
        return False
    return 1900 <= int(mantissa) <= 2099


def _extract_candidates(text: str) -> list[re.Match]:
    excluded = _excluded_spans(text)
    candidates = []
    for m in _NUMBER_RE.finditer(text):
        if _overlaps((m.start(), m.end()), excluded):
            continue
        # A bare suffix with no leading "$" ("3M", "10K") is dropped outright, not reinterpreted
        # as an unscaled number -- resolves the "3M-the-company" vs "3-million-dollars"
        # ambiguity without a ticker/company-name detector.
        if m.group("suffix") and not m.group("dollar"):
            continue
        if _is_bare_year(m):
            continue
        candidates.append(m)
    return candidates


def _format_label(m: re.Match) -> str:
    dollar = bool(m.group("dollar"))
    if m.group("percent"):
        return "percent"
    if m.group("word"):
        return "dollar_scale_word" if dollar else "scale_word"
    if m.group("suffix"):
        return "dollar_suffix"
    mantissa = m.group("mantissa")
    if "," in mantissa:
        return "dollar_comma_grouped" if dollar else "raw_comma_grouped"
    if "." in mantissa:
        return "dollar_decimal" if dollar else "bare_decimal"
    return "dollar_integer" if dollar else "plain_integer"


def _normalize(m: re.Match) -> tuple[float, int]:
    """Return (normalized_value, ndigits) -- ndigits is what `round()` needs to compare a
    candidate at exactly the precision it was stated at, per the module docstring."""
    mantissa = m.group("mantissa").replace(",", "")
    decimal_places = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0

    word = m.group("word")
    suffix = m.group("suffix")
    if word:
        divisor = _SCALE_WORDS[word.lower()]
    elif suffix:
        divisor = _SCALE_SUFFIXES[suffix.upper()]
    elif m.group("percent"):
        divisor = 0.01
    else:
        divisor = 1.0

    ndigits = decimal_places - round(math.log10(divisor))
    value = float(mantissa) * divisor
    if m.group("sign"):
        value = -value
    return value, ndigits


def _walk_json_numbers(obj, path: str = ""):
    """Yield (value, json_path) for every numeric leaf in a JSON-decoded structure. `None`
    leaves (ratios.py's uncomputable-value convention) are skipped, never treated as 0; JSON
    bool is excluded even though `bool` subclasses `int` in Python."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk_json_numbers(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _walk_json_numbers(value, f"{path}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield float(obj), path


def _collect_tool_values(tool_calls: list[dict]) -> list[dict]:
    collected = []
    for idx, call in enumerate(tool_calls):
        raw = call.get("tool_result")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for value, path in _walk_json_numbers(payload):
            collected.append(
                {
                    "value": value,
                    "json_path": path,
                    "tool_call_index": idx,
                    "tool_name": call.get("tool_name"),
                    "iteration": call.get("iteration"),
                }
            )
    return collected


def _find_match(value: float, ndigits: int, tool_values: list[dict]) -> dict | None:
    """First tool value (in tool_calls/JSON-traversal order) that rounds to the same value at
    the candidate's own stated precision. `abs_tol` guards only float-representation noise
    around the two round() calls -- it is not a behavioral tolerance."""
    target = round(value, ndigits)
    for entry in tool_values:
        if math.isclose(round(entry["value"], ndigits), target, abs_tol=1e-6):
            return entry
    return None


def check_figures(result: dict) -> dict:
    """Verify every numeric figure in result["final_answer"] traces back to a value present in
    result["tool_calls"][*]["tool_result"]. Flags, never modifies the answer. Returns a report:
    figures_checked/traced/untraced counts, all_traced, and a per-figure breakdown with the
    matched tool call and JSON path when traced."""
    final_answer = result.get("final_answer") or ""
    tool_calls = result.get("tool_calls") or []
    tool_values = _collect_tool_values(tool_calls)

    figures = []
    for m in _extract_candidates(final_answer):
        normalized_value, ndigits = _normalize(m)
        match = _find_match(normalized_value, ndigits, tool_values)
        figures.append(
            {
                "raw_text": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "normalized_value": normalized_value,
                "precision_ndigits": ndigits,
                "format": _format_label(m),
                "traced": match is not None,
                "match": (
                    {
                        "tool_call_index": match["tool_call_index"],
                        "tool_name": match["tool_name"],
                        "iteration": match["iteration"],
                        "json_path": match["json_path"],
                        "matched_value": match["value"],
                    }
                    if match is not None
                    else None
                ),
            }
        )

    traced = sum(1 for f in figures if f["traced"])
    total = len(figures)
    return {
        "figures_checked": total,
        "figures_traced": traced,
        "figures_untraced": total - traced,
        "all_traced": total == traced,
        "figures": figures,
    }

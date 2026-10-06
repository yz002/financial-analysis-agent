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

Precision-aware matching has its own failure mode, though: an undecorated small integer ("21")
normalizes to whole-number precision (ndigits=0), which is coarse enough that some unrelated
tool value can round to the same integer purely by chance. A real Phase 4 violation slipped
through this way -- the model wrote "Nvidia operates roughly 21 points higher on gross margin"
(a genuine no-arithmetic violation, differencing two get_ratios margin values itself) and
`check_figures` reported it as traced, because 21 happens to be the rounded value of an
unrelated get_market_data `valuation.price_to_sales` of 20.677921 (see NOTES.md). `_find_match`
now picks the *closest* candidate rather than merely the first one encountered, but that alone
doesn't close this gap -- 20.68 rounds to 21 exactly, so it's still the only candidate at that
precision, closest or not. Instead, a match whose only supporting precision is a bare or
dollar-prefixed whole number (no percent sign, scale suffix/word, or comma grouping -- see
`_WEAK_PRECISION_FORMATS`) is reported with `weak_match: True` and `traced: False` rather than
folded silently into "traced": the check's job stays flag-don't-block, but a coincidence-prone
trace is no longer indistinguishable from an exact one.
"""

import json
import math
import re

_SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_SCALE_SUFFIXES = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

# Claude's own prose doesn't stick to ASCII for dates, labels, or negative numbers, and these
# uses don't cleanly separate by character. Every dash-like character _DASH_CHARS excludes as a
# date/quarter/form-label separator ("10-Q", "FQ4'24", "2026-01-30") has also turned up glued
# directly onto a negative figure instead, found one code point at a time as each showed up in a
# real run: U+2011 non-breaking hyphen ("-2.26 trailing standard deviations", the Phase 6 eval
# harness's 84-untraced-figure audit) and U+2013 en dash ("-$11.557B", a live Streamlit run on
# Ford's derived Q4 2025 operating loss) were each added to SIGN_CHARS only after being caught
# live -- see NOTES.md. Rather than keep enumerating individual dash characters as new ones show
# up, _SIGN_CHARS is defined as a superset of _DASH_CHARS (plus U+2212, true minus -- see below):
# any character capable of standing in for a hyphen in Claude's prose is now also treated as
# capable of standing in for a minus sign, so a newly observed dash only has to be added in one
# place. U+2212 stays out of _DASH_CHARS in the other direction: it's a math symbol (Unicode
# category Sm), not a dash (category Pd), and nothing in this codebase has ever seen Claude use
# it to join a date or a form label, so adding it to the exclusion patterns built on _DASH_CHARS
# would just be unused surface area, not a real gap closed.
_DASH_CHARS = "\\-\u2010\u2011\u2012\u2013\u2014"
_SIGN_CHARS = _DASH_CHARS + "\u2212"

# One combined pattern for every prose format the agent writes ($90.0 billion, $90,007 million,
# 57,006,000,000, 45.1%, 0.451, $1.2B), so overlapping interpretations across formats never need
# manual reconciliation. Sign is checked before the dollar sign since that's how a negative
# dollar figure is conventionally written ("-$90 million"), not "$-90 million". \b right before
# the mantissa means a digit glued onto a preceding letter (e.g. the "4" inside the field name
# `q4_subtraction_value` when the model quotes it in prose) is never matched -- \b only holds
# between a non-word and a word character, and "$"/"-"/whitespace are all non-word, but a
# letter immediately before a digit is not.
#
# The sign group also carries a left-context check the other groups don't need: a dash-like
# character means "minus" only when nothing number-like precedes it. Making _SIGN_CHARS a full
# superset of _DASH_CHARS (above) was a real fix for the U+2013 case but, on its own, was too
# broad in a specific way -- re-scoring the Phase 6 eval harness's 21 saved traces against it
# surfaced 8 regressions where an en dash was a *range* separator, not a sign, sitting directly
# against the tail of the first number with no space ("$42.4B" then "-$99.9B" mean "$42.4B to
# $99.9B", not a negative $99.9B; likewise "0.842" then "-0.846" as consecutive quarters' debt-to-
# assets ratios). A genuine negative sign in Claude's prose is preceded by whitespace or
# punctuation ("was -$11.557B", "(-2.26 trailing..."); a range's second dash is preceded by the
# end of the first number itself -- a digit, "%", or a scale letter/word. `(?<![\w%])` right
# before the sign character class encodes exactly that distinction: it's wrapped with the sign
# group in its own optional unit (rather than placed once at the top of the whole pattern) so it
# constrains only the sign attempt, never the unsigned no-dollar-no-sign case that most matches
# take -- \b already handles that case's own left-context requirement independently, two
# positions later in the pattern. See NOTES.md for the before/after re-score.
_NUMBER_RE = re.compile(
    rf"""
    (?:(?<![\w%])(?P<sign>[{_SIGN_CHARS}]))?
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
# Slash-formatted dates ("6/30/25", "12/31/2025") -- a compact period-label form seen in a live
# run_agent trace restating each quarter's end date next to its label inside a markdown table
# ("Q4 2025 (12/31/25)"), documented as an untracked gap by the Phase 6 eval harness's audit (4
# of the 44 remaining untraced figures at the time) before this pattern existed. Both a 2-digit
# and 4-digit year are covered because a second live run of the *identical question* used the
# 4-digit form instead -- the model's date formatting varies run to run as much as its numeric
# formatting does (see NOTES.md). Without this, each date fragments into up to three separate
# untraced-looking bare integers (month, day, year); a single quarter-end date stated three times
# across a table, as in that live run, produced 9 such fragments from 3 real dates.
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2}(?:\d{2})?\b")

# The sign-by-word gap: Claude sometimes states a negative figure as an unsigned magnitude and
# carries the sign in the surrounding prose instead of a literal sign character -- "the (derived)
# ~$11B Q4 2025 charge" against a tool value of -$11,054,000,000, or "operating loss of $134M"
# against -134,000,000. Neither has anything for `_NUMBER_RE`'s `sign` group to match, so the
# unsigned candidate looks fabricated even though it's a real, correctly cited figure. This word
# list is intentionally short and financial-statement-specific, not a general sentiment lexicon --
# see `_negation_match` below for why a word landing nearby is only ever a secondary signal, never
# sufficient on its own, to accept the flip.
_NEGATION_WORD_RE = re.compile(
    r"\b(?:loss(?:es)?|charge[sd]?|deficits?|negative|shortfalls?|wrote\s+off|written\s+off"
    r"|write-off|down|fell|below)\b",
    re.IGNORECASE,
)
# How far (in characters) a negation word is allowed to sit from the candidate's own span before
# it counts as "surrounding" it, rather than describing some other number in the same paragraph --
# "still depressed by the (derived) ~$11B Q4 2025 charge" (9 chars after) and "operating loss of
# $134M" (4 chars before) are the closest observed cases; "2.38 trailing standard deviations below
# its own baseline" (30 chars after) is the farthest. 40 covers all three with room to spare
# without reaching into an unrelated neighboring sentence.
_NEGATION_WORD_WINDOW = 40

# A cell or range reference qualified by its sheet -- 'P&L (000s)'!B5, ‘Q3’!A3:G7, Sheet1!B5:D7 --
# including when it sits inside a backtick code span. Bare cell references ("B5", "A3:G7") never
# yield a candidate anyway (\b before the mantissa), but a sheet name can hold digits of its own
# ("P&L (000s)"), and those aren't figures. Only the reference itself is excluded, so a real
# figure next to it in the same code span ("`'P&L'!B5 = 1,310`") is still checked.
_CELL_REF = r"\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?"
_SHEET_REF_RE = re.compile(
    rf"(?:'[^'\n]{{1,100}}'|‘[^’\n]{{1,100}}’|\b[A-Za-z_][\w.]*)!{_CELL_REF}(?!\w)"
)

_EXCLUSION_PATTERNS = (
    _SHEET_REF_RE,
    _FY_YEAR_RE,
    _QUARTER_RE,
    _PERIOD_COUNT_RE,
    _FORM_LABEL_RE,
    _ISO_DATE_RE,
    _NL_DATE_RE,
    _SLASH_DATE_RE,
    _LIST_ORDINAL_RE,
)


def _usable_phrase(phrase) -> bool:
    """A known name is only excluded when it can't be mistaken for a figure: at least 3
    characters and at least one letter. A purely numeric sheet or business name ("100", "2024")
    is never excluded -- otherwise an invented figure equal to it would go unchecked."""
    return isinstance(phrase, str) and len(phrase.strip()) >= 3 and any(c.isalpha() for c in phrase)


def _excluded_spans(text: str, excluded_phrases=None) -> list[tuple[int, int]]:
    spans = []
    for pattern in _EXCLUSION_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    # Known names -- the statement's sheet, file and business names -- whose digits aren't
    # figures ("the P&L (000s) tab", "Studio 54"), matched literally on word boundaries.
    for phrase in excluded_phrases or ():
        if not _usable_phrase(phrase):
            continue
        for m in re.finditer(rf"(?<!\w){re.escape(phrase.strip())}(?!\w)", text):
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


# What separates the two ends of a range: a dash (any _DASH_CHARS) or "to", with optional spaces.
_RANGE_GAP_RE = re.compile(rf"\s*(?:[{_DASH_CHARS}]|to)\s*", re.IGNORECASE)


def _unit_of(m: re.Match) -> dict | None:
    if m.group("percent"):
        return {"percent": True}
    if m.group("word"):
        return {"word": m.group("word")}
    if m.group("suffix"):
        return {"suffix": m.group("suffix")}
    return None


def _range_units(text: str, matches: list[re.Match]) -> dict[int, dict]:
    """Units shared across a range, keyed by the receiving match's start offset. In "61–62%",
    "$1.2–1.3 million" or "1.2 to 1.3 million" the unit is written once, on the right end, but
    applies to both, so the left end inherits it. In "$1.2–1.3B" the currency is written once, on
    the left end, so the right end inherits the "$" (and isn't skipped as a bare suffix). Each
    end is still normalized and matched on its own, at its own stated precision -- only the
    unit is shared, never the value."""
    inherited: dict[int, dict] = {}
    for left, right in zip(matches, matches[1:]):
        if not _RANGE_GAP_RE.fullmatch(text[left.end() : right.start()]):
            continue
        right_unit = _unit_of(right)
        if _unit_of(left) is None and right_unit is not None:
            inherited[left.start()] = dict(right_unit)
        if left.group("dollar") and not right.group("dollar"):
            inherited.setdefault(right.start(), {})["dollar"] = True
    return inherited


def _extract_with_units(
    text: str, excluded_phrases=None
) -> tuple[list[re.Match], list[re.Match], dict[int, dict]]:
    """(candidates, skipped, inherited): every number-like token check_figures checks, the
    bare-suffix tokens it deliberately can't (see below), and the units a range shares between
    its ends (_range_units)."""
    excluded = _excluded_spans(text, excluded_phrases)
    matches = [m for m in _NUMBER_RE.finditer(text) if not _overlaps((m.start(), m.end()), excluded)]
    inherited = _range_units(text, matches)
    candidates, skipped = [], []
    for m in matches:
        unit = inherited.get(m.start(), {})
        # A bare suffix with no leading "$" ("3M", "10K") is not reinterpreted as an unscaled
        # number -- resolves the "3M-the-company" vs "3-million-dollars" ambiguity without a
        # ticker/company-name detector. It can't be checked either way, so it's reported in
        # figures_skipped: a figure written "1.25M" (no "$", as a statement with no stated
        # currency requires) would otherwise vanish from the check entirely. The right end of
        # a range whose "$" is written on the left end ("$1.2–1.3B") does carry a currency.
        if m.group("suffix") and not (m.group("dollar") or unit.get("dollar")):
            skipped.append(m)
            continue
        if not unit and _is_bare_year(m):
            continue
        candidates.append(m)
    return candidates, skipped, inherited


def _extract(text: str) -> tuple[list[re.Match], list[re.Match]]:
    """(candidates, skipped) -- see _extract_with_units."""
    candidates, skipped, _ = _extract_with_units(text)
    return candidates, skipped


def _extract_candidates(text: str) -> list[re.Match]:
    return _extract(text)[0]


def _format_label(m: re.Match, inherited: dict | None = None) -> str:
    """`inherited` is a unit shared from the other end of a range (_range_units)."""
    inherited = inherited or {}
    dollar = bool(m.group("dollar") or inherited.get("dollar"))
    if m.group("percent") or inherited.get("percent"):
        return "percent"
    if m.group("word") or inherited.get("word"):
        return "dollar_scale_word" if dollar else "scale_word"
    if m.group("suffix") or inherited.get("suffix"):
        return "dollar_suffix"
    mantissa = m.group("mantissa")
    if "," in mantissa:
        return "dollar_comma_grouped" if dollar else "raw_comma_grouped"
    if "." in mantissa:
        return "dollar_decimal" if dollar else "bare_decimal"
    return "dollar_integer" if dollar else "plain_integer"


def _normalize(m: re.Match, inherited: dict | None = None) -> tuple[float, int]:
    """Return (normalized_value, ndigits) -- ndigits is what `round()` needs to compare a
    candidate at exactly the precision it was stated at, per the module docstring. `inherited`
    is a unit shared from the other end of a range (_range_units); the precision is still this
    token's own decimal places, so each end of a range is checked on its own."""
    inherited = inherited or {}
    mantissa = m.group("mantissa").replace(",", "")
    decimal_places = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0

    word = m.group("word") or inherited.get("word")
    suffix = m.group("suffix") or inherited.get("suffix")
    if word:
        divisor = _SCALE_WORDS[word.lower()]
    elif suffix:
        divisor = _SCALE_SUFFIXES[suffix.upper()]
    elif m.group("percent") or inherited.get("percent"):
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


def _collect_from(tool_calls: list[dict], turn_id) -> list[dict]:
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
                    "turn_id": turn_id,
                }
            )
    return collected


def collect_tool_values(tool_calls: list[dict], prior_tool_calls: list[dict] | None = None) -> list[dict]:
    """Every numeric leaf in this run's tool results (`turn_id` None), then in the earlier turns'
    tool results replayed into the model's context (`prior_tool_calls`, each call carrying its
    own "turn_id"). A figure the model restates from an earlier turn's tool result is grounded
    in what it actually saw, so it has to trace. The caller passes exactly the replayed turns,
    never a wider window: every extra value is one more chance of a coincidental match.
    `tool_call_index` indexes into whichever list the value came from, so (turn_id,
    tool_call_index) together identify the call. Current-turn values come first, so on an exact
    tie the current turn's value wins."""
    collected = _collect_from(tool_calls, None)
    by_turn: dict = {}
    for call in prior_tool_calls or []:
        by_turn.setdefault(call.get("turn_id"), []).append(call)
    for turn_id, calls in by_turn.items():
        collected.extend(_collect_from(calls, turn_id))
    return collected


def _collect_tool_values(tool_calls: list[dict]) -> list[dict]:
    return collect_tool_values(tool_calls)


def find_all_matches(value: float, ndigits: int, tool_values: list[dict]) -> list[dict]:
    """Every tool value that rounds to `value` at the candidate's own stated precision,
    closest first (by absolute distance to the exact stated value; a stable sort, so equally
    close entries keep their collection order). `abs_tol` guards only float-representation noise
    around the two round() calls -- it's not a tolerance widening. Shared by check_figures
    (which keeps the closest) and src/agent/citations.py (which needs all of them, to show an
    ambiguous figure's every possible source rather than silently picking one)."""
    target = round(value, ndigits)
    hits = [
        entry
        for entry in tool_values
        if math.isclose(round(entry["value"], ndigits), target, abs_tol=1e-6)
    ]
    return sorted(hits, key=lambda entry: abs(entry["value"] - value))


def _find_match(value: float, ndigits: int, tool_values: list[dict]) -> dict | None:
    """The *closest* tool value -- by absolute distance to the candidate's own exact stated
    value, not merely the first one encountered in tool_calls/JSON-traversal order -- among
    every entry that rounds to the candidate's value at the candidate's own stated precision.
    Picking the closest of several qualifying candidates is a separate, deliberate step, not a
    tolerance widening. This alone doesn't prevent a coincidental match at coarse
    (whole-number) precision -- see the module docstring and `_WEAK_PRECISION_FORMATS` in
    `check_figures` for that."""
    hits = find_all_matches(value, ndigits, tool_values)
    return hits[0] if hits else None


def _negation_match(
    text: str, m: re.Match, value: float, ndigits: int, tool_values: list[dict]
) -> dict | None:
    """A fallback for a positive candidate that failed to trace as stated: retry against its
    negation, but only when both hold -- a negation word (see `_NEGATION_WORD_RE`) sits within
    `_NEGATION_WORD_WINDOW` characters of the candidate, *and* a tool value actually matches the
    negated magnitude at the candidate's own precision. Either check alone is too weak. The word
    alone isn't: "revenue was down 5%" has "down" sitting right against "5%", but if 5% is a
    genuinely positive growth rate (deceleration, not decline) there's no -5% tool value for it to
    match, so nothing here ever flips it -- the primary, unsigned check already traces it and this
    function is never even reached. The tool value alone isn't either: this codebase's numbers are
    small and can coincide by magnitude alone (e.g. two different ratios both landing near 0.05),
    so requiring an unrelated nearby word too keeps an accidental magnitude collision from being
    read as a sign flip. Only called for a candidate with no literal sign character already
    (`m.group("sign")` falsy) and a positive stated value -- a figure already written with an
    explicit sign, or as a genuine negative, has nothing to flip."""
    if not _NEGATION_WORD_RE.search(
        text[max(0, m.start() - _NEGATION_WORD_WINDOW) : m.end() + _NEGATION_WORD_WINDOW]
    ):
        return None
    return _find_match(-value, ndigits, tool_values)


def _is_parenthesized(text: str, m: re.Match) -> bool:
    """The token sits directly inside parentheses -- "($45,000)", accounting's negative
    notation -- with nothing else inside them."""
    return m.start() > 0 and text[m.start() - 1] == "(" and text[m.end() : m.end() + 1] == ")"


def _paren_negative_match(
    text: str, m: re.Match, value: float, ndigits: int, tool_values: list[dict]
) -> dict | None:
    """A fallback, never the primary reading, for a positive candidate that failed to trace as
    stated: if it's written in parentheses, retry it as a negative. Parentheses are far more
    often an aside ("gross margin (61.5%)") than an accounting negative, so a parenthesized
    figure is always checked as positive first and flipped only when that fails and a tool value
    actually matches the negative at the stated precision. Like _negation_match, only reached
    for an unsigned, positive candidate."""
    if not _is_parenthesized(text, m):
        return None
    return _find_match(-value, ndigits, tool_values)


# A bare or dollar-prefixed whole number ("21", "$5" -- `_format_label` returns "plain_integer"/
# "dollar_integer" only when there's no percent sign, scale suffix/word, or comma grouping) is
# the lowest-discriminating-power shape `_NUMBER_RE` produces: it states nothing beyond "nearest
# integer." Every other format narrows the collision space one of two ways -- a percent or
# decimal fraction adds precision digits past the integer (`ndigits > 0`), while a scale suffix/
# word or comma grouping pushes the *value itself* into a range (millions+) sparse enough that
# an unrelated tool value landing in the same rounding bucket by chance is implausible. Explicitly
# not attempted here: classifying which JSON field can plausibly ground which *kind* of claim
# (e.g. "a margin shouldn't trace to a valuation ratio") -- considered, but rejected as its own
# new source of false exclusions in a codebase whose tool results have no stable field taxonomy
# to hang that on. See the module docstring and NOTES.md for the live case this closes.
_WEAK_PRECISION_FORMATS = frozenset({"plain_integer", "dollar_integer"})


def check_figures(
    result: dict,
    prior_tool_calls: list[dict] | None = None,
    excluded_phrases: list[str] | None = None,
) -> dict:
    """Verify every numeric figure in result["final_answer"] traces back to a value present in
    result["tool_calls"][*]["tool_result"] -- or, when `prior_tool_calls` is given, in the
    earlier turns' tool results that were replayed into the model's context (see
    collect_tool_values; each such call carries its "turn_id", which the match reports). Flags,
    never modifies the answer. Returns a report: figures_checked/traced/untraced counts,
    all_traced, a per-figure breakdown with the matched tool call and JSON path when traced, and
    figures_skipped -- tokens like a bare "1.25M" that can't be checked at all (see _extract). A
    figure whose only matching evidence is a coincidence-prone whole-number match (see
    `_WEAK_PRECISION_FORMATS`) reports `traced: False` and `weak_match: True` -- its near-miss
    `match` is still included for transparency, but it doesn't count toward
    `figures_traced`/`all_traced`.

    `excluded_phrases` are known names (the statement's sheet, file and business names) whose
    digits aren't figures; see _excluded_spans for which ones are used."""
    final_answer = result.get("final_answer") or ""
    tool_calls = result.get("tool_calls") or []
    tool_values = collect_tool_values(tool_calls, prior_tool_calls)
    candidates, skipped, inherited = _extract_with_units(final_answer, excluded_phrases)

    figures = []
    for m in candidates:
        unit = inherited.get(m.start())
        normalized_value, ndigits = _normalize(m, unit)
        fmt = _format_label(m, unit)
        match = _find_match(normalized_value, ndigits, tool_values)
        sign_inferred = False
        if match is None and not m.group("sign") and normalized_value > 0:
            match = _paren_negative_match(final_answer, m, normalized_value, ndigits, tool_values)
            if match is None:
                match = _negation_match(final_answer, m, normalized_value, ndigits, tool_values)
            if match is not None:
                normalized_value = -normalized_value
                sign_inferred = True
        weak_match = match is not None and fmt in _WEAK_PRECISION_FORMATS
        figures.append(
            {
                "raw_text": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "normalized_value": normalized_value,
                "precision_ndigits": ndigits,
                "format": fmt,
                "traced": match is not None and not weak_match,
                "sign_inferred": sign_inferred,
                "weak_match": weak_match,
                "unit_from_range": bool(unit and (unit.keys() - {"dollar"})),
                "match": (
                    {
                        "tool_call_index": match["tool_call_index"],
                        "tool_name": match["tool_name"],
                        "iteration": match["iteration"],
                        "json_path": match["json_path"],
                        "matched_value": match["value"],
                        "turn_id": match["turn_id"],
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
        "figures_skipped": [
            {"raw_text": m.group(0), "start": m.start(), "end": m.end(), "reason": "bare_scale_suffix"}
            for m in skipped
        ],
    }

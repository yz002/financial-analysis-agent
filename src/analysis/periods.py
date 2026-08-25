"""
Calendar-date period lookup, replacing positional shift(n)/rolling(n) row
offsets used elsewhere in this package.

A positional offset ("row i-1", "row i-4") silently means the wrong thing
the moment a quarter is missing from a statement's period_end sequence --
which happens (see NOTES.md): a fiscal-calendar stub period can be
classified as period_length="other" by concepts.py and dropped entirely
from get_statement's quarterly output, so "1 row back" ends up comparing
across a 2-quarter gap without error. find_prior_period looks up "1
quarter back"/"1 year back" by the *calendar* date it should land on
instead, within a tolerance that accounts for real fiscal-quarter length
variation (a 52/53-week retail calendar, a reclassified long opening
quarter), and refuses -- returns None plus an explicit reason, never the
nearest available row -- when no row actually falls in that window. Only
quarters_back=1 and quarters_back=4 are supported: the only two offsets
used anywhere in this codebase (_growth's QoQ/YoY, growth_anomalies' and
the detect_anomalies agent tool's documented lag of 1 or 4), and the only
two with a tolerance justified by this codebase's own day-span constants
(see the constants below). Any other offset raises rather than guessing
at an untested tolerance.

_ttm and trailing_stats need several *contiguous* prior quarters, not one
offset -- they're built on top of this module by chaining quarters_back=1
hops (see their own docstrings in ratios.py/trends.py), not by calling
find_prior_period with quarters_back=2, 3, etc.
"""

import pandas as pd

# A real filed quarterly period spans roughly 80-125 days (concepts.py's
# _QUARTERLY_DAYS_MIN/_MAX=80/100, plus the reclassified long-opening-quarter
# ceiling of 125 for e.g. Kroger's ~111-day Q1) against a ~91-day nominal
# 3-calendar-month target -- worst-case drift ~35 days, rounded up with
# slack. Stays well under half of one quarter's ~91-day typical spacing (46
# days), so an adjacent real quarter can never be mistaken for the target one.
_QUARTER_STEP_TOLERANCE_DAYS = 40

# A same-fiscal-quarter year-ago comparison on a 52/53-week retail calendar
# can drift a business week or two from an exact 12-calendar-month target (an
# extra 53rd week shifts every later quarter's start ~7 days until the
# calendar re-syncs). 21 days covers a few weeks of that drift and is still
# well under half of one quarter's ~91-day spacing.
_YEAR_STEP_TOLERANCE_DAYS = 21

_TOLERANCE_DAYS = {1: _QUARTER_STEP_TOLERANCE_DAYS, 4: _YEAR_STEP_TOLERANCE_DAYS}


def find_prior_period(
    period_ends: pd.Series, i: int, quarters_back: int
) -> tuple[int | None, str | None]:
    """
    Find the row before position `i` whose period_end falls within
    tolerance of the calendar date `quarters_back` quarters before
    period_ends.iloc[i]. Returns (row_index, None) on a match, or
    (None, reason) if nothing qualifies -- never the nearest available
    row. `period_ends` must already be sorted ascending with a 0-based
    positional index (get_statement's output already is); violating that,
    or passing a `quarters_back` other than 1 or 4, raises ValueError as
    a caller configuration error rather than a data fact.

    Reasons:
      "insufficient_history" -- the target date predates the earliest
        row in `period_ends`; there just isn't `quarters_back` quarters
        of history yet.
      "gap_no_prior_period" -- the target date falls within the covered
        range, but no row's period_end lands within tolerance of it --
        the quarter that should be there is missing.
    """
    if quarters_back not in _TOLERANCE_DAYS:
        raise ValueError(f"quarters_back must be 1 or 4, got {quarters_back!r}")
    if not period_ends.is_monotonic_increasing:
        raise ValueError("period_ends must be sorted ascending")
    if i <= 0:
        return None, "insufficient_history"

    tolerance = pd.Timedelta(days=_TOLERANCE_DAYS[quarters_back])
    target = period_ends.iloc[i] - pd.DateOffset(months=3 * quarters_back)

    prior = period_ends.iloc[:i]
    diffs = (prior - target).abs()
    best_pos = diffs.values.argmin()

    if diffs.iloc[best_pos] > tolerance:
        if target < period_ends.iloc[0]:
            return None, "insufficient_history"
        return None, "gap_no_prior_period"
    return prior.index[best_pos], None


def prior_period_series(period_ends: pd.Series, quarters_back: int) -> pd.DataFrame:
    """
    find_prior_period run for every row of `period_ends`. Returns a
    DataFrame aligned to period_ends.index with columns:
      prior_index -- Int64 (nullable), the matched row's positional
        index, or NA if refused.
      reason -- object, None on a match, else the refusal reason.
    """
    prior_indices: list[int | None] = []
    reasons: list[str | None] = []
    for i in range(len(period_ends)):
        idx, reason = find_prior_period(period_ends, i, quarters_back)
        prior_indices.append(idx)
        reasons.append(reason)
    return pd.DataFrame(
        {
            "prior_index": pd.array(prior_indices, dtype="Int64"),
            "reason": pd.Series(reasons, index=period_ends.index, dtype=object),
        },
        index=period_ends.index,
    )


def chained_trailing_window(
    period_ends: pd.Series, i: int, hops: int
) -> tuple[list[int] | None, str | None]:
    """
    Walk back `hops` quarters from row `i`, one contiguous quarter at a
    time (each hop is a quarters_back=1 find_prior_period call from the
    previously found row, not an independent multi-quarter target -- see
    the module docstring for why). Returns (indices, None) on success,
    with `indices` the `hops` row indices in chronological order (oldest
    first); or (None, reason) if any hop can't find a contiguous prior
    quarter -- a gap anywhere in the window invalidates the whole trailing
    computation rather than silently spanning it. `reason` is whichever of
    find_prior_period's reasons the failing hop returned
    ("insufficient_history" or "gap_no_prior_period").
    """
    indices: list[int] = []
    j = i
    for _ in range(hops):
        idx, reason = find_prior_period(period_ends, j, 1)
        if idx is None:
            return None, reason
        indices.append(idx)
        j = idx
    indices.reverse()
    return indices, None

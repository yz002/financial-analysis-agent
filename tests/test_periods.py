"""
Tests for src/analysis/periods.py, fully offline: pure functions of a
period_end Series, so every case here builds its own synthetic dates
rather than touching EdgarClient or statements.get_statement.
"""

import pandas as pd
import pytest

from src.analysis.periods import chained_trailing_window, find_prior_period, prior_period_series


def _period_ends(start="2020-03-31", step_days=91, n=8):
    return pd.Series([pd.Timestamp(start) + pd.Timedelta(days=step_days * i) for i in range(n)])


def _period_ends_with_gap(start="2020-03-31", step_days=91, n=8, gap_at=4, extra_days=91):
    """Same as _period_ends, but widens the step right before index `gap_at` by `extra_days`."""
    dates = []
    current = pd.Timestamp(start)
    for i in range(n):
        if i == gap_at:
            current = current + pd.Timedelta(days=extra_days)
        dates.append(current)
        current = current + pd.Timedelta(days=step_days)
    return pd.Series(dates)


# --- find_prior_period: normal contiguous cases ------------------------------


def test_finds_one_quarter_back_in_continuous_series():
    period_ends = _period_ends()
    idx, reason = find_prior_period(period_ends, 4, quarters_back=1)
    assert (idx, reason) == (3, None)


def test_finds_one_year_back_in_continuous_series():
    period_ends = _period_ends()
    idx, reason = find_prior_period(period_ends, 4, quarters_back=4)
    assert (idx, reason) == (0, None)


# --- refusal: insufficient history -------------------------------------------


def test_first_row_is_insufficient_history():
    period_ends = _period_ends()
    idx, reason = find_prior_period(period_ends, 0, quarters_back=1)
    assert (idx, reason) == (None, "insufficient_history")


def test_yoy_lookup_before_a_years_history_is_insufficient_history():
    period_ends = _period_ends(n=3)
    idx, reason = find_prior_period(period_ends, 2, quarters_back=4)
    assert (idx, reason) == (None, "insufficient_history")


# --- refusal: a real gap, not a fallback to the nearest row -------------------


def test_missing_quarter_is_refused_not_matched_to_nearest():
    # Row 4's real prior quarter (row 3) was skipped entirely -- the gap
    # doubles the step right before index 4, so nothing lands near the
    # ~91-day-back target.
    period_ends = _period_ends_with_gap(gap_at=4, extra_days=91)
    idx, reason = find_prior_period(period_ends, 4, quarters_back=1)
    assert (idx, reason) == (None, "gap_no_prior_period")


def test_row_after_the_gap_recovers_normally():
    # Row 5's prior quarter (row 4) is present and normally spaced -- only
    # the single row spanning the gap should be refused.
    period_ends = _period_ends_with_gap(gap_at=4, extra_days=91)
    idx, reason = find_prior_period(period_ends, 5, quarters_back=1)
    assert (idx, reason) == (4, None)


# --- tolerance boundary: long real quarters vs. genuine gaps -----------------


def test_kroger_style_long_quarter_still_matches():
    # A ~111-day quarter (Kroger's real long Q1) is 20 days off the ~91-day
    # nominal target -- well within _QUARTER_STEP_TOLERANCE_DAYS=40.
    period_ends = _period_ends_with_gap(gap_at=4, extra_days=20)
    idx, reason = find_prior_period(period_ends, 4, quarters_back=1)
    assert (idx, reason) == (3, None)


def test_doubled_gap_does_not_match():
    # A full extra quarter's worth of gap (91 extra days) pushes the true
    # prior row ~180 days back -- well past tolerance -- so it must refuse
    # rather than silently pairing row 4 with row 3 across the gap.
    period_ends = _period_ends_with_gap(gap_at=4, extra_days=91)
    idx, reason = find_prior_period(period_ends, 4, quarters_back=1)
    assert idx is None
    assert reason == "gap_no_prior_period"


# --- malformed input (raises) -------------------------------------------------


def test_unsupported_quarters_back_raises():
    period_ends = _period_ends()
    with pytest.raises(ValueError):
        find_prior_period(period_ends, 4, quarters_back=2)


def test_non_monotonic_period_ends_raises():
    period_ends = pd.Series(
        [pd.Timestamp("2020-06-30"), pd.Timestamp("2020-03-31"), pd.Timestamp("2020-09-30")]
    )
    with pytest.raises(ValueError):
        find_prior_period(period_ends, 2, quarters_back=1)


# --- prior_period_series -------------------------------------------------


def test_prior_period_series_matches_row_by_row():
    period_ends = _period_ends(n=5)
    result = prior_period_series(period_ends, quarters_back=1)
    assert list(result["prior_index"]) == [pd.NA, 0, 1, 2, 3]
    assert result["reason"].iloc[0] == "insufficient_history"
    assert result["reason"].iloc[1:].isna().all()


def test_prior_period_series_flags_gap():
    period_ends = _period_ends_with_gap(gap_at=4, extra_days=91)
    result = prior_period_series(period_ends, quarters_back=1)
    assert pd.isna(result["prior_index"].iloc[4])
    assert result["reason"].iloc[4] == "gap_no_prior_period"


# --- chained_trailing_window -------------------------------------------------


def test_chained_trailing_window_walks_back_contiguous_quarters():
    period_ends = _period_ends(n=8)
    indices, reason = chained_trailing_window(period_ends, 7, hops=3)
    assert indices == [4, 5, 6]
    assert reason is None


def test_chained_trailing_window_aborts_on_gap_anywhere_in_window():
    period_ends = _period_ends_with_gap(n=8, gap_at=4, extra_days=91)
    # Row 7's trailing 3-quarter window (rows 4,5,6) doesn't span the gap
    # (the gap is between rows 3 and 4) -- but a window reaching back to
    # include row 3 or earlier must abort.
    indices, reason = chained_trailing_window(period_ends, 7, hops=4)
    assert indices is None
    assert reason == "gap_no_prior_period"

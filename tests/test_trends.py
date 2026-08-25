import pandas as pd
import pytest

from src.analysis.trends import detect_anomalies, growth_anomalies, trailing_stats


def _quarterly_index(n, start="2020-03-31", step_days=91):
    """A sorted-ascending DatetimeIndex of n quarterly period_end dates -- the
    shape trailing_stats/detect_anomalies now require of series.index."""
    return pd.DatetimeIndex([pd.Timestamp(start) + pd.Timedelta(days=step_days * i) for i in range(n)])


def _quarterly_index_with_gap(n, start="2020-03-31", step_days=91, gap_at=None, extra_days=91):
    """Same as _quarterly_index, but widens the step right before index
    `gap_at` (default: the midpoint) by `extra_days`."""
    if gap_at is None:
        gap_at = n // 2
    dates = []
    current = pd.Timestamp(start)
    for i in range(n):
        if i == gap_at:
            current = current + pd.Timedelta(days=extra_days)
        dates.append(current)
        current = current + pd.Timedelta(days=step_days)
    return pd.DatetimeIndex(dates)


def test_trailing_stats_raises_for_short_series():
    series = pd.Series([1.0, 2.0, 3.0], index=_quarterly_index(3))
    with pytest.raises(ValueError):
        trailing_stats(series, window=3)


def test_trailing_stats_excludes_point_itself():
    # A point far outside a stable series should NOT pull its own baseline
    # toward itself -- the baseline for point i must come only from points
    # before i. Construct a series where the last point is a huge outlier;
    # if it leaked into its own baseline, trailing_mean would be dragged
    # toward it and the deviation would be understated.
    stable = [10.0] * 9
    series = pd.Series(stable + [1000.0], index=_quarterly_index(10))
    stats = trailing_stats(series, window=8)
    last = stats.iloc[-1]
    assert last["trailing_mean"] == pytest.approx(10.0)
    assert last["trailing_std"] == pytest.approx(0.0)
    assert not last["trailing_gap"]


def test_detect_anomalies_flags_known_spike():
    stable = [10.0] * 10
    series = pd.Series(stable + [10.0, 10.0, 100.0], index=_quarterly_index(13))
    result = detect_anomalies(series, window=8, threshold=2.0)
    assert result["is_anomaly"].iloc[-1]
    assert not result["is_anomaly"].iloc[:-1].any()
    assert result["deviation_abs"].iloc[-1] == pytest.approx(90.0)


def test_detect_anomalies_no_flags_on_flat_series():
    series = pd.Series([10.0] * 15, index=_quarterly_index(15))
    result = detect_anomalies(series, window=8, threshold=2.0)
    assert not result["is_anomaly"].any()


def test_trailing_stats_flags_gap_vs_insufficient_history():
    # A genuinely gapped series: the row whose trailing window would have
    # to span the missing quarter gets trailing_gap=True, not just NaN --
    # distinguishing it from ordinary leading rows that are merely short on
    # history (trailing_gap=False there, even though they're also NaN).
    values = [10.0] * 12
    gapped_index = _quarterly_index_with_gap(12, gap_at=4, extra_days=91)
    stats = trailing_stats(pd.Series(values, index=gapped_index), window=4)

    # Leading rows: not enough history yet, not a gap.
    assert stats["trailing_gap"].iloc[:4].eq(False).all()
    assert stats["trailing_mean"].iloc[:4].isna().all()

    # A row whose 4-quarter trailing window reaches back across the gap
    # (row 4 is here) is a real gap, not just insufficient history.
    assert stats["trailing_gap"].iloc[4]
    assert pd.isna(stats["trailing_mean"].iloc[4])

    # Once enough real quarters have accumulated after the gap, later rows
    # recover a normal, gap-free trailing window.
    assert not stats["trailing_gap"].iloc[8]
    assert stats["trailing_mean"].iloc[8] == pytest.approx(10.0)


def test_growth_anomalies_does_not_silently_compact_across_gap():
    # Revenue grows steadily except for a missing quarter partway through.
    # The old dropna()-then-rolling behavior would have silently compacted
    # the gap away and compared across it; the fix must instead leave a NaN
    # growth rate in place for the row spanning the gap, so it's excluded
    # from anomaly detection rather than mis-flagged (or mis-cleared).
    n = 14
    values = [100.0 * (1.05**i) for i in range(n)]
    gapped_index = _quarterly_index_with_gap(n, gap_at=6, extra_days=91)
    stmt = pd.DataFrame({"period_end": gapped_index, "revenue": values})

    result = growth_anomalies(stmt, "revenue", window=4, threshold=2.0, lag=1)
    row = result.loc[gapped_index[6]]
    assert pd.isna(row["value"])
    assert not row["is_anomaly"]


def test_nvda_level_zscore_overflags_vs_growth(nvda_quarterly):
    # The core design problem this module exists to solve: NVDA's revenue
    # nearly doubles across several consecutive real quarters (AI-boom
    # growth from 2023 onward -- confirmed in the cached data). A naive
    # level-based z-score flags most of that run as anomalous, which is
    # true but useless -- it can't tell "consistent explosive growth" from
    # "something broke". growth_anomalies, run on the same data with the
    # same window/threshold, should flag far fewer periods: only where the
    # growth *rate itself* breaks from its own recent trend.
    level_series = nvda_quarterly.set_index("period_end")["revenue"]
    level_result = detect_anomalies(level_series, window=8, threshold=2.0)
    growth_result = growth_anomalies(nvda_quarterly, "revenue", window=8, threshold=2.0)

    level_flagged = level_result["is_anomaly"].sum()
    growth_flagged = growth_result["is_anomaly"].sum()

    assert level_flagged > 15  # over-flags a large fraction of NVDA's real growth history
    assert growth_flagged < level_flagged / 2  # markedly fewer, not just "somewhat fewer"

    # The genuine regime-change quarter (growth rate itself jumping, not
    # just the level continuing to rise) should still be caught.
    assert growth_result.loc[pd.Timestamp("2023-07-30"), "is_anomaly"]

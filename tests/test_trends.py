import pandas as pd
import pytest

from src.analysis.trends import detect_anomalies, growth_anomalies, trailing_stats


def test_trailing_stats_raises_for_short_series():
    series = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        trailing_stats(series, window=3)


def test_trailing_stats_excludes_point_itself():
    # A point far outside a stable series should NOT pull its own baseline
    # toward itself -- the baseline for point i must come only from points
    # before i. Construct a series where the last point is a huge outlier;
    # if it leaked into its own baseline, trailing_mean would be dragged
    # toward it and the deviation would be understated.
    stable = [10.0] * 9
    series = pd.Series(stable + [1000.0])
    stats = trailing_stats(series, window=8)
    last = stats.iloc[-1]
    assert last["trailing_mean"] == pytest.approx(10.0)
    assert last["trailing_std"] == pytest.approx(0.0)


def test_detect_anomalies_flags_known_spike():
    stable = [10.0] * 10
    series = pd.Series(stable + [10.0, 10.0, 100.0])
    result = detect_anomalies(series, window=8, threshold=2.0)
    assert result["is_anomaly"].iloc[-1]
    assert not result["is_anomaly"].iloc[:-1].any()
    assert result["deviation_abs"].iloc[-1] == pytest.approx(90.0)


def test_detect_anomalies_no_flags_on_flat_series():
    series = pd.Series([10.0] * 15)
    result = detect_anomalies(series, window=8, threshold=2.0)
    assert not result["is_anomaly"].any()


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

"""
Trailing statistics and anomaly detection over a time series.

This module answers two different questions, and it matters which one a
caller is actually asking:

  1. "Is this an unusual *level*?" -- detect_anomalies, run directly on a
     raw series (e.g. revenue).
  2. "Is this period's *growth* unusual relative to its own recent
     trend?" -- growth_anomalies, run on period-over-period growth rates.

detect_anomalies is a generic, domain-agnostic primitive: it doesn't know
or care what the series represents. That matters concretely for a name
like NVIDIA, whose real, sustained revenue growth is itself statistically
extreme -- consecutive quarters nearly doubling is normal for NVDA in a
way it would never be for a mature company. Running detect_anomalies on
raw NVDA revenue levels flags nearly every recent quarter, which is true
but useless: it can't distinguish "this company grows explosively, as
usual" from "something just broke." growth_anomalies exists specifically
for this: it runs the same primitive on period-over-period growth rates
instead of levels, so a *consistent* growth rate -- however large --
sits close to its own trailing baseline and isn't flagged. Only a break
from that trend (an acceleration, deceleration, or reversal) is. For a
second-derivative view (is growth itself accelerating unexpectedly),
call .pct_change() a second time before passing to detect_anomalies.
"""

import pandas as pd


def trailing_stats(series: pd.Series, window: int) -> pd.DataFrame:
    """
    Rolling mean/std of `series`, computed from the `window` points
    immediately preceding each point -- never including the point itself,
    to avoid look-ahead/self-inclusion bias. Returns a DataFrame aligned to
    `series.index` with columns value, trailing_mean, trailing_std; the
    first `window` rows have NaN trailing_mean/trailing_std (not enough
    prior history yet).

    Raises ValueError if len(series) <= window -- i.e. no point in the
    series could ever get a full trailing baseline. This is a caller
    configuration error (an unsatisfiable window for the given history), so
    it raises loudly rather than silently returning an all-NaN frame that
    would look like "no anomalies" to a careless caller.
    """
    if len(series) <= window:
        raise ValueError(
            f"series has {len(series)} points; need more than window={window} "
            "to compute a trailing baseline that excludes the point itself"
        )
    shifted = series.shift(1)
    return pd.DataFrame(
        {
            "value": series,
            "trailing_mean": shifted.rolling(window).mean(),
            "trailing_std": shifted.rolling(window).std(),
        }
    )


def detect_anomalies(series: pd.Series, window: int, threshold: float) -> pd.DataFrame:
    """
    Flag points in `series` more than `threshold` trailing standard
    deviations from their trailing baseline (see trailing_stats -- the
    baseline for point i uses only points before i). Domain-agnostic: pass
    raw levels, period-over-period growth rates, or any numeric series;
    this function has no notion of what the series represents. See the
    module docstring for why a level-based call is the wrong tool for a
    metric with a real sustained growth trend (growth_anomalies is).

    `series` must already be sorted ascending by whatever it's indexed on
    (typically period_end) -- this function doesn't sort it.

    Returns the full period-aligned DataFrame: value, trailing_mean,
    trailing_std, deviation_abs, deviation_std, is_anomaly. Filter to just
    the flagged periods with result[result.is_anomaly].
    """
    stats = trailing_stats(series, window)
    deviation_abs = stats["value"] - stats["trailing_mean"]
    deviation_std = deviation_abs / stats["trailing_std"]

    # A zero trailing_std means the baseline window was perfectly constant.
    # Any nonzero deviation from a perfectly constant history is itself
    # anomalous -- gating on trailing_std > 0 (as if 0 meant "undefined,
    # skip") would instead suppress every real spike off a flat baseline,
    # which is the opposite of what an anomaly detector should do.
    zero_std_anomaly = (stats["trailing_std"] == 0) & (deviation_abs != 0)
    normal_anomaly = (deviation_std.abs() > threshold) & (stats["trailing_std"] > 0)
    is_anomaly = (zero_std_anomaly | normal_anomaly).fillna(False)
    return pd.DataFrame(
        {
            "value": stats["value"],
            "trailing_mean": stats["trailing_mean"],
            "trailing_std": stats["trailing_std"],
            "deviation_abs": deviation_abs,
            "deviation_std": deviation_std,
            "is_anomaly": is_anomaly,
        }
    )


def growth_anomalies(
    stmt: pd.DataFrame,
    column: str,
    window: int = 8,
    threshold: float = 2.0,
    lag: int = 1,
) -> pd.DataFrame:
    """
    Recommended entry point for flagging anomalies in a metric with a real,
    sustained growth trend (revenue is the motivating case -- see module
    docstring). Answers "is this period's growth unusual relative to its
    own recent growth trend?", not "is this an unusual level?".

    Runs detect_anomalies on stmt[column]'s period-over-period growth rate
    (pct_change(lag)) rather than its raw level, so a statistically
    extreme but *consistent* growth rate sits near its own trailing
    baseline and isn't flagged -- only a break in that trend is.

    `stmt` is expected to have a period_end column (e.g. from
    statements.get_statement) and is indexed by it internally. `lag=1` is
    quarter-over-quarter; use `lag=4` for year-over-year growth on a
    quarterly-cadence statement.
    """
    growth = stmt.set_index("period_end")[column].pct_change(periods=lag).dropna()
    return detect_anomalies(growth, window=window, threshold=threshold)

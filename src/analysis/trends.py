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

from .periods import chained_trailing_window, prior_period_series


def trailing_stats(series: pd.Series, window: int) -> pd.DataFrame:
    """
    Rolling mean/std of `series`, computed from the `window` calendar
    quarters immediately preceding each point -- never including the point
    itself, to avoid look-ahead/self-inclusion bias. `series.index` must be
    a sorted-ascending DatetimeIndex of period_end dates (both current
    callers already build it that way via stmt.set_index("period_end")):
    each point's trailing window is found by chaining `window`
    quarters_back=1 periods.find_prior_period hops back from it
    (periods.chained_trailing_window), not a positional `rolling(window)`
    -- a missing quarter anywhere in what would be the window means there's
    no real trailing baseline for that point, not a silently-shorter one.

    Returns a DataFrame aligned to `series.index` with columns value,
    trailing_mean, trailing_std, trailing_gap. trailing_mean/trailing_std
    are NaN whenever the trailing window isn't fully available -- either
    not enough history yet, or a real gap partway through it;
    `trailing_gap` is True only for the latter case (a real missing
    quarter broke the chain), so a caller can tell the two apart.

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
    period_ends = pd.Series(series.index, index=series.index)
    if not period_ends.is_monotonic_increasing:
        raise ValueError("series.index must be a sorted-ascending DatetimeIndex of period_end")

    trailing_mean = []
    trailing_std = []
    trailing_gap = []
    for i in range(len(series)):
        indices, reason = chained_trailing_window(
            period_ends.reset_index(drop=True), i, hops=window
        )
        if indices is None:
            trailing_mean.append(float("nan"))
            trailing_std.append(float("nan"))
            trailing_gap.append(reason == "gap_no_prior_period")
        else:
            window_values = series.iloc[indices]
            if window_values.isna().any():
                trailing_mean.append(float("nan"))
                trailing_std.append(float("nan"))
            else:
                trailing_mean.append(window_values.mean())
                trailing_std.append(window_values.std())
            trailing_gap.append(False)

    return pd.DataFrame(
        {
            "value": series,
            "trailing_mean": pd.Series(trailing_mean, index=series.index),
            "trailing_std": pd.Series(trailing_std, index=series.index),
            "trailing_gap": pd.Series(trailing_gap, index=series.index),
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

    `series.index` must be a sorted-ascending DatetimeIndex of period_end
    dates (see trailing_stats) -- this function doesn't sort it.

    Returns the full period-aligned DataFrame: value, trailing_mean,
    trailing_std, deviation_abs, deviation_std, is_anomaly, trailing_gap
    (see trailing_stats -- True only when a real missing quarter, not just
    insufficient leading history, broke that point's trailing window).
    Filter to just the flagged periods with result[result.is_anomaly].
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
            "trailing_gap": stats["trailing_gap"],
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
    rather than its raw level, so a statistically extreme but *consistent*
    growth rate sits near its own trailing baseline and isn't flagged --
    only a break in that trend is. The growth rate for each row is found
    via periods.find_prior_period (calendar-based, `lag` quarters back),
    not a positional `pct_change(periods=lag)` -- a missing quarter
    produces NaN in place for that row rather than silently comparing
    across the gap. Unlike a plain pct_change, rows with no computable
    growth are kept (as NaN), not dropped -- dropping them would collapse
    away a real reporting gap and let a later row's trailing window
    silently span it, reintroducing the same positional bug one level up.

    `stmt` is expected to have a period_end column (e.g. from
    statements.get_statement) and is indexed by it internally. `lag=1` is
    quarter-over-quarter; `lag=4` is year-over-year growth on a
    quarterly-cadence statement -- the only two values periods.py has a
    justified tolerance for.
    """
    lookup = prior_period_series(stmt["period_end"], quarters_back=lag)
    current = stmt[column]
    growth_values = []
    for i in range(len(stmt)):
        prior_idx = lookup["prior_index"].iloc[i]
        if pd.isna(prior_idx):
            growth_values.append(float("nan"))
            continue
        c, p = current.iloc[i], current.iloc[int(prior_idx)]
        if pd.notna(c) and pd.notna(p) and p != 0:
            growth_values.append((c - p) / p)
        else:
            growth_values.append(float("nan"))
    growth = pd.Series(growth_values, index=pd.DatetimeIndex(stmt["period_end"]))
    return detect_anomalies(growth, window=window, threshold=threshold)

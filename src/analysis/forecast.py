"""
Deterministic projections of a filed statement metric into future periods.

forecast_metric is the one place in the analysis layer that produces a
number the company hasn't reported yet. Per this project's core design
principle (see CLAUDE.md), the agent layer calls this as a tool rather
than ever projecting a number itself -- every returned row carries the
assumptions that produced it (method, how many historical periods were
fit, the fitted slope/growth rate, a fit-quality figure, and any
seasonal factors) in `df.attrs["assumptions"]`, not just in this
docstring, so the caller has something concrete to relay rather than
presenting a bare projected number.

Three methods:
  - "trend": ordinary least squares of the last `lookback` periods'
    values against their row position, projected forward.
  - "growth": the average period-over-period growth rate over the last
    `lookback` periods, compounded forward from the most recent value.
    Not guarded against a negative or sign-flipping base metric (e.g.
    net_income crossing from negative to positive) -- the compounding
    math still runs, but the result may not be interpretable; use
    method="trend" for a metric that can go negative.
  - "seasonal": the same linear trend as "trend", plus a fixed offset
    per position-in-fiscal-year (this period's row position modulo 4)
    derived from that position's average historical deviation from
    trend. Needs at least MIN_PERIODS_SEASONAL periods of
    quarterly-cadence history to have more than one observation per
    fiscal-quarter bucket.

    Deliberately buckets periods by row position modulo 4, not calendar
    month (df["period_end"].dt.quarter): statements.get_statement
    excludes fiscal_year/fiscal_period on purpose (see its module
    docstring -- EDGAR's fy/fp can reflect a later filing's comparative-
    column attribution, not the period's real fiscal quarter), and many
    companies' fiscal quarters don't align to calendar quarters anyway
    (e.g. MSFT's fiscal year ends June 30). Row position modulo 4 is a
    safe stand-in *only* because the gap check below
    (_check_regular_cadence) already guarantees the window has no
    missing periods -- four consecutive rows are guaranteed to be one
    real fiscal year, so "position mod 4" reliably groups the same
    fiscal quarter together across years without ever needing to know
    which calendar month it falls in.

Refuses -- returns an empty DataFrame with `df.attrs["refused"] = True`
and a plain-English `df.attrs["reason"]` -- rather than emitting a
misleading projection, in the same spirit as statements._derive_q4
refusing a Q4 synthesis when its periods don't tile. This covers: not
enough usable history for the method (including a caller-supplied
`lookback` that's already below the method's floor), a gap in the
period_end sequence (per NOTES.md, rolling/shift windows in this
codebase are positional, not calendar-aware -- a gap would silently
make an "N trailing periods" window span more real calendar time than
the caller thinks; forecast_metric checks for this explicitly instead
of inheriting that bug), non-quarterly cadence for method="seasonal",
a linear fit too poor to be meaningful (R^2 < MIN_R2), or period-over-
period growth too volatile to average meaningfully (std > MAX_GROWTH_STD).

Malformed input -- an unknown method, a column not present in `stmt`,
periods_ahead < 1, or a non-positive `lookback` -- raises ValueError
instead: a caller configuration error, not a data fact, mirroring
get_statement's ValueError for an unknown period_length.

Confidence intervals (95%, in the `lower`/`upper` columns) are a normal
approximation off the in-sample residual standard deviation -- not a
proper Student-t prediction interval, since this project has no scipy
dependency -- widening for how far a projected point sits from the
historical window's center, per the standard OLS prediction-interval
formula. Documented simplification, not a bug.
"""

import numpy as np
import pandas as pd

from ..data.concepts import _QUARTERLY_DAYS_MAX, _QUARTERLY_DAYS_MIN

MIN_PERIODS_TREND = 4
MIN_PERIODS_GROWTH = 3
MIN_PERIODS_SEASONAL = 8
DEFAULT_LOOKBACK = 8

# Below this R^2, a linear trend (method="trend"/"seasonal") is refused as
# too poor a fit to project from. Above this period-over-period growth-rate
# standard deviation, method="growth" is refused as too volatile to average
# meaningfully. Both are judgment calls, not derived thresholds -- tune here.
MIN_R2 = 0.3
MAX_GROWTH_STD = 1.0

# ~95% two-sided normal-approximation multiplier -- see module docstring's CI caveat.
_CI_Z = 1.96

# A period_end gap deviating more than this fraction from the window's median
# gap is treated as a missing period -- see module docstring / NOTES.md.
_GAP_TOLERANCE = 0.5

METHODS = ("trend", "growth", "seasonal")
_MIN_PERIODS_BY_METHOD = {
    "trend": MIN_PERIODS_TREND,
    "growth": MIN_PERIODS_GROWTH,
    "seasonal": MIN_PERIODS_SEASONAL,
}


def forecast_metric(
    stmt: pd.DataFrame,
    column: str,
    periods_ahead: int = 2,
    method: str = "trend",
    lookback: int | None = None,
) -> pd.DataFrame:
    """
    Project `column` (a statement concept, e.g. "revenue") `periods_ahead`
    periods beyond `stmt`'s last row, using `method` ("trend"/"growth"/
    "seasonal" -- see module docstring). `lookback` caps how many of the
    most recent historical periods with usable (non-null) `column` data
    the method fits on; defaults to DEFAULT_LOOKBACK, capped by however
    much history is actually available.

    Returns a DataFrame with columns period_end, value, lower, upper (one
    row per projected period) on success, or an empty DataFrame when
    refused -- see module docstring for the raise-vs-refuse split.
    `df.attrs` always carries: method, periods_ahead,
    historical_periods_used, refused, reason (None on success), and
    assumptions (None when refused; a method-specific dict of the fitted
    parameters otherwise -- see the module docstring's per-method list).
    """
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}")
    if column not in stmt.columns:
        raise ValueError(f"{column!r} is not a column of the given statement")
    if periods_ahead < 1:
        raise ValueError("periods_ahead must be at least 1")
    if lookback is not None and lookback < 1:
        raise ValueError("lookback must be at least 1")

    min_periods = _MIN_PERIODS_BY_METHOD[method]
    if lookback is not None and lookback < min_periods:
        return _refuse(
            method,
            periods_ahead,
            0,
            f"requested lookback={lookback} is below the {min_periods} periods "
            f"method={method!r} needs to fit anything meaningful",
        )

    history = (
        stmt[["period_end", column]]
        .dropna(subset=[column])
        .sort_values("period_end")
        .reset_index(drop=True)
    )
    requested_lookback = lookback if lookback is not None else DEFAULT_LOOKBACK
    window = history.tail(min(requested_lookback, len(history))).reset_index(drop=True)
    n = len(window)
    if n < min_periods:
        return _refuse(
            method,
            periods_ahead,
            n,
            f"only {n} historical period(s) have usable {column!r} data; "
            f"method={method!r} needs at least {min_periods}",
        )

    regular, median_gap_days = _check_regular_cadence(window["period_end"])
    if not regular:
        return _refuse(
            method,
            periods_ahead,
            n,
            f"gap detected in the {column!r} period_end sequence over the last {n} periods "
            "-- a trailing window here would silently span more calendar time than "
            f"{n} periods (see NOTES.md's positional-window caveat)",
        )

    if method == "seasonal" and not (_QUARTERLY_DAYS_MIN <= median_gap_days <= _QUARTERLY_DAYS_MAX):
        return _refuse(
            method,
            periods_ahead,
            n,
            f"method='seasonal' requires quarterly-cadence data; this window's periods are "
            f"~{median_gap_days:.0f} days apart",
        )

    future_period_ends = [
        window["period_end"].iloc[-1] + pd.Timedelta(days=round(median_gap_days * k))
        for k in range(1, periods_ahead + 1)
    ]
    values = window[column].astype(float).to_numpy()

    if method == "trend":
        return _forecast_trend(column, values, future_period_ends, periods_ahead, n)
    if method == "growth":
        return _forecast_growth(column, values, future_period_ends, periods_ahead, n)
    return _forecast_seasonal(column, values, future_period_ends, periods_ahead, n)


def _check_regular_cadence(period_ends: pd.Series) -> tuple[bool, float]:
    """
    True (plus the median day-gap) if consecutive `period_ends` are evenly
    spaced within _GAP_TOLERANCE of their median gap; False if any gap
    deviates more than that -- signaling a missing period that would
    otherwise silently distort a positional trailing window (see module
    docstring / NOTES.md).
    """
    diffs = period_ends.diff().dropna().dt.days
    if diffs.empty:
        return True, 0.0
    median = float(diffs.median())
    if median <= 0:
        return False, median
    irregular = (diffs - median).abs() > _GAP_TOLERANCE * median
    return not bool(irregular.any()), median


def _fit_linear(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float, float, float]:
    """OLS fit of y on x. Returns (slope, intercept, r_squared, residual_std, sxx, x_mean)."""
    n = len(x)
    x_mean, y_mean = float(x.mean()), float(y.mean())
    sxx = float(((x - x_mean) ** 2).sum())
    if sxx == 0:
        slope = 0.0
    else:
        sxy = float(((x - x_mean) * (y - y_mean)).sum())
        slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    fitted = intercept + slope * x
    residuals = y - fitted
    ss_res = float((residuals**2).sum())
    ss_tot = float(((y - y_mean) ** 2).sum())
    # A perfectly flat series has ss_tot == 0 -- the trend (slope 0) fits it exactly,
    # so R^2 is 1.0, not undefined-by-division-by-zero.
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    residual_std = float(np.sqrt(ss_res / (n - 2))) if n > 2 else 0.0
    return slope, intercept, r_squared, residual_std, sxx, x_mean


def _prediction_interval(
    future_x: np.ndarray, projected: np.ndarray, residual_std: float, n: int, sxx: float, x_mean: float
) -> tuple[np.ndarray, np.ndarray]:
    """95% (normal-approximation) OLS prediction interval around `projected`."""
    if sxx == 0 or residual_std == 0:
        se = np.zeros_like(future_x)
    else:
        se = residual_std * np.sqrt(1 + 1 / n + (future_x - x_mean) ** 2 / sxx)
    return projected - _CI_Z * se, projected + _CI_Z * se


def _refuse(method: str, periods_ahead: int, n: int, reason: str) -> pd.DataFrame:
    df = pd.DataFrame({"period_end": [], "value": [], "lower": [], "upper": []})
    df.attrs = {
        "method": method,
        "periods_ahead": periods_ahead,
        "historical_periods_used": n,
        "refused": True,
        "reason": reason,
        "assumptions": None,
    }
    return df


def _result_frame(
    method: str,
    periods_ahead: int,
    n: int,
    period_ends: list,
    values: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    assumptions: dict,
) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "period_end": period_ends,
            "value": [float(v) for v in values],
            "lower": [float(v) for v in lower],
            "upper": [float(v) for v in upper],
        }
    )
    df.attrs = {
        "method": method,
        "periods_ahead": periods_ahead,
        "historical_periods_used": n,
        "refused": False,
        "reason": None,
        "assumptions": assumptions,
    }
    return df


def _forecast_trend(
    column: str, values: np.ndarray, future_period_ends: list, periods_ahead: int, n: int
) -> pd.DataFrame:
    x = np.arange(n, dtype=float)
    slope, intercept, r_squared, residual_std, sxx, x_mean = _fit_linear(x, values)
    if r_squared < MIN_R2:
        return _refuse(
            "trend",
            periods_ahead,
            n,
            f"linear trend fit quality is too low to project from (R^2={r_squared:.2f} < "
            f"{MIN_R2}) over the last {n} periods of {column!r}",
        )

    future_x = np.arange(n, n + periods_ahead, dtype=float)
    projected = intercept + slope * future_x
    lower, upper = _prediction_interval(future_x, projected, residual_std, n, sxx, x_mean)

    assumptions = {
        "historical_periods_used": n,
        "slope_per_period": slope,
        "intercept": intercept,
        "r_squared": r_squared,
        "confidence_interval": (
            "95% normal approximation off the in-sample residual std "
            "(not a formal Student-t prediction interval)"
        ),
    }
    return _result_frame("trend", periods_ahead, n, future_period_ends, projected, lower, upper, assumptions)


def _forecast_growth(
    column: str, values: np.ndarray, future_period_ends: list, periods_ahead: int, n: int
) -> pd.DataFrame:
    prior, current = values[:-1], values[1:]
    growth_rates = [(c - p) / p for p, c in zip(prior, current) if p != 0]
    min_rates = MIN_PERIODS_GROWTH - 1
    if len(growth_rates) < min_rates:
        return _refuse(
            "growth",
            periods_ahead,
            n,
            f"only {len(growth_rates)} computable period-over-period growth rate(s) over the "
            f"last {n} periods of {column!r} (a zero-value base period can't produce one); "
            f"need at least {min_rates}",
        )

    last_value = float(values[-1])
    if last_value == 0:
        return _refuse(
            "growth",
            periods_ahead,
            n,
            f"the most recent period's {column!r} value is zero, so a growth rate can't be "
            "projected forward from it",
        )

    growth_arr = np.array(growth_rates)
    avg_growth = float(growth_arr.mean())
    growth_std = float(growth_arr.std(ddof=1)) if len(growth_arr) > 1 else 0.0
    if growth_std > MAX_GROWTH_STD:
        return _refuse(
            "growth",
            periods_ahead,
            n,
            f"period-over-period growth rate of {column!r} is too volatile to average "
            f"meaningfully (std={growth_std:.2f} > {MAX_GROWTH_STD}) over the last {n} periods",
        )

    steps = np.arange(1, periods_ahead + 1)
    projected = last_value * (1.0 + avg_growth) ** steps
    lower_growth, upper_growth = avg_growth - _CI_Z * growth_std, avg_growth + _CI_Z * growth_std
    bound_a = last_value * (1.0 + lower_growth) ** steps
    bound_b = last_value * (1.0 + upper_growth) ** steps
    lower, upper = np.minimum(bound_a, bound_b), np.maximum(bound_a, bound_b)

    assumptions = {
        "historical_periods_used": n,
        "growth_rates_used": len(growth_rates),
        "average_growth_rate": avg_growth,
        "growth_rate_std": growth_std,
        "confidence_interval": (
            "95% normal approximation: average_growth_rate +/- 1.96*std, compounded "
            "forward from the last period's value"
        ),
    }
    return _result_frame("growth", periods_ahead, n, future_period_ends, projected, lower, upper, assumptions)


def _forecast_seasonal(
    column: str, values: np.ndarray, future_period_ends: list, periods_ahead: int, n: int
) -> pd.DataFrame:
    x = np.arange(n, dtype=float)
    slope, intercept, r_squared, _, sxx, x_mean = _fit_linear(x, values)
    if r_squared < MIN_R2:
        return _refuse(
            "seasonal",
            periods_ahead,
            n,
            "the underlying linear trend fit quality is too low to build a seasonal "
            f"projection on (R^2={r_squared:.2f} < {MIN_R2}) over the last {n} periods of "
            f"{column!r}",
        )

    fitted = intercept + slope * x
    residuals = values - fitted
    buckets = np.arange(n) % 4
    seasonal_factors = {
        b: float(residuals[buckets == b].mean()) if (buckets == b).any() else 0.0 for b in range(4)
    }

    deseasonalized = residuals - np.array([seasonal_factors[b] for b in buckets])
    ss_res_deseason = float((deseasonalized**2).sum())
    residual_std = float(np.sqrt(ss_res_deseason / (n - 2))) if n > 2 else 0.0

    future_x = np.arange(n, n + periods_ahead, dtype=float)
    future_buckets = np.arange(n, n + periods_ahead) % 4
    trend_component = intercept + slope * future_x
    seasonal_component = np.array([seasonal_factors[b] for b in future_buckets])
    projected = trend_component + seasonal_component

    lower, upper = _prediction_interval(future_x, projected, residual_std, n, sxx, x_mean)

    assumptions = {
        "historical_periods_used": n,
        "slope_per_period": slope,
        "intercept": intercept,
        "r_squared_before_seasonal_adjustment": r_squared,
        "seasonal_factors_by_fiscal_quarter_position": {
            str(b + 1): seasonal_factors[b] for b in range(4)
        },
        "confidence_interval": (
            "95% normal approximation off the deseasonalized in-sample residual std "
            "(not a formal Student-t prediction interval)"
        ),
    }
    return _result_frame(
        "seasonal", periods_ahead, n, future_period_ends, projected, lower, upper, assumptions
    )

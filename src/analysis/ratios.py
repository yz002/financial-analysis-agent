"""
Computed financial ratios from a statement DataFrame (statements.get_statement).

Every function takes the whole statement DataFrame and returns a DataFrame
aligned by period_end, with a "value" column plus the named input columns
that produced it -- a table, not a single-row API, because growth ratios
need multi-row history (i-1/i-4 lookback) and this way the result composes
directly with trends.py (result["value"] feeds detect_anomalies directly).
A single period's value+inputs is just result.loc[i] or a period_end
lookup -- no separate return type is needed for that.

None, not NaN, for anything uncomputable. A bare Python None upcasts to
NaN the moment it's put in a normal float64 pandas column, which would
make "missing" indistinguishable from "computed as zero" once mixed with
real floats. To keep None as a literal, surviving value, "value" columns
here are built dtype=object from a plain Python list produced by
_safe_divide (never raises: a None/NaN input or a zero denominator
produces None, not an exception, not a propagated NaN). This does mean a
"value" column doesn't behave like a normal numeric pandas column --
vectorized numeric ops need `.astype("float64")` first (which correctly
turns any surviving None into NaN, pandas' idiom for a numeric-computation
context). See NOTES.md. trends.py routes around this by pulling raw
numeric statement columns directly rather than going through this module.
"""

import pandas as pd

from .periods import chained_trailing_window, prior_period_series


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    """None if either input is missing/NaN or denominator is zero; else a plain float."""
    if numerator is None or denominator is None or pd.isna(numerator) or pd.isna(denominator):
        return None
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _ratio_frame(stmt: pd.DataFrame, values: list, inputs: dict[str, pd.Series]) -> pd.DataFrame:
    result = {
        "period_end": stmt["period_end"],
        "value": pd.Series(values, index=stmt.index, dtype=object),
    }
    result.update(inputs)
    return pd.DataFrame(result).reset_index(drop=True)


def _ratio(stmt: pd.DataFrame, numerator_col: str, denominator_col: str) -> pd.DataFrame:
    numerator, denominator = stmt[numerator_col], stmt[denominator_col]
    values = [_safe_divide(n, d) for n, d in zip(numerator, denominator)]
    return _ratio_frame(stmt, values, {numerator_col: numerator, denominator_col: denominator})


def gross_margin(stmt: pd.DataFrame) -> pd.DataFrame:
    """gross_profit / revenue, per period."""
    return _ratio(stmt, "gross_profit", "revenue")


def operating_margin(stmt: pd.DataFrame) -> pd.DataFrame:
    """operating_income / revenue, per period."""
    return _ratio(stmt, "operating_income", "revenue")


def net_margin(stmt: pd.DataFrame) -> pd.DataFrame:
    """net_income / revenue, per period."""
    return _ratio(stmt, "net_income", "revenue")


def _growth(stmt: pd.DataFrame, column: str, lag: int) -> pd.DataFrame:
    """
    (column[i] - column[prior]) / column[prior], where `prior` is the row
    whose period_end falls ~lag quarters before row i's, found via
    periods.find_prior_period (calendar-based, not `shift(lag)` -- a
    missing quarter is refused, not silently misaligned; see NOTES.md).
    Value is None, with the reason recorded in the `{column}_growth_reason`
    companion column, when: there isn't `lag` quarters of history yet
    ("insufficient_history"), the calendar lookup can't find a matching
    prior quarter ("gap_no_prior_period"), or a matching quarter exists but
    `column`'s value itself is missing that period ("missing_value").
    """
    lookup = prior_period_series(stmt["period_end"], quarters_back=lag)
    current = stmt[column]
    prior = pd.Series(
        [current.iloc[idx] if pd.notna(idx) else None for idx in lookup["prior_index"]],
        index=stmt.index,
    )
    reasons = []
    values = []
    for c, p, lookup_reason in zip(current, prior, lookup["reason"]):
        if lookup_reason is not None:
            values.append(None)
            reasons.append(lookup_reason)
        elif pd.notna(c) and pd.notna(p):
            v = _safe_divide(c - p, p)
            values.append(v)
            reasons.append(None if v is not None else "division_by_zero")
        else:
            values.append(None)
            reasons.append("missing_value")
    reasons = pd.Series(reasons, index=stmt.index, dtype=object)
    return _ratio_frame(
        stmt,
        values,
        {column: current, f"{column}_prior": prior, f"{column}_growth_reason": reasons},
    )


def revenue_growth_qoq(stmt: pd.DataFrame) -> pd.DataFrame:
    """Quarter-over-quarter revenue growth. Assumes `stmt` is quarterly-cadence."""
    return _growth(stmt, "revenue", lag=1)


def revenue_growth_yoy(stmt: pd.DataFrame) -> pd.DataFrame:
    """
    Year-over-year revenue growth (current quarter vs. the same quarter a
    year ago, lag=4). Assumes `stmt` is quarterly-cadence -- on an annual
    statement, use revenue_growth_qoq instead, since consecutive annual
    rows are already a year apart.
    """
    return _growth(stmt, "revenue", lag=4)


def earnings_growth_qoq(stmt: pd.DataFrame) -> pd.DataFrame:
    """Quarter-over-quarter net income growth. Assumes `stmt` is quarterly-cadence."""
    return _growth(stmt, "net_income", lag=1)


def earnings_growth_yoy(stmt: pd.DataFrame) -> pd.DataFrame:
    """Year-over-year net income growth (lag=4). Assumes `stmt` is quarterly-cadence."""
    return _growth(stmt, "net_income", lag=4)


def free_cash_flow(stmt: pd.DataFrame) -> pd.DataFrame:
    """
    operating_cash_flow - capex, per period. Not a ratio (no division), but
    still normalizes a missing input to None rather than a propagated NaN,
    for consistency with every other function here. Sign convention:
    capex tags (see concepts.py CONCEPTS) report positive payment
    magnitudes, so subtraction (not addition) is correct.
    """
    ocf, capex = stmt["operating_cash_flow"], stmt["capex"]
    values = [(o - c) if pd.notna(o) and pd.notna(c) else None for o, c in zip(ocf, capex)]
    return _ratio_frame(stmt, values, {"operating_cash_flow": ocf, "capex": capex})


def debt_to_assets(stmt: pd.DataFrame) -> pd.DataFrame:
    """total_liabilities / total_assets, per period."""
    return _ratio(stmt, "total_liabilities", "total_assets")


def current_ratio(stmt: pd.DataFrame) -> pd.DataFrame:
    """current_assets / current_liabilities, per period."""
    return _ratio(stmt, "current_assets", "current_liabilities")


def _ttm(period_ends: pd.Series, series: pd.Series) -> pd.Series:
    """
    Trailing-twelve-month sum: the current row plus the 3 quarters
    immediately preceding it *by calendar date*, found by chaining three
    quarters_back=1 periods.find_prior_period hops back from each row
    (periods.chained_trailing_window) rather than a positional
    `rolling(4)` -- a gap anywhere in the trailing window (a missing
    quarter) means there's no real 4-quarter window to sum, so that row's
    result is NaN, same as not having 4 rows of history yet at all
    (consistent with _growth's "not enough history yet -> None"
    convention rather than understating an incomplete window).
    """
    values = []
    for i in range(len(series)):
        window, _reason = chained_trailing_window(period_ends, i, hops=3)
        if window is None:
            values.append(float("nan"))
        else:
            window_values = series.iloc[window + [i]]
            values.append(float("nan") if window_values.isna().any() else window_values.sum())
    return pd.Series(values, index=series.index)


def _return_ratio(stmt: pd.DataFrame, denominator_col: str, period_length: str) -> pd.DataFrame:
    """Shared by roa/roe -- see their docstrings for the numerator/denominator handling."""
    if period_length == "quarterly":
        numerator, numerator_col = _ttm(stmt["period_end"], stmt["net_income"]), "net_income_ttm"
    elif period_length == "annual":
        numerator, numerator_col = stmt["net_income"], "net_income"
    else:
        raise ValueError('period_length must be "quarterly" or "annual"')
    denominator = stmt[denominator_col]
    values = [_safe_divide(n, d) for n, d in zip(numerator, denominator)]
    return _ratio_frame(stmt, values, {numerator_col: numerator, denominator_col: denominator})


def roa(stmt: pd.DataFrame, period_length: str = "quarterly") -> pd.DataFrame:
    """
    Return on assets: net_income / total_assets, annualized. On a
    quarterly-cadence `stmt` (period_length="quarterly", the default --
    matching this module's assumed-cadence convention elsewhere, e.g.
    revenue_growth_qoq), net_income is the trailing-twelve-month sum
    (current quarter plus the prior 3, see _ttm) rather than the single
    quarter's figure: dividing one quarter's net_income by a full-year-scale
    total_assets understates ROA by roughly 4x versus any published
    (annualized) figure. The first 3 rows of a quarterly `stmt` have no
    full trailing window yet, so their value is None. On
    period_length="annual", net_income is already a full fiscal year and
    is used as-is, unmodified. total_assets is always the period's ending
    balance rather than a beginning/ending average -- a documented
    simplification, not a bug. Raises ValueError for any other
    period_length.
    """
    return _return_ratio(stmt, "total_assets", period_length)


def roe(stmt: pd.DataFrame, period_length: str = "quarterly") -> pd.DataFrame:
    """
    Return on equity: net_income / stockholders_equity, annualized. Same
    trailing-twelve-month numerator and ending-balance denominator
    handling as roa (see its docstring).
    """
    return _return_ratio(stmt, "stockholders_equity", period_length)

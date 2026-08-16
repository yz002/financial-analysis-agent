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
    (column[i] - column[i-lag]) / column[i-lag]. The first `lag` rows have
    no prior period to compare against, so their value is None.
    """
    current, prior = stmt[column], stmt[column].shift(lag)
    values = [
        _safe_divide(c - p, p) if pd.notna(c) and pd.notna(p) else None
        for c, p in zip(current, prior)
    ]
    return _ratio_frame(stmt, values, {column: current, f"{column}_prior": prior})


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


def roa(stmt: pd.DataFrame) -> pd.DataFrame:
    """
    net_income / total_assets, per period. net_income is a period (duration)
    figure while total_assets is a point-in-time (instant) figure; this
    uses the period's ending balance as the denominator rather than an
    average of beginning/ending balances -- a documented simplification,
    not a bug.
    """
    return _ratio(stmt, "net_income", "total_assets")


def roe(stmt: pd.DataFrame) -> pd.DataFrame:
    """
    net_income / stockholders_equity, per period. Same ending-balance
    simplification as roa (see its docstring).
    """
    return _ratio(stmt, "net_income", "stockholders_equity")

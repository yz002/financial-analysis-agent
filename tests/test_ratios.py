import pandas as pd
import pytest

from src.analysis import ratios


def _stmt_with_gap(values, column="revenue", start="2020-03-31", step_days=91, gap_at=None, extra_days=91):
    """
    A minimal statement-shaped DataFrame: period_end plus one value column.
    Steps are `step_days` apart, except right before index `gap_at`
    (default: the midpoint), where the step is widened by `extra_days` --
    mirrors test_forecast.py's _stmt_with_gap.
    """
    if gap_at is None:
        gap_at = len(values) // 2
    period_ends = []
    current = pd.Timestamp(start)
    for i in range(len(values)):
        if i == gap_at:
            current = current + pd.Timedelta(days=extra_days)
        period_ends.append(current)
        current = current + pd.Timedelta(days=step_days)
    return pd.DataFrame({"period_end": period_ends, column: values})


def test_ford_gross_margin_is_none_for_every_row(ford_quarterly):
    result = ratios.gross_margin(ford_quarterly)
    assert result["value"].apply(lambda v: v is None).all()


def test_msft_gross_margin_in_plausible_range(msft_quarterly):
    result = ratios.gross_margin(msft_quarterly)
    latest = result["value"].iloc[-1]
    assert isinstance(latest, float)
    assert 0 < latest < 1


def test_division_by_zero_returns_none_not_nan_or_inf():
    stmt = pd.DataFrame(
        {
            "period_end": [pd.Timestamp("2024-01-01")],
            "gross_profit": [100.0],
            "revenue": [0.0],
        }
    )
    value = ratios.gross_margin(stmt)["value"].iloc[0]
    assert value is None


def test_missing_input_returns_none():
    stmt = pd.DataFrame(
        {
            "period_end": [pd.Timestamp("2024-01-01")],
            "gross_profit": [float("nan")],
            "revenue": [100.0],
        }
    )
    value = ratios.gross_margin(stmt)["value"].iloc[0]
    assert value is None


def test_revenue_growth_qoq_first_row_is_none(msft_quarterly):
    result = ratios.revenue_growth_qoq(msft_quarterly)
    assert result["value"].iloc[0] is None


def test_revenue_growth_yoy_first_four_rows_are_none(msft_quarterly):
    result = ratios.revenue_growth_yoy(msft_quarterly)
    assert result["value"].iloc[:4].apply(lambda v: v is None).all()


def test_revenue_growth_qoq_gap_returns_none_with_gap_reason():
    stmt = _stmt_with_gap([100.0, 110.0, 120.0, 130.0, 140.0, 150.0], gap_at=3, extra_days=91)
    result = ratios.revenue_growth_qoq(stmt)
    assert result["value"].iloc[3] is None
    assert result["revenue_growth_reason"].iloc[3] == "gap_no_prior_period"
    # The row right after the gap has a normal, present prior quarter.
    assert result["value"].iloc[4] is not None
    assert result["revenue_growth_reason"].iloc[4] is None


def test_revenue_growth_yoy_gap_returns_none_with_gap_reason():
    stmt = _stmt_with_gap(
        [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0],
        gap_at=1,
        extra_days=365,
    )
    result = ratios.revenue_growth_yoy(stmt)
    assert result["value"].iloc[4] is None
    assert result["revenue_growth_reason"].iloc[4] == "gap_no_prior_period"


def test_revenue_growth_qoq_first_row_reason_is_insufficient_history(msft_quarterly):
    result = ratios.revenue_growth_qoq(msft_quarterly)
    assert result["revenue_growth_reason"].iloc[0] == "insufficient_history"


def test_revenue_growth_qoq_long_kroger_style_quarter_is_not_spuriously_refused():
    # A ~111-day quarter is real (e.g. Kroger's Q1) -- 20 days off the
    # ~91-day nominal target, well within tolerance. Growth must still be
    # computed, not refused.
    stmt = _stmt_with_gap([100.0, 110.0, 120.0, 130.0], gap_at=2, extra_days=20)
    result = ratios.revenue_growth_qoq(stmt)
    assert result["value"].iloc[2] is not None
    assert result["revenue_growth_reason"].iloc[2] is None


def test_revenue_growth_qoq_returns_gap_for_every_row_on_annual_statement(msft_annual):
    """Confirmed live during the pre-demo audit: on an annual-cadence statement,
    revenue_growth_qoq's lag=1 targets ~3 calendar months back, which never matches annual
    spacing -- every row comes back None, and every row but the first is "gap_no_prior_period"
    (the first is "insufficient_history", same as on a quarterly statement). No existing test
    exercised either growth direction against an annual statement before this."""
    result = ratios.revenue_growth_qoq(msft_annual)
    assert result["value"].apply(lambda v: v is None).all()
    assert result["revenue_growth_reason"].iloc[0] == "insufficient_history"
    assert (result["revenue_growth_reason"].iloc[1:] == "gap_no_prior_period").all()


def test_revenue_growth_yoy_returns_real_growth_on_annual_statement(msft_annual):
    """The correct call for annual YoY growth -- lag=4 targets ~12 months back, matching
    annual spacing, so every row but the first (which has no prior year at all) gets a real
    value. Range-checked rather than pinned to exact figures, since MSFT's live annual history
    grows by a row every year."""
    result = ratios.revenue_growth_yoy(msft_annual)
    non_null_values = [v for v in result["value"] if v is not None]
    assert len(non_null_values) == len(msft_annual) - 1
    assert all(isinstance(v, float) and -0.9 < v < 2.0 for v in non_null_values)


def test_free_cash_flow_matches_manual_calculation(msft_quarterly):
    result = ratios.free_cash_flow(msft_quarterly)
    latest_row = msft_quarterly.iloc[-1]
    expected = latest_row["operating_cash_flow"] - latest_row["capex"]
    assert result["value"].iloc[-1] == pytest.approx(expected)


def test_roe_and_current_ratio_not_none_for_msft(msft_quarterly):
    # Confirms the stockholders_equity/current_assets/current_liabilities
    # CONCEPTS entries added for this are actually wired through
    # get_statement -- otherwise these would be permanently None.
    assert ratios.roe(msft_quarterly)["value"].iloc[-1] is not None
    assert ratios.current_ratio(msft_quarterly)["value"].iloc[-1] is not None


def test_roa_not_none_for_msft(msft_quarterly):
    assert ratios.roa(msft_quarterly)["value"].iloc[-1] is not None


def test_debt_to_assets_not_none_for_msft(msft_quarterly):
    assert ratios.debt_to_assets(msft_quarterly)["value"].iloc[-1] is not None


ALL_RATIO_FUNCTIONS = [
    ratios.gross_margin,
    ratios.operating_margin,
    ratios.net_margin,
    ratios.revenue_growth_qoq,
    ratios.revenue_growth_yoy,
    ratios.earnings_growth_qoq,
    ratios.earnings_growth_yoy,
    ratios.free_cash_flow,
    ratios.debt_to_assets,
    ratios.current_ratio,
    ratios.roa,
    ratios.roe,
]


@pytest.mark.parametrize("ratio_fn", ALL_RATIO_FUNCTIONS)
def test_ratio_never_raises_on_fords_degraded_statement(ford_quarterly, ratio_fn):
    ratio_fn(ford_quarterly)

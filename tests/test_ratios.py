import pandas as pd
import pytest

from src.analysis import ratios


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

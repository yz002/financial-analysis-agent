"""
Tests for src/analysis/forecast.py, fully offline: forecast_metric is a pure
function of a statement-shaped DataFrame, so every case here builds its own
synthetic period_end/value series rather than touching EdgarClient or
statements.get_statement.
"""

import pandas as pd
import pytest

from src.analysis import forecast


def _stmt(values, column="revenue", start="2020-03-31", step_days=91):
    """A minimal statement-shaped DataFrame: period_end plus one value column, evenly spaced."""
    period_ends = [pd.Timestamp(start) + pd.Timedelta(days=step_days * i) for i in range(len(values))]
    return pd.DataFrame({"period_end": period_ends, column: values})


def _stmt_with_gap(values, column="revenue", start="2020-03-31", step_days=91, gap_at=None):
    """Same as _stmt, but doubles the gap right before index `gap_at` (default: the midpoint)."""
    if gap_at is None:
        gap_at = len(values) // 2
    period_ends = []
    current = pd.Timestamp(start)
    for i in range(len(values)):
        if i == gap_at:
            current = current + pd.Timedelta(days=step_days)
        period_ends.append(current)
        current = current + pd.Timedelta(days=step_days)
    return pd.DataFrame({"period_end": period_ends, column: values})


# --- malformed input (raises) -----------------------------------------------


def test_unknown_method_raises_value_error():
    stmt = _stmt([100.0] * 8)
    with pytest.raises(ValueError):
        forecast.forecast_metric(stmt, "revenue", method="bogus")


def test_unknown_column_raises_value_error():
    stmt = _stmt([100.0] * 8)
    with pytest.raises(ValueError):
        forecast.forecast_metric(stmt, "not_a_column")


def test_periods_ahead_below_one_raises_value_error():
    stmt = _stmt([100.0] * 8)
    with pytest.raises(ValueError):
        forecast.forecast_metric(stmt, "revenue", periods_ahead=0)


def test_nonpositive_lookback_raises_value_error():
    stmt = _stmt([100.0] * 8)
    with pytest.raises(ValueError):
        forecast.forecast_metric(stmt, "revenue", lookback=0)


# --- refusal paths (returns empty, doesn't raise) ---------------------------


def test_lookback_below_method_minimum_is_refused_not_raised():
    stmt = _stmt([100.0 + 10.0 * i for i in range(8)])
    result = forecast.forecast_metric(stmt, "revenue", method="trend", lookback=2)
    assert result.attrs["refused"] is True
    assert len(result) == 0
    assert "lookback" in result.attrs["reason"]
    assert result.attrs["assumptions"] is None


def test_insufficient_history_is_refused():
    # trend needs MIN_PERIODS_TREND=4; only 3 periods available.
    stmt = _stmt([100.0, 110.0, 120.0])
    result = forecast.forecast_metric(stmt, "revenue", method="trend")
    assert result.attrs["refused"] is True
    assert "3" in result.attrs["reason"]


def test_all_null_column_is_refused_like_fords_gross_profit():
    stmt = _stmt([float("nan")] * 8)
    result = forecast.forecast_metric(stmt, "revenue", method="trend")
    assert result.attrs["refused"] is True
    assert result.attrs["historical_periods_used"] == 0


def test_gap_in_period_sequence_is_refused():
    stmt = _stmt_with_gap([100.0 + 10.0 * i for i in range(8)])
    result = forecast.forecast_metric(stmt, "revenue", method="trend")
    assert result.attrs["refused"] is True
    assert "gap" in result.attrs["reason"]


def test_poor_trend_fit_is_refused():
    # A sign-flipping, non-linear series has essentially no linear trend to fit.
    values = [100.0, -100.0, 100.0, -100.0, 100.0, -100.0, 100.0, -100.0]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", method="trend")
    assert result.attrs["refused"] is True
    assert "R^2" in result.attrs["reason"]


def test_growth_refuses_on_zero_base_value():
    stmt = _stmt([100.0, 110.0, 0.0])
    result = forecast.forecast_metric(stmt, "revenue", method="growth")
    assert result.attrs["refused"] is True
    assert "zero" in result.attrs["reason"]


def test_growth_refuses_when_too_few_computable_rates():
    # Only the (100 -> 110) pair is computable; the (0 -> 100) pair is skipped (zero base),
    # leaving 1 rate < MIN_PERIODS_GROWTH - 1 = 2.
    stmt = _stmt([0.0, 100.0, 110.0])
    result = forecast.forecast_metric(stmt, "revenue", method="growth")
    assert result.attrs["refused"] is True
    assert "computable" in result.attrs["reason"]


def test_growth_refuses_on_excessive_volatility():
    values = [100.0, 1000.0, 10.0, 1000.0, 10.0, 1000.0, 10.0, 1000.0]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", method="growth")
    assert result.attrs["refused"] is True
    assert "volatile" in result.attrs["reason"]


def test_seasonal_refuses_below_minimum_periods():
    # 7 periods < MIN_PERIODS_SEASONAL=8.
    stmt = _stmt([100.0 + 10.0 * i for i in range(7)])
    result = forecast.forecast_metric(stmt, "revenue", method="seasonal")
    assert result.attrs["refused"] is True


def test_seasonal_refuses_non_quarterly_cadence():
    stmt = _stmt([100.0 + 10.0 * i for i in range(8)], step_days=365)
    result = forecast.forecast_metric(stmt, "revenue", method="seasonal")
    assert result.attrs["refused"] is True
    assert "quarterly" in result.attrs["reason"]


def test_seasonal_refuses_on_gap():
    stmt = _stmt_with_gap([100.0 + 10.0 * i for i in range(8)])
    result = forecast.forecast_metric(stmt, "revenue", method="seasonal")
    assert result.attrs["refused"] is True
    assert "gap" in result.attrs["reason"]


# --- method="trend" success --------------------------------------------------


def test_trend_success_on_perfect_line():
    values = [100.0 + 10.0 * i for i in range(8)]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", periods_ahead=2, method="trend")

    assert result.attrs["refused"] is False
    assert result.attrs["reason"] is None
    assert len(result) == 2
    assert result["value"].iloc[0] == pytest.approx(180.0)
    assert result["value"].iloc[1] == pytest.approx(190.0)
    # A perfect fit has zero residual std -> a zero-width interval.
    assert result["lower"].iloc[0] == pytest.approx(result["value"].iloc[0])
    assert result["upper"].iloc[0] == pytest.approx(result["value"].iloc[0])

    assumptions = result.attrs["assumptions"]
    assert assumptions["historical_periods_used"] == 8
    assert assumptions["slope_per_period"] == pytest.approx(10.0)
    assert assumptions["intercept"] == pytest.approx(100.0)
    assert assumptions["r_squared"] == pytest.approx(1.0)
    assert "confidence_interval" in assumptions


def test_trend_future_period_ends_extend_the_cadence():
    values = [100.0 + 10.0 * i for i in range(8)]
    stmt = _stmt(values, step_days=91)
    result = forecast.forecast_metric(stmt, "revenue", periods_ahead=2, method="trend")

    last_historical = stmt["period_end"].iloc[-1]
    assert result["period_end"].iloc[0] == last_historical + pd.Timedelta(days=91)
    assert result["period_end"].iloc[1] == last_historical + pd.Timedelta(days=182)
    assert result["period_end"].is_monotonic_increasing


def test_trend_respects_explicit_lookback():
    # A flat recent window should be recognized even though earlier history trends up sharply --
    # proof the fit only used the last `lookback` periods, not the full history.
    values = [1000.0, 2000.0, 3000.0, 4000.0] + [50.0, 50.0, 50.0, 50.0]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", method="trend", lookback=4)

    assert result.attrs["refused"] is False
    assert result.attrs["assumptions"]["historical_periods_used"] == 4
    assert result["value"].iloc[0] == pytest.approx(50.0, abs=1e-6)


# --- method="growth" success --------------------------------------------------


def test_growth_success_on_constant_rate():
    values = [100.0 * (1.05**i) for i in range(8)]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", periods_ahead=2, method="growth")

    assert result.attrs["refused"] is False
    assumptions = result.attrs["assumptions"]
    assert assumptions["average_growth_rate"] == pytest.approx(0.05, abs=1e-6)
    assert assumptions["growth_rate_std"] == pytest.approx(0.0, abs=1e-6)
    assert assumptions["growth_rates_used"] == 7

    last_value = values[-1]
    assert result["value"].iloc[0] == pytest.approx(last_value * 1.05)
    assert result["value"].iloc[1] == pytest.approx(last_value * 1.05**2)
    assert result["lower"].iloc[0] <= result["value"].iloc[0] <= result["upper"].iloc[0]


# --- method="seasonal" success ------------------------------------------------


def test_seasonal_success_shape_and_assumptions():
    trend = [100.0 + 10.0 * i for i in range(8)]
    seasonal_offsets = [5.0, -5.0, 5.0, -5.0] * 2
    values = [t + s for t, s in zip(trend, seasonal_offsets)]
    stmt = _stmt(values, step_days=91)

    result = forecast.forecast_metric(stmt, "revenue", periods_ahead=4, method="seasonal")

    assert result.attrs["refused"] is False
    assert len(result) == 4
    assumptions = result.attrs["assumptions"]
    assert set(assumptions["seasonal_factors_by_fiscal_quarter_position"]) == {"1", "2", "3", "4"}
    assert assumptions["r_squared_before_seasonal_adjustment"] > forecast.MIN_R2
    for lower, value, upper in zip(result["lower"], result["value"], result["upper"]):
        assert lower <= value <= upper


# --- shared result shape -----------------------------------------------------


def test_periods_ahead_controls_row_count():
    values = [100.0 + 10.0 * i for i in range(8)]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", periods_ahead=5, method="trend")
    assert len(result) == 5


def test_refused_result_has_expected_columns_even_when_empty():
    stmt = _stmt([100.0, 110.0, 120.0])
    result = forecast.forecast_metric(stmt, "revenue", method="trend")
    assert list(result.columns) == ["period_end", "value", "lower", "upper"]
    assert len(result) == 0


def test_success_result_attrs_report_method_and_periods_ahead():
    values = [100.0 + 10.0 * i for i in range(8)]
    stmt = _stmt(values)
    result = forecast.forecast_metric(stmt, "revenue", periods_ahead=3, method="trend")
    assert result.attrs["method"] == "trend"
    assert result.attrs["periods_ahead"] == 3
    assert result.attrs["historical_periods_used"] == 8

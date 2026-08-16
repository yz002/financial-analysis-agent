import pandas as pd
import pytest

from src.analysis.statements import get_statement


def test_ford_missing_gross_profit_is_nan(ford_quarterly):
    assert "gross_profit" in ford_quarterly.columns
    assert ford_quarterly["gross_profit"].isna().all()
    assert ford_quarterly["gross_profit_tag"].isna().all()


def test_ford_full_statement_does_not_raise(client):
    # Exercises the whole pipeline against Ford's degraded data (no
    # gross_profit at all) -- the important graceful-degradation case.
    get_statement("F", "quarterly", client=client)


def test_msft_fy2025_q4_revenue_derivation(msft_quarterly):
    row = msft_quarterly.loc[msft_quarterly["period_end"] == pd.Timestamp("2025-06-30")].iloc[0]
    assert row["revenue_is_derived"]
    assert row["revenue"] == pytest.approx(76_441_000_000)


def test_q4_derivation_happens_for_multiple_fiscal_years(msft_quarterly):
    # MSFT's fiscal year ends June 30 every year, so a healthy pipeline
    # should derive Q4 revenue for several years of history, not just one.
    # Catches a regression that silently derives zero (or exactly one) row.
    assert msft_quarterly["revenue_is_derived"].sum() > 1


def test_derived_rows_land_only_on_fiscal_year_ends(msft_quarterly, msft_annual):
    fiscal_year_ends = set(msft_annual["period_end"])
    derived_period_ends = set(
        msft_quarterly.loc[msft_quarterly["revenue_is_derived"], "period_end"]
    )
    assert derived_period_ends <= fiscal_year_ends

    non_fy_end_rows = msft_quarterly[~msft_quarterly["period_end"].isin(fiscal_year_ends)]
    assert not non_fy_end_rows["revenue_is_derived"].any()


def test_is_derived_columns_are_bool_never_nan(msft_quarterly, ford_quarterly):
    # Regression test: concat'ing derived Q4 rows onto the real quarterly
    # rows must not leave real rows with NaN in *_is_derived -- NaN is
    # truthy, so `if row.revenue_is_derived` would have treated every real
    # quarter as derived. Every duration concept's flag column must be a
    # real bool dtype with no missing values, on both a ticker with full
    # data (MSFT) and one with a fully-missing concept (Ford).
    for stmt in (msft_quarterly, ford_quarterly):
        for col in stmt.columns:
            if col.endswith("_is_derived"):
                assert stmt[col].dtype == bool, f"{col} is {stmt[col].dtype}, not bool"
                assert not stmt[col].isna().any(), f"{col} has NaN values"


def test_schema_consistent_across_tickers(msft_quarterly, nvda_quarterly, ford_quarterly):
    assert set(msft_quarterly.columns) == set(nvda_quarterly.columns) == set(ford_quarterly.columns)


def test_periods_truncation(client, msft_quarterly):
    truncated = get_statement("MSFT", "quarterly", periods=4, client=client)
    assert len(truncated) <= 4
    expected = msft_quarterly.tail(4).reset_index(drop=True)
    pd.testing.assert_frame_equal(truncated, expected)


def test_annual_mode_no_derivation(msft_annual):
    derived_cols = [c for c in msft_annual.columns if c.endswith("_is_derived")]
    for col in derived_cols:
        assert not msft_annual[col].any()


def test_sorted_by_period_end(msft_quarterly):
    assert msft_quarterly["period_end"].is_monotonic_increasing


def test_unknown_period_length_raises(client):
    with pytest.raises(ValueError):
        get_statement("MSFT", "monthly", client=client)

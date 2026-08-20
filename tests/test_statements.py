import pandas as pd
import pytest

from src.analysis.statements import _derive_q4, get_statement
from src.data.concepts import ConceptNotFoundError


def _qtr_row(period_start, period_end, value, fiscal_period="Q1", fiscal_year=2024,
             form="10-Q", filed="2024-06-01", tag="RevTag"):
    """A single quarterly-classified duration row, matching get_concept's real column shape."""
    return {
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "value": value,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "form": form,
        "filed": pd.Timestamp(filed),
        "tag": tag,
        "period_length": "quarterly",
    }


def _fy_row(period_start, period_end, value, fiscal_year=2024, form="10-K",
            filed="2025-02-01", tag="RevTag"):
    """A single annual-classified duration row, matching get_concept's real column shape."""
    return {
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "value": value,
        "fiscal_year": fiscal_year,
        "fiscal_period": "FY",
        "form": form,
        "filed": pd.Timestamp(filed),
        "tag": tag,
        "period_length": "annual",
    }


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
    # A derived-by-subtraction row has no separate filed Q4 to reconcile against.
    assert not row["revenue_q4_diverges_from_subtraction"]
    assert pd.isna(row["revenue_q4_subtraction_value"])


def test_msft_fy2016_q4_diverges_from_subtraction(msft_quarterly):
    # Discovered while validating this fix, not derived: MSFT's real filed FY2016 Q4 revenue
    # ($20.614B) disagrees with FY-(Q1+Q2+Q3) subtraction ($26.448B) by ~6.4% of FY total.
    # Root cause: the FY2016 annual total is tagged
    # RevenueFromContractWithCustomerExcludingAssessedTax from the FY2018 10-K's
    # ASC-606-restated comparative column (filed 2018-08-03), while the FY2016 quarters remain
    # tagged SalesRevenueNet from the FY2017 10-K (filed 2017-08-02) -- different vintages of
    # the same period, not an error in either figure.
    row = msft_quarterly.loc[msft_quarterly["period_end"] == pd.Timestamp("2016-06-30")].iloc[0]
    assert not row["revenue_is_derived"]
    assert row["revenue"] == pytest.approx(20_614_000_000)
    assert row["revenue_q4_diverges_from_subtraction"]
    assert row["revenue_q4_subtraction_value"] == pytest.approx(26_448_000_000)


def test_derive_q4_four_candidates_reconciles_without_adding_row():
    # 4 real quarterly candidates tiling a fiscal year means the 4th *is* Q4 -- a real filed
    # fact, not something to synthesize. FY2022 agrees exactly with subtraction; FY2023 diverges.
    qtr_df = pd.DataFrame([
        _qtr_row("2022-01-01", "2022-03-31", 100, "Q1"),
        _qtr_row("2022-04-01", "2022-06-30", 110, "Q2"),
        _qtr_row("2022-07-01", "2022-09-30", 105, "Q3"),
        _qtr_row("2022-10-01", "2022-12-31", 115, "Q4"),  # agrees: 430-(100+110+105)=115
        _qtr_row("2023-01-01", "2023-03-31", 120, "Q1", fiscal_year=2023),
        _qtr_row("2023-04-01", "2023-06-30", 130, "Q2", fiscal_year=2023),
        _qtr_row("2023-07-01", "2023-09-30", 125, "Q3", fiscal_year=2023),
        _qtr_row("2023-10-01", "2023-12-31", 140, "Q4", fiscal_year=2023),  # diverges: 530-375=155
    ])
    ann_df = pd.DataFrame([
        _fy_row("2022-01-01", "2022-12-31", 430, fiscal_year=2022),
        _fy_row("2023-01-01", "2023-12-31", 530, fiscal_year=2023),
    ])

    derived, reconciliation = _derive_q4(qtr_df, ann_df)

    assert derived.empty  # the 4th candidate is real -- nothing to synthesize

    recon_2022 = reconciliation.loc[reconciliation["period_end"] == pd.Timestamp("2022-12-31")].iloc[0]
    assert recon_2022["q4_subtraction_value"] == pytest.approx(115)
    assert not recon_2022["q4_diverges_from_subtraction"]

    recon_2023 = reconciliation.loc[reconciliation["period_end"] == pd.Timestamp("2023-12-31")].iloc[0]
    assert recon_2023["q4_subtraction_value"] == pytest.approx(155)
    assert recon_2023["q4_diverges_from_subtraction"]


def test_derive_q4_three_candidates_unchanged():
    qtr_df = pd.DataFrame([
        _qtr_row("2024-01-01", "2024-03-31", 150, "Q1"),
        _qtr_row("2024-04-01", "2024-06-30", 160, "Q2"),
        _qtr_row("2024-07-01", "2024-09-30", 155, "Q3"),
    ])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 630)])

    derived, reconciliation = _derive_q4(qtr_df, ann_df)

    assert len(derived) == 1
    row = derived.iloc[0]
    assert row["is_derived"]
    assert row["tag"] == "derived"
    assert row["value"] == pytest.approx(165)  # 630 - (150+160+155)
    assert reconciliation.empty


def test_get_statement_three_candidate_year_reconciliation_not_applicable(monkeypatch):
    # Confirms the not-applicable case flows all the way through get_statement's merge/fillna
    # steps, not just _derive_q4 in isolation: a derived row gets NaN/False, not left unset.
    qtr_df = pd.DataFrame([
        _qtr_row("2024-01-01", "2024-03-31", 150, "Q1"),
        _qtr_row("2024-04-01", "2024-06-30", 160, "Q2"),
        _qtr_row("2024-07-01", "2024-09-30", 155, "Q3"),
    ])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 630)])

    def fake_get_concept(ticker, concept_name, client=None, period_length=None):
        if concept_name != "revenue":
            raise ConceptNotFoundError(concept_name)
        return ann_df if period_length == "annual" else qtr_df

    monkeypatch.setattr("src.analysis.statements.get_concept", fake_get_concept)
    stmt = get_statement("TEST", "quarterly", client=object())

    row = stmt.loc[stmt["period_end"] == pd.Timestamp("2024-12-31")].iloc[0]
    assert row["revenue_is_derived"]
    assert pd.isna(row["revenue_q4_subtraction_value"])
    assert not row["revenue_q4_diverges_from_subtraction"]


def test_derive_q4_four_candidates_not_aligned_to_fy_end_refuses():
    # The last period_start-sorted candidate is a short stub that doesn't reach fy_end (e.g. a
    # stray/mis-tagged comparative fact landing inside the FY window rather than a real Q4) --
    # refuses exactly like the existing 3-candidate non-tiling case: no row, no reconciliation
    # entry, nothing crashes. (A literal same-period_end duplicate can't reach _derive_q4 at all
    # -- _dedupe_by_period_end collapses those before this function ever runs.)
    qtr_df = pd.DataFrame([
        _qtr_row("2025-01-01", "2025-03-31", 100, "Q1", fiscal_year=2025),
        _qtr_row("2025-04-01", "2025-06-30", 110, "Q2", fiscal_year=2025),
        _qtr_row("2025-07-01", "2025-09-30", 105, "Q3", fiscal_year=2025),
        _qtr_row("2025-10-01", "2025-11-15", 40, "Q4", fiscal_year=2025),
    ])
    ann_df = pd.DataFrame([_fy_row("2025-01-01", "2025-12-31", 400, fiscal_year=2025)])

    derived, reconciliation = _derive_q4(qtr_df, ann_df)

    assert derived.empty
    assert reconciliation.empty


def test_wmt_fy2010_q4_diverges_from_subtraction(client):
    stmt = get_statement("WMT", "quarterly", client=client)
    row = stmt.loc[stmt["period_end"] == pd.Timestamp("2010-01-31")].iloc[0]
    assert row["revenue"] == pytest.approx(112_826_000_000)
    assert not row["revenue_is_derived"]
    assert row["revenue_q4_subtraction_value"] == pytest.approx(115_779_000_000)
    assert row["revenue_q4_diverges_from_subtraction"]


def test_wmt_fy2013_q4_agrees_with_subtraction(client):
    stmt = get_statement("WMT", "quarterly", client=client)
    row = stmt.loc[stmt["period_end"] == pd.Timestamp("2013-01-31")].iloc[0]
    assert not row["revenue_q4_diverges_from_subtraction"]


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
            if col.endswith("_is_derived") or col.endswith("_q4_diverges_from_subtraction"):
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

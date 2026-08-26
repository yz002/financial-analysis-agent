import pandas as pd
import pytest

from src.analysis.statements import (
    DURATION_CONCEPTS,
    _derive_q4,
    _derive_total_liabilities,
    _derive_ytd_quarters,
    get_statement,
)
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


def _ytd_row(period_start, period_end, value, fiscal_period="Q2", fiscal_year=2024,
             form="10-Q", filed="2024-06-01", tag="RevTag"):
    """A single 'other'-classified (fiscal-year-to-date cumulative) duration row, matching
    get_concept's real column shape."""
    return {
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "value": value,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "form": form,
        "filed": pd.Timestamp(filed),
        "tag": tag,
        "period_length": "other",
    }


def test_ford_missing_gross_profit_is_nan(ford_quarterly):
    assert "gross_profit" in ford_quarterly.columns
    assert ford_quarterly["gross_profit"].isna().all()
    assert ford_quarterly["gross_profit_tag"].isna().all()


def test_stockholders_equity_tag_mixes_nci_for_ford_but_not_msft_nvda(
    ford_quarterly, msft_quarterly, nvda_quarterly
):
    # CONCEPTS["stockholders_equity"]'s two-tag priority list (StockholdersEquity, parent-only,
    # vs. StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest, NCI-inclusive)
    # means get_concept can source different periods' equity from different bases. Ford mixes
    # both tags across its history; MSFT/NVDA only ever report the plain, parent-only tag. See
    # get_ratios's roe-specific note (src/agent/tools.py) for how this surfaces to a caller.
    nci_tag = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"

    ford_tags = set(ford_quarterly["stockholders_equity_tag"].dropna())
    assert nci_tag in ford_tags
    assert "StockholdersEquity" in ford_tags

    for stmt in (msft_quarterly, nvda_quarterly):
        assert set(stmt["stockholders_equity_tag"].dropna()) == {"StockholdersEquity"}


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
    assert row["derivation_method"] == "q1q2q3_subtraction"
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
    monkeypatch.setattr("src.analysis.statements.get_cik", lambda ticker, client=None: "0000000000")
    monkeypatch.setattr(
        "src.analysis.statements.get_company_name", lambda ticker, client=None: "Test Corp"
    )
    stmt = get_statement("TEST", "quarterly", client=object())

    row = stmt.loc[stmt["period_end"] == pd.Timestamp("2024-12-31")].iloc[0]
    assert row["revenue_is_derived"]
    assert row["revenue_derivation_method"] == "q1q2q3_subtraction"
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


# --- YTD-chain quarter derivation (Q2 = H1-Q1, Q3 = 9M-H1, Q4 = FY-9M) -----------------------
#
# Shared fixture shape across these tests: Q1 = 2024-01-01..2024-03-31 (90d), H1 =
# 2024-01-01..2024-06-30 (181d), 9-month = 2024-01-01..2024-09-30 (273d), FY =
# 2024-01-01..2024-12-31 (365d) -- implied Q2 = Apr1-Jun30 (91d), implied Q3 = Jul1-Sep30 (92d),
# implied Q4 = Oct1-Dec31 (92d), all within their respective bound checks.


def test_derive_ytd_quarters_derives_q2_q3_q4_when_real_quarters_absent():
    qtr_df = pd.DataFrame([_qtr_row("2024-01-01", "2024-03-31", 100, "Q1")])
    qtr_df["is_derived"] = False
    ytd_df = pd.DataFrame([
        _ytd_row("2024-01-01", "2024-06-30", 220, "Q2"),
        _ytd_row("2024-01-01", "2024-09-30", 345, "Q3"),
    ])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert len(result) == 3
    assert set(result["derivation_method"]) == {"ytd_chain"}
    assert set(result["is_derived"]) == {True}

    q2 = result.loc[result["fiscal_period"] == "Q2"].iloc[0]
    assert q2["period_end"] == pd.Timestamp("2024-06-30")
    assert q2["value"] == pytest.approx(120)  # 220 - 100

    q3 = result.loc[result["fiscal_period"] == "Q3"].iloc[0]
    assert q3["period_end"] == pd.Timestamp("2024-09-30")
    assert q3["value"] == pytest.approx(125)  # 345 - 220

    q4 = result.loc[result["fiscal_period"] == "Q4"].iloc[0]
    assert q4["period_end"] == pd.Timestamp("2024-12-31")
    assert q4["value"] == pytest.approx(135)  # 480 - 345


def test_derive_ytd_quarters_q2_not_derived_when_real_quarter_exists():
    qtr_df = pd.DataFrame([
        _qtr_row("2024-01-01", "2024-03-31", 100, "Q1"),
        _qtr_row("2024-04-01", "2024-06-30", 121, "Q2"),
    ])
    qtr_df["is_derived"] = False
    ytd_df = pd.DataFrame([
        _ytd_row("2024-01-01", "2024-06-30", 220, "Q2"),
        _ytd_row("2024-01-01", "2024-09-30", 345, "Q3"),
    ])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert "Q2" not in set(result["fiscal_period"])
    assert set(result["fiscal_period"]) == {"Q3", "Q4"}


def test_derive_ytd_quarters_q3_not_derived_when_real_quarter_exists():
    qtr_df = pd.DataFrame([
        _qtr_row("2024-01-01", "2024-03-31", 100, "Q1"),
        _qtr_row("2024-07-01", "2024-09-30", 126, "Q3"),
    ])
    qtr_df["is_derived"] = False
    ytd_df = pd.DataFrame([
        _ytd_row("2024-01-01", "2024-06-30", 220, "Q2"),
        _ytd_row("2024-01-01", "2024-09-30", 345, "Q3"),
    ])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert "Q3" not in set(result["fiscal_period"])
    assert set(result["fiscal_period"]) == {"Q2", "Q4"}


def test_derive_ytd_quarters_q4_not_derived_when_q1q2q3_subtraction_already_resolved():
    # Build qtr_df the way get_statement actually does: run _derive_q4 first and concat its
    # output on, so this exercises the real precedence check (full qtr_df, not just real rows).
    qtr_df = pd.DataFrame([
        _qtr_row("2024-01-01", "2024-03-31", 150, "Q1"),
        _qtr_row("2024-04-01", "2024-06-30", 160, "Q2"),
        _qtr_row("2024-07-01", "2024-09-30", 155, "Q3"),
    ])
    qtr_df["is_derived"] = False
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 630)])
    derived, _reconciliation = _derive_q4(qtr_df, ann_df)
    qtr_df = pd.concat([qtr_df, derived], ignore_index=True)

    ytd_df = pd.DataFrame([_ytd_row("2024-01-01", "2024-09-30", 465, "Q3")])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert result.empty  # Q4 already resolved by subtraction; no H1 present for Q2/Q3 either


def test_derive_ytd_quarters_q4_not_derived_when_real_q4_fact_already_exists():
    qtr_df = pd.DataFrame([
        _qtr_row("2022-01-01", "2022-03-31", 100, "Q1", fiscal_year=2022),
        _qtr_row("2022-04-01", "2022-06-30", 110, "Q2", fiscal_year=2022),
        _qtr_row("2022-07-01", "2022-09-30", 105, "Q3", fiscal_year=2022),
        _qtr_row("2022-10-01", "2022-12-31", 115, "Q4", fiscal_year=2022),
    ])
    qtr_df["is_derived"] = False
    ann_df = pd.DataFrame([_fy_row("2022-01-01", "2022-12-31", 430, fiscal_year=2022)])
    ytd_df = pd.DataFrame([_ytd_row("2022-01-01", "2022-09-30", 315, "Q3", fiscal_year=2022)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    # No H1 fact provided, so Q2/Q3 can't derive either -- Q4 is the only thing that could have
    # fired here, and it's blocked by the real filed Q4 fact already in qtr_df.
    assert result.empty


def test_derive_ytd_quarters_refuses_on_bad_implied_span():
    # Q1 is deliberately not a plausible quarter length, to isolate the implied-span check.
    qtr_df = pd.DataFrame([_qtr_row("2024-01-01", "2024-01-31", 100, "Q1")])
    qtr_df["is_derived"] = False
    ytd_df = pd.DataFrame([_ytd_row("2024-01-01", "2024-06-30", 220, "Q2")])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert result.empty  # implied Q2 span (Feb1-Jun30, 150d) is outside the mid-quarter bounds


def test_derive_ytd_quarters_refuses_on_ambiguous_h1_candidates():
    qtr_df = pd.DataFrame([_qtr_row("2024-01-01", "2024-03-31", 100, "Q1")])
    qtr_df["is_derived"] = False
    ytd_df = pd.DataFrame([
        _ytd_row("2024-01-01", "2024-06-24", 215, "Q2"),  # 175d, also in the H1 bucket
        _ytd_row("2024-01-01", "2024-06-30", 220, "Q2"),  # 181d
    ])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert result.empty  # two H1 candidates -- refuse rather than guess which one is real


def test_derive_ytd_quarters_refuses_when_h1_start_off_tolerance():
    qtr_df = pd.DataFrame([_qtr_row("2024-01-01", "2024-03-31", 100, "Q1")])
    qtr_df["is_derived"] = False
    # period_start is 14 days off fy_start -- well outside _TILE_TOLERANCE_DAYS.
    ytd_df = pd.DataFrame([_ytd_row("2024-01-15", "2024-07-14", 220, "Q2")])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)])

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert result.empty


def test_derive_ytd_quarters_empty_when_no_annual_years():
    qtr_df = pd.DataFrame([_qtr_row("2024-01-01", "2024-03-31", 100, "Q1")])
    qtr_df["is_derived"] = False
    ytd_df = pd.DataFrame([_ytd_row("2024-01-01", "2024-06-30", 220, "Q2")])
    ann_df = pd.DataFrame([_fy_row("2024-01-01", "2024-12-31", 480)]).iloc[0:0]

    result = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)

    assert result.empty


def test_derivation_method_consistent_with_is_derived(msft_quarterly, nvda_quarterly, ford_quarterly):
    # derivation_method must be None exactly where is_derived is False, and one of the two known
    # method strings exactly where is_derived is True -- never left unset/misaligned by the
    # concat/merge steps that assemble each concept's column.
    for stmt in (msft_quarterly, nvda_quarterly, ford_quarterly):
        for concept in DURATION_CONCEPTS:
            is_derived = stmt[f"{concept}_is_derived"]
            method = stmt[f"{concept}_derivation_method"]
            assert method[~is_derived].isna().all()
            assert method[is_derived].isin(["q1q2q3_subtraction", "ytd_chain"]).all()


def test_nvda_fy2022_capex_ytd_chain_derivation(nvda_quarterly):
    # NVDA files capex almost entirely as YTD cumulative facts (see NOTES.md) -- FY2022 (fiscal
    # year ended 2022-01-30) has only a real filed Q1 ($298M); Q2/Q3/Q4 exist only as
    # H1/9-month/FY cumulative facts, recoverable only via this YTD chain.
    expected = {
        pd.Timestamp("2021-08-01"): (183_000_000, "Q2"),
        pd.Timestamp("2021-10-31"): (222_000_000, "Q3"),
        pd.Timestamp("2022-01-30"): (273_000_000, "Q4"),
    }
    for period_end, (expected_value, expected_fp) in expected.items():
        row = nvda_quarterly.loc[nvda_quarterly["period_end"] == period_end].iloc[0]
        assert row["capex"] == pytest.approx(expected_value)
        assert row["capex_is_derived"]
        assert row["capex_derivation_method"] == "ytd_chain"

    q1 = nvda_quarterly.loc[nvda_quarterly["period_end"] == pd.Timestamp("2021-05-02")].iloc[0]
    assert q1["capex"] == pytest.approx(298_000_000)
    assert not q1["capex_is_derived"]
    assert q1["capex_derivation_method"] is None


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
            if (
                col.endswith("_is_derived")
                or col.endswith("_q4_diverges_from_subtraction")
                or col.endswith("_diverges_from_alt")
            ):
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
    # Scoped to duration concepts: Q4-by-subtraction and YTD-chain derivation are quarterly-only
    # concerns (a fiscal year's own annual row needs no Q4 synthesized from itself). total_liabilities's
    # is_derived is deliberately excluded -- its sum-of-parts/identity fallback is period_length-
    # independent, so it can legitimately be True in annual mode too (e.g. a period lacking a
    # direct us-gaap:Liabilities tag) -- see statements.py's _derive_total_liabilities.
    derived_cols = [f"{c}_is_derived" for c in DURATION_CONCEPTS]
    for col in derived_cols:
        assert not msft_annual[col].any()


# --- total_liabilities fallback derivation -----------------------------------------------------


def _liabilities_stmt_row(
    period_end="2024-01-31",
    tl=float("nan"), tl_tag=None, tl_filed=pd.NaT,
    cl=float("nan"), cl_filed=pd.NaT,
    ln=float("nan"), ln_filed=pd.NaT,
    ta=float("nan"), ta_filed=pd.NaT,
    se=float("nan"), se_filed=pd.NaT,
):
    """One row of the minimal column set _derive_total_liabilities reads."""
    return {
        "period_end": pd.Timestamp(period_end),
        "total_liabilities": tl,
        "total_liabilities_tag": tl_tag,
        "total_liabilities_filed": pd.Timestamp(tl_filed) if pd.notna(tl_filed) else pd.NaT,
        "current_liabilities": cl,
        "current_liabilities_filed": pd.Timestamp(cl_filed) if pd.notna(cl_filed) else pd.NaT,
        "liabilities_noncurrent": ln,
        "liabilities_noncurrent_filed": pd.Timestamp(ln_filed) if pd.notna(ln_filed) else pd.NaT,
        "total_assets": ta,
        "total_assets_filed": pd.Timestamp(ta_filed) if pd.notna(ta_filed) else pd.NaT,
        "stockholders_equity": se,
        "stockholders_equity_filed": pd.Timestamp(se_filed) if pd.notna(se_filed) else pd.NaT,
    }


def test_derive_total_liabilities_tier_b_current_plus_noncurrent():
    stmt = pd.DataFrame(
        [_liabilities_stmt_row(cl=600, cl_filed="2024-03-01", ln=400, ln_filed="2024-02-01")]
    )
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert row["total_liabilities"] == 1000
    assert row["total_liabilities_tag"] == "derived"
    assert row["total_liabilities_filed"] == pd.Timestamp("2024-03-01")
    assert row["total_liabilities_is_derived"]
    assert row["total_liabilities_derivation_method"] == "current_plus_noncurrent_sum"


def test_derive_total_liabilities_tier_c_assets_minus_equity():
    # Only current_liabilities present (no noncurrent) -- tier b can't fire, falls to tier c.
    stmt = pd.DataFrame(
        [_liabilities_stmt_row(cl=600, cl_filed="2024-03-01", ta=1000, ta_filed="2024-04-01",
                                se=400, se_filed="2024-05-01")]
    )
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert row["total_liabilities"] == 600
    assert row["total_liabilities_tag"] == "derived"
    assert row["total_liabilities_filed"] == pd.Timestamp("2024-05-01")
    assert row["total_liabilities_is_derived"]
    assert row["total_liabilities_derivation_method"] == "assets_minus_equity_identity"


def test_derive_total_liabilities_tier_c_when_only_noncurrent_present():
    # Mirror of the tier-c test above: liabilities_noncurrent present, current_liabilities
    # genuinely absent (NaN, not 0) -- tier b's `pd.notna(cur) and pd.notna(noncur)` gate must
    # fail on the missing cur alone, falling through to tier c, not silently treat the missing
    # current_liabilities as 0 and sum noncur+0. Values are chosen so a buggy "treat missing as
    # 0" tier-b computation (400 + 0 = 400) would produce a numerically different result from the
    # correct tier-c identity (1000 - 250 = 750), so the assertion on total_liabilities itself --
    # not just derivation_method -- would catch that bug too.
    stmt = pd.DataFrame(
        [_liabilities_stmt_row(ln=400, ln_filed="2024-02-01", ta=1000, ta_filed="2024-04-01",
                                se=250, se_filed="2024-05-01")]
    )
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert row["total_liabilities"] == 750
    assert row["total_liabilities_tag"] == "derived"
    assert row["total_liabilities_filed"] == pd.Timestamp("2024-05-01")
    assert row["total_liabilities_is_derived"]
    assert row["total_liabilities_derivation_method"] == "assets_minus_equity_identity"


def test_derive_total_liabilities_refuses_with_no_partial_credit():
    # Only one of current/noncurrent AND only one of assets/equity present -- neither tier
    # has both its required inputs, so no fallback fires and no partial sum is reported.
    stmt = pd.DataFrame([_liabilities_stmt_row(cl=600, cl_filed="2024-03-01", ta=1000, ta_filed="2024-04-01")])
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert pd.isna(row["total_liabilities"])
    assert row["total_liabilities_tag"] is None
    assert pd.isna(row["total_liabilities_filed"])
    assert not row["total_liabilities_is_derived"]
    assert row["total_liabilities_derivation_method"] is None


def test_derive_total_liabilities_alt_divergence_flags():
    stmt = pd.DataFrame(
        [_liabilities_stmt_row(tl=1000, tl_tag="Liabilities", tl_filed="2024-03-01",
                                ta=1200, ta_filed="2024-04-01", se=100, se_filed="2024-05-01")]
    )
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert row["total_liabilities"] == 1000
    assert row["total_liabilities_derivation_method"] == "direct_tag"
    assert not row["total_liabilities_is_derived"]
    assert row["total_liabilities_alt_value"] == 1100
    assert row["total_liabilities_alt_method"] == "assets_minus_equity_identity"
    assert row["total_liabilities_diverges_from_alt"]


def test_derive_total_liabilities_alt_agrees_within_tolerance():
    stmt = pd.DataFrame(
        [_liabilities_stmt_row(tl=1000, tl_tag="Liabilities", tl_filed="2024-03-01",
                                ta=1400, ta_filed="2024-04-01", se=401, se_filed="2024-05-01")]
    )
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert row["total_liabilities_alt_value"] == 999
    assert not row["total_liabilities_diverges_from_alt"]


def test_derive_total_liabilities_alt_prefers_tier_b_over_tier_c():
    stmt = pd.DataFrame(
        [_liabilities_stmt_row(
            tl=1000, tl_tag="Liabilities", tl_filed="2024-03-01",
            cl=600, cl_filed="2024-02-01", ln=400, ln_filed="2024-01-01",
            ta=1200, ta_filed="2024-04-01", se=100, se_filed="2024-05-01",
        )]
    )
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert row["total_liabilities_alt_method"] == "current_plus_noncurrent_sum"
    assert row["total_liabilities_alt_value"] == 1000


def test_derive_total_liabilities_no_alt_when_neither_available():
    stmt = pd.DataFrame([_liabilities_stmt_row(tl=1000, tl_tag="Liabilities", tl_filed="2024-03-01")])
    result = _derive_total_liabilities(stmt)
    row = result.iloc[0]
    assert pd.isna(row["total_liabilities_alt_value"])
    assert row["total_liabilities_alt_method"] is None
    assert not row["total_liabilities_diverges_from_alt"]


def test_wmt_total_liabilities_derived_via_assets_minus_equity_identity(client):
    stmt = get_statement("WMT", "quarterly", client=client)
    assert stmt["total_liabilities"].notna().all()
    assert (stmt["total_liabilities_derivation_method"] == "assets_minus_equity_identity").all()
    assert stmt["total_liabilities_is_derived"].all()
    assert (stmt["total_liabilities_tag"] == "derived").all()

    fy2025 = stmt.loc[stmt["period_end"] == pd.Timestamp("2025-01-31")].iloc[0]
    assert fy2025["total_liabilities"] == pytest.approx(163_402_000_000, rel=1e-3)


def test_msft_nvda_ford_total_liabilities_direct_tag(msft_quarterly, nvda_quarterly, ford_quarterly):
    # All three report us-gaap:Liabilities directly in recent history, though a company's
    # earliest XBRL-era quarters can predate that tag and fall back to the identity (confirmed
    # for MSFT's own 2009 annual row) -- so this checks the most recent period, not every period.
    for stmt in (msft_quarterly, nvda_quarterly, ford_quarterly):
        latest = stmt.iloc[-1]
        assert latest["total_liabilities_derivation_method"] == "direct_tag"
        assert not latest["total_liabilities_is_derived"]
        direct_rows = stmt.loc[stmt["total_liabilities_derivation_method"] == "direct_tag"]
        assert not direct_rows["total_liabilities_is_derived"].any()


def test_msft_total_liabilities_diverges_from_alt_2016(msft_quarterly):
    row = msft_quarterly.loc[msft_quarterly["period_end"] == pd.Timestamp("2016-06-30")].iloc[0]
    assert row["total_liabilities_derivation_method"] == "direct_tag"
    assert row["total_liabilities_diverges_from_alt"]
    assert row["total_liabilities_alt_method"] == "assets_minus_equity_identity"


def test_total_liabilities_derivation_method_consistent_with_is_derived(
    msft_quarterly, nvda_quarterly, ford_quarterly
):
    # Unlike DURATION_CONCEPTS (where is_derived=False always pairs with derivation_method=None
    # -- see test_derivation_method_consistent_with_is_derived), total_liabilities's is_derived=
    # False legitimately pairs with derivation_method="direct_tag" for a real filed value, so
    # this invariant is checked separately rather than folded into that generic test.
    for stmt in (msft_quarterly, nvda_quarterly, ford_quarterly):
        derived = stmt["total_liabilities_is_derived"]
        method = stmt["total_liabilities_derivation_method"]
        assert method[derived].isin(["current_plus_noncurrent_sum", "assets_minus_equity_identity"]).all()
        assert method[~derived].isin([None, "direct_tag"]).all()


def test_sorted_by_period_end(msft_quarterly):
    assert msft_quarterly["period_end"].is_monotonic_increasing


def test_unknown_period_length_raises(client):
    with pytest.raises(ValueError):
        get_statement("MSFT", "monthly", client=client)


# --- sparse-history / successor-registrant detection ------------------------
#
# See statements.py's `_MIN_PLAUSIBLE_PERIODS` docstring and NOTES.md: SEC's ticker-to-CIK map
# can repoint a ticker at a newly registered successor entity after a merger, reorganization, or
# redomiciliation (confirmed real case: ExxonMobil's 2026-07-01 Texas redomiciliation created
# "ExxonMobil Holdings Corp", CIK 2115436, as XOM's SEC Rule 12g-3(a) successor registrant). That
# successor inherits the ticker but starts with none of the predecessor's XBRL history, which
# would otherwise look just like a data gap. This is a general class of problem, not an XOM
# quirk -- these tests exercise the detection mechanism generically, with one live confirmatory
# case at the end for the specific ticker that surfaced it.


def test_is_sparse_history_below_threshold():
    from src.analysis.statements import _is_sparse_history

    assert _is_sparse_history("quarterly", 2) is True
    assert _is_sparse_history("quarterly", 7) is True
    assert _is_sparse_history("annual", 2) is True


def test_is_sparse_history_at_or_above_threshold_not_sparse():
    from src.analysis.statements import _is_sparse_history

    assert _is_sparse_history("quarterly", 8) is False
    assert _is_sparse_history("quarterly", 76) is False
    assert _is_sparse_history("annual", 3) is False


def test_sparse_history_note_names_entity_and_cik():
    from src.analysis.statements import _sparse_history_note

    note = _sparse_history_note("zzzz", "0009999999", "Thin Successor Corp", "quarterly", 2)
    assert "ZZZZ" in note
    assert "0009999999" in note
    assert "Thin Successor Corp" in note
    assert "2 quarterly period" in note
    assert "12g-3" in note


def test_get_statement_flags_sparse_history_for_thin_synthetic_entity(monkeypatch, client):
    # A synthetic entity with only 2 real quarterly revenue periods and nothing else -- the same
    # shape as XOM's new CIK today (see the live test below), but decoupled from real EDGAR data
    # so this test doesn't silently stop exercising the "thin" case once ExxonMobil Holdings
    # Corp's own history grows past the threshold.
    qtr_df = pd.DataFrame([
        _qtr_row("2025-04-01", "2025-06-30", 81_506_000_000, "Q2", 2026, filed="2026-08-03"),
        _qtr_row("2026-04-01", "2026-06-30", 116_017_000_000, "Q2", 2026, filed="2026-08-03"),
    ])

    def fake_try_get_concept(ticker, concept, period_length, client):
        if concept == "revenue" and period_length == "quarterly":
            return qtr_df
        return None

    monkeypatch.setattr("src.analysis.statements._try_get_concept", fake_try_get_concept)
    monkeypatch.setattr(
        "src.analysis.statements.get_cik", lambda ticker, client=None: "0002115436"
    )
    monkeypatch.setattr(
        "src.analysis.statements.get_company_name",
        lambda ticker, client=None: "Thin Successor Corp",
    )

    stmt = get_statement("ZZZZ", "quarterly", client=client)

    assert stmt.attrs["periods_available"] == 2
    assert stmt.attrs["sparse_history"] is True
    assert stmt.attrs["cik"] == "0002115436"
    assert stmt.attrs["entity_name"] == "Thin Successor Corp"
    assert "Thin Successor Corp" in stmt.attrs["sparse_history_note"]
    assert "predecessor" in stmt.attrs["sparse_history_note"]


def test_msft_full_history_not_flagged_sparse(msft_quarterly):
    # Negative case: an established filer with 76 quarters on file must not be flagged.
    assert msft_quarterly.attrs["sparse_history"] is False
    assert msft_quarterly.attrs["sparse_history_note"] is None
    assert msft_quarterly.attrs["periods_available"] >= 8
    assert msft_quarterly.attrs["cik"] == "0000789019"


def test_xom_successor_registrant_sparse_history(client):
    # Real, confirmed case (see NOTES.md): ExxonMobil redomiciled from New Jersey to Texas on
    # 2026-07-01, and SEC's ticker map now resolves XOM to "ExxonMobil Holdings Corp"
    # (CIK 2115436) -- a Rule 12g-3(a) successor registrant with, as of this writing, a single
    # 10-Q on file (2 real quarterly revenue periods), despite Exxon being a household name with
    # a decades-long filing history under a different (predecessor) CIK. This assertion will need
    # loosening once ExxonMobil Holdings Corp accumulates 8+ quarters of its own history
    # (expected 2027+) -- at that point it correctly stops being "sparse" by this heuristic,
    # which is the intended behavior, not a regression.
    stmt = get_statement("XOM", "quarterly", client=client)

    assert stmt.attrs["sparse_history"] is True
    assert stmt.attrs["cik"] == "0002115436"
    assert "ExxonMobil" in stmt.attrs["entity_name"]
    assert "0002115436" in stmt.attrs["sparse_history_note"]
    assert "12g-3" in stmt.attrs["sparse_history_note"]

import pandas as pd
import pytest

from src.data.concepts import _reclassify_long_opening_quarters, get_concept


def _entry(start, end):
    """A minimal duration-fact dict, matching the shape _reclassify_long_opening_quarters reads."""
    return {"start": start, "end": end}


def test_long_opening_quarter_reclassified():
    # Kroger's real pattern: a ~16-week (111-day) Q1 whose start coincides with the fiscal
    # year's own annual fact, with nothing shorter sharing that start.
    deduped = [
        _entry("2020-02-02", "2020-05-23"),  # 111-day Q1 -- currently "other"
        _entry("2020-02-02", "2021-01-30"),  # annual FY, same start
    ]
    lengths = ["other", "annual"]
    result = _reclassify_long_opening_quarters(deduped, lengths)
    assert result == ["quarterly", "annual"]


def test_long_closing_quarter_not_reclassified():
    # Costco's real pattern: a ~111-118 day Q4 whose start does NOT coincide with the fiscal
    # year's annual fact (it starts well after the year began, ending where the year ends).
    # This must stay "other" here -- statements.py's discrete-Q4-fact path already handles it,
    # and reclassifying it here would double up with that (already-correct) mechanism.
    deduped = [
        _entry("2008-09-01", "2009-08-30"),  # annual FY, starts well before the Q4 candidate
        _entry("2009-05-11", "2009-08-30"),  # 111-day Q4 -- shares the annual fact's END, not start
    ]
    lengths = ["annual", "other"]
    result = _reclassify_long_opening_quarters(deduped, lengths)
    assert result == ["annual", "other"]


def test_long_ytd_sharing_opening_start_not_reclassified():
    # A cumulative YTD-through-Q2 fact starts at the same fiscal-year start as a genuine long Q1,
    # so it also "opens the fiscal year" by that signal alone -- it must lose out to the shorter
    # fact from the same start, not get promoted just for matching the annual fact's start.
    deduped = [
        _entry("2020-02-02", "2020-05-23"),  # 111-day Q1 -- the real quarter
        _entry("2020-02-02", "2020-08-15"),  # 195-day H1 YTD, same start, longer
        _entry("2020-02-02", "2021-01-30"),  # annual FY, same start
    ]
    lengths = ["other", "other", "annual"]
    result = _reclassify_long_opening_quarters(deduped, lengths)
    assert result == ["quarterly", "other", "annual"]


def test_long_opening_quarter_without_annual_fact_stays_other():
    # The newest, not-yet-closed fiscal year has no annual fact in the data yet, so there's
    # nothing to confirm this candidate actually opens a fiscal year against. Refuses rather
    # than guess -- matches this codebase's general stance (see statements._derive_q4) of not
    # classifying a period without the corroborating fact to justify it.
    deduped = [_entry("2026-02-01", "2026-05-23")]  # 111-day Q1, no annual sibling yet
    lengths = ["other"]
    result = _reclassify_long_opening_quarters(deduped, lengths)
    assert result == ["other"]


def test_already_tight_window_quarter_unaffected():
    # A normal ~83-day quarter is untouched by this function -- it was already "quarterly"
    # from the base day-span classification, so there's nothing for this pass to change.
    deduped = [_entry("2020-05-24", "2020-08-15")]
    lengths = ["quarterly"]
    result = _reclassify_long_opening_quarters(deduped, lengths)
    assert result == ["quarterly"]


def test_ordinary_ytd_fact_far_outside_window_unaffected():
    # A 9-month (279-day) YTD fact is nowhere near _LONG_OPENING_QUARTER_DAYS_MAX and must stay
    # "other" regardless of what else it shares a start with.
    deduped = [
        _entry("2020-02-02", "2020-11-07"),  # 279-day 9-month YTD
        _entry("2020-02-02", "2021-01-30"),  # annual FY, same start
    ]
    lengths = ["other", "annual"]
    result = _reclassify_long_opening_quarters(deduped, lengths)
    assert result == ["other", "annual"]


def test_kr_q1_classified_quarterly(client):
    # Kroger's fiscal Q1 genuinely spans ~16 weeks (111 days) rather than the ~13 weeks
    # (80-100 days) _QUARTERLY_DAYS_MIN/MAX expects -- confirmed against Kroger's own press
    # release: total company sales of $45.3B for the quarter ended 2024-05-25
    # (https://ir.kroger.com/news/news-details/2024/Kroger-Reports-First-Quarter-2024-Results-and-Reaffirms-Guidance/default.aspx).
    df = get_concept("KR", "revenue", client=client, period_length="quarterly")
    row = df.loc[df["period_end"] == pd.Timestamp("2024-05-25")].iloc[0]
    assert row["value"] == pytest.approx(45_269_000_000)
    assert (row["period_end"] - row["period_start"]).days == 111


def test_kr_quarterly_revenue_has_broad_history(client):
    # Before this fix, every one of Kroger's ~111-day Q1 periods was misclassified "other", so
    # no analysis keyed on period_length="quarterly" ever saw one -- confirmed by day span
    # rather than fiscal_period, since fiscal_period is filing-attribution and, for Kroger's
    # older 10-K-era Q1 facts, reads "FY" rather than "Q1" (see concepts.py's module docstring
    # on why period_end, not fiscal_year/fiscal_period, is this project's time key).
    df = get_concept("KR", "revenue", client=client, period_length="quarterly")
    days = (df["period_end"] - df["period_start"]).dt.days
    assert (days > 100).sum() > 10


def test_kr_full_statement_does_not_raise(client):
    from src.analysis.statements import get_statement

    get_statement("KR", "quarterly", client=client)


def test_cost_q4_classification_unchanged(client):
    # Costco's long (~111-118 day) Q4 is a different case from Kroger's long Q1 (see the module
    # docstring on _reclassify_long_opening_quarters) and must stay "other" here -- its real
    # filed value is already surfaced quarterly via statements.py's discrete-Q4-fact path.
    df = get_concept("COST", "revenue", client=client)
    days = (df["period_end"] - df["period_start"]).dt.days
    long_facts = df[(days > 100) & (days <= 125)]
    assert not long_facts.empty
    assert (long_facts["period_length"] == "other").all()

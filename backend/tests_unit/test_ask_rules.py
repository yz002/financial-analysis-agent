"""Pure tests for app/ask_rules.py -- no database, no HTTP."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import ask_rules
from app.ask_rules import (
    DONE,
    FAILED,
    LOST,
    MISMATCH,
    NEEDS_RECONFIRM,
    REUSED,
    RUNNING,
    UNBOUND,
    UNEXPECTED,
    USE,
    binding_decision,
    replay_state,
    request_fingerprint,
    statement_problem,
)

_GOOD_ATTRS = {
    "entity_name": "Acme",
    "csv_source": {"filename": "a.csv", "cadence": "quarterly", "scale": "thousands", "currency": None},
}
_DATA = [{"period_end": "2024-03-31", "revenue": 1.0}]


# --- statement_problem ---------------------------------------------------------------------


def test_usable_statement_has_no_problem():
    assert statement_problem("confirmed", _DATA, _GOOD_ATTRS) is None


def test_null_currency_is_fine_because_it_means_not_specified():
    attrs = {**_GOOD_ATTRS, "csv_source": {**_GOOD_ATTRS["csv_source"], "currency": None}}
    assert statement_problem("confirmed", _DATA, attrs) is None


@pytest.mark.parametrize(
    "status, data, attrs",
    [
        ("unconfirmed", _DATA, _GOOD_ATTRS),
        ("confirmed", None, _GOOD_ATTRS),  # legacy: no statement_data
        ("confirmed", [], _GOOD_ATTRS),
        ("confirmed", _DATA, None),  # legacy: no statement_attrs
        ("confirmed", _DATA, {**_GOOD_ATTRS, "entity_name": None}),
        ("confirmed", _DATA, {"entity_name": "Acme"}),  # no csv_source
        # Confirmed before scale/currency existed: never read as "ones".
        ("confirmed", _DATA, {"entity_name": "Acme", "csv_source": {"cadence": "quarterly"}}),
        ("confirmed", _DATA, {"entity_name": "Acme", "csv_source": {"scale": "ones"}}),  # no currency key
        ("confirmed", _DATA, {"entity_name": "Acme", "csv_source": {"scale": "dozens", "currency": None}}),
    ],
)
def test_unusable_statements_are_refused(status, data, attrs):
    assert statement_problem(status, data, attrs) is not None


def test_known_scales_match_what_confirm_accepts():
    from src.analysis.csv_statement import SCALE_FACTORS

    assert ask_rules.KNOWN_SCALES == set(SCALE_FACTORS)


# --- binding_decision ----------------------------------------------------------------------

X, Y = uuid.uuid4(), uuid.uuid4()


def _never():
    raise AssertionError("had_csv_turns must only be consulted for a pre-0005 conversation")


@pytest.mark.parametrize(
    "bound, fk, requested, expected",
    [
        (X, X, None, (USE, X)),  # omitted: the bound statement is used
        (X, X, X, (USE, X)),
        (X, X, Y, (MISMATCH, None)),  # never switched silently
        (X, None, None, (NEEDS_RECONFIRM, None)),  # bound statement deleted
        (X, None, Y, (NEEDS_RECONFIRM, None)),  # deleted wins over a mismatch
        (None, X, None, (USE, X)),  # pre-0005, statement still there
        (None, X, Y, (MISMATCH, None)),
    ],
)
def test_binding_table(bound, fk, requested, expected):
    decision = binding_decision(bound, fk, requested, _never)
    assert (decision.kind, decision.statement_id) == expected


def test_pre_0005_conversation_that_used_csv_tools_is_needs_reconfirm():
    decision = binding_decision(None, None, None, lambda: True)
    assert decision.kind == NEEDS_RECONFIRM


def test_never_bound_conversation_stays_unbound_and_refuses_a_statement():
    assert binding_decision(None, None, None, lambda: False).kind == UNBOUND
    assert binding_decision(None, None, Y, lambda: False).kind == MISMATCH


# --- replay_state ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
FP = request_fingerprint("Q?", None, None)


def _state(outcome, turn_id=None, age=timedelta(minutes=1), stored_fp=FP, fp=FP):
    return replay_state(outcome, turn_id, NOW - age, stored_fp, fp, NOW)


def test_running():
    assert _state("in_progress") == RUNNING


def test_lost_only_after_the_stale_cutoff():
    assert _state("in_progress", age=ask_rules.STALE_IN_PROGRESS_AFTER) == RUNNING
    assert _state("in_progress", age=ask_rules.STALE_IN_PROGRESS_AFTER + timedelta(seconds=1)) == LOST


def test_done_needs_a_turn():
    assert _state("answered", turn_id=uuid.uuid4()) == DONE
    assert _state("hit_iteration_cap", turn_id=uuid.uuid4()) == DONE


def test_failed():
    assert _state("error") == FAILED


@pytest.mark.parametrize(
    "outcome, turn_id",
    [
        ("answered", None),  # done with no turn
        ("hit_iteration_cap", None),
        ("rejected_daily_cap", None),  # /v1/ask never stores a request_id on a 429 row
        ("mapping_proposal", None),
        ("something_new", None),
    ],
)
def test_anything_else_is_unexpected(outcome, turn_id):
    assert _state(outcome, turn_id=turn_id) == UNEXPECTED


def test_fingerprint_mismatch_is_checked_first():
    other = request_fingerprint("A different question", None, None)
    assert _state("answered", turn_id=uuid.uuid4(), fp=other) == REUSED
    assert _state("in_progress", fp=other) == REUSED


def test_fingerprint_covers_the_whole_request():
    base = request_fingerprint("Q?", None, None)
    assert request_fingerprint("Q?", None, None) == base
    assert request_fingerprint("Q?", str(X), None) != base
    assert request_fingerprint("Q?", None, str(X)) != base
    assert request_fingerprint("Q!", None, None) != base


def test_stale_cutoff_sits_above_the_run_budget():
    from src.agent.agent import RUN_BUDGET_SECONDS

    assert ask_rules.STALE_IN_PROGRESS_AFTER.total_seconds() >= RUN_BUDGET_SECONDS + 10 * 60

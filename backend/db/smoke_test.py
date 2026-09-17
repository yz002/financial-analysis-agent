"""
One-off smoke test for session 1: confirm the initial Alembic migration actually
applied to the real database and produced the expected shape. Not a permanent
pytest suite -- this session's scope is "confirm the migration applied
successfully," not test infrastructure (see the session-1 plan). Run directly:

    backend/.venv/Scripts/python.exe -m db.smoke_test

from inside backend/, with DATABASE_URL set in the environment (or backend/.env).
"""

import sys

from sqlalchemy import inspect, text

from .base import get_engine

EXPECTED_TABLES = {
    "accounts": {
        "id",
        "primary_email",
        "byo_key_id",
        "created_at",
        "last_seen_at",
    },
    "linked_identities": {
        "id",
        "account_id",
        "provider",
        "provider_subject",
        "provider_email",
        "created_at",
    },
    "sessions": {
        "id",
        "account_id",
        "token_hash",
        "created_via_provider",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
    },
    "byo_keys": {"id", "account_id", "encrypted_key", "created_at", "last_used_at", "is_active"},
    "subscriptions": {
        "id",
        "account_id",
        "stripe_customer_id",
        "stripe_subscription_id",
        "status",
        "current_period_start",
        "current_period_end",
        "cancel_at_period_end",
        "created_at",
        "updated_at",
    },
    "stripe_webhook_events": {"stripe_event_id", "event_type", "processed_at"},
    "csv_statements": {
        "id",
        "account_id",
        "status",
        "filename",
        "uploaded_at",
        "raw_columns",
        "proposed_mapping",
        "confirmed_mapping",
        "entity_name",
        "cadence",
        "statement_data",
        "statement_attrs",
        "expires_at",
        "created_at",
        "confirmed_at",
    },
    "conversations": {
        "id",
        "account_id",
        "title",
        "csv_context_id",
        "created_at",
        "last_turn_at",
    },
    "turns": {
        "id",
        "conversation_id",
        "question",
        "final_answer",
        "hit_iteration_cap",
        "iterations_used",
        "stop_reason",
        "figure_check",
        "tool_calls",
        "model",
        "created_at",
    },
    "usage_events": {"id", "account_id", "occurred_at", "turn_id", "outcome"},
}


def main() -> int:
    engine = get_engine()
    failures = []

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        print(f"alembic_version: {version}")
        if version != "0003_oauth_identity":
            failures.append(f"expected alembic_version '0003_oauth_identity', got {version!r}")

    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())

    for table, expected_columns in EXPECTED_TABLES.items():
        if table not in actual_tables:
            failures.append(f"table {table!r} is missing")
            continue
        actual_columns = {c["name"] for c in inspector.get_columns(table)}
        missing = expected_columns - actual_columns
        extra = actual_columns - expected_columns
        if missing:
            failures.append(f"{table}: missing columns {sorted(missing)}")
        if extra:
            failures.append(f"{table}: unexpected extra columns {sorted(extra)}")
        print(f"{table}: {len(actual_columns)} columns OK")

    # Confirm both directions of the accounts<->byo_keys circular FK resolved.
    accounts_fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("accounts")}
    byo_keys_fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("byo_keys")}
    if "byo_keys" not in accounts_fks:
        failures.append("accounts.byo_key_id -> byo_keys.id FK did not land")
    if "accounts" not in byo_keys_fks:
        failures.append("byo_keys.account_id -> accounts.id FK did not land")

    # Confirm the composite usage_events index.
    usage_indexes = {ix["name"] for ix in inspector.get_indexes("usage_events")}
    if "ix_usage_events_account_id_occurred_at" not in usage_indexes:
        failures.append("usage_events composite (account_id, occurred_at) index is missing")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"\nPASS: all {len(EXPECTED_TABLES)} tables present with expected columns, "
        "both FK directions resolved."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""oauth identity: accounts, linked_identities, sessions replace installs

Revision ID: 0003_oauth_identity
Revises: 0002_monetization_tiers
Create Date: 2026-09-17

Phase C session 1, implementing the OAuth identity design doc's SS4 (schema
redesign): installs' identity_type/identity_value split is retired in favor of
mandatory provider-verified OAuth sign-in for every account. No real data exists
to preserve (confirmed live against the dev database this session -- every
installs row is identity_type='uuid' from the _get_or_create_install shim, every
csv_statements/conversations row is synthetic Phase B test data), so this is a
straight structural rewrite, not a data migration.

Four things happen:
1. New accounts table (replaces installs) -- id/primary_email/byo_key_id/
   created_at/last_seen_at. No identity_type/identity_value: every account is
   now provider-verified by construction, so there's nothing left to
   distinguish.
2. install_id -> account_id rename-and-repoint on byo_keys, subscriptions,
   csv_statements, conversations, usage_events -- FK'd to accounts.id instead
   of installs.install_id. Every other column property (type, nullability,
   other indexes) is untouched; usage_events' composite index is recreated
   under a name matching the renamed column.
3. installs is dropped, along with its now-orphaned install_identity_type enum
   type (op.drop_table does not drop the type itself -- see 0001's own
   documented lesson about this, reapplied here).
4. New linked_identities table (one row per provider identity an account has
   signed in with; UNIQUE(provider, provider_subject) is the per-provider
   anti-duplicate key) and new sessions table (opaque bearer session tokens,
   storing only a token_hash, never the plaintext -- see the design doc SS2).

accounts.byo_key_id and byo_keys.account_id are a circular FK pair, handled
with the same two-step use_alter pattern 0001_initial_schema.py used for
installs.byo_key_id/byo_keys.install_id: byo_keys.account_id's FK is altered
in place once accounts exists, then accounts.byo_key_id's own FK is added as a
deferred ALTER TABLE.

A rename-and-repoint alone doesn't work here, confirmed by actually running this
migration against the dev database: accounts starts empty, but the existing
byo_keys/subscriptions/csv_statements/conversations/usage_events rows still hold
their old install_id values, which the new account_id FK to accounts.id can't
satisfy. The design doc is explicit that no backfill is warranted (no real users
exist), and this session's own live check of the dev database (see the session
plan) confirmed every row in these tables is disposable Phase B test/seed data --
so upgrade() deletes that stale test data outright, in FK-dependency order,
before repointing the columns. This is not a data migration and isn't meant to
be one; a real installation with actual accounts to preserve would need a
genuinely different (backfilling) migration, not this one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_oauth_identity"
down_revision: Union[str, None] = "0002_monetization_tiers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches 0001_initial_schema.py's own installs enum exactly -- needed here only
# for its explicit .drop() call in upgrade()/re-creation in downgrade(), per that
# migration's documented lesson that op.drop_table/op.create_table don't
# symmetrically manage a native Postgres enum type on their own.
_install_identity_type = sa.Enum("google_email", "uuid", name="install_identity_type")


def upgrade() -> None:
    # Clear disposable Phase B test/seed data before repointing FKs to the (empty) new
    # accounts table -- see module docstring. Deleted in FK-dependency order (children
    # before parents); the circular installs<->byo_keys FK is broken by nulling
    # installs.byo_key_id before byo_keys rows are removed. installs' own rows are left
    # in place -- they're destroyed along with the table itself in step 4 below, and
    # dropping a table with rows is not an FK concern the way deleting one row at a time is.
    op.execute("DELETE FROM usage_events")
    op.execute("DELETE FROM turns")
    op.execute("DELETE FROM conversations")
    op.execute("DELETE FROM csv_statements")
    op.execute("DELETE FROM subscriptions")
    op.execute("UPDATE installs SET byo_key_id = NULL")
    op.execute("DELETE FROM byo_keys")

    # 1. accounts -- byo_key_id present, unconstrained until its deferred FK
    # below (mirrors 0001's installs/byo_keys circular-FK sequencing exactly).
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("primary_email", sa.Text(), nullable=True),
        sa.Column("byo_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 2. Rename-and-repoint install_id -> account_id on every dependent table.
    # Each block: drop the old FK (named per 0001/0002's own naming, which
    # SQLAlchemy assigns as "<table>_install_id_fkey" when no explicit name was
    # given at create_table time), rename the column, add the new FK to
    # accounts.id. Column type/nullability and every other constraint/index are
    # untouched.
    op.drop_constraint("byo_keys_install_id_fkey", "byo_keys", type_="foreignkey")
    op.alter_column("byo_keys", "install_id", new_column_name="account_id")
    op.create_foreign_key(
        "byo_keys_account_id_fkey", "byo_keys", "accounts", ["account_id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint("subscriptions_install_id_fkey", "subscriptions", type_="foreignkey")
    op.alter_column("subscriptions", "install_id", new_column_name="account_id")
    op.create_foreign_key(
        "subscriptions_account_id_fkey",
        "subscriptions",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("csv_statements_install_id_fkey", "csv_statements", type_="foreignkey")
    op.alter_column("csv_statements", "install_id", new_column_name="account_id")
    op.create_foreign_key(
        "csv_statements_account_id_fkey",
        "csv_statements",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("conversations_install_id_fkey", "conversations", type_="foreignkey")
    op.alter_column("conversations", "install_id", new_column_name="account_id")
    op.create_foreign_key(
        "conversations_account_id_fkey",
        "conversations",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("usage_events_install_id_fkey", "usage_events", type_="foreignkey")
    op.alter_column("usage_events", "install_id", new_column_name="account_id")
    op.create_foreign_key(
        "usage_events_account_id_fkey",
        "usage_events",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Composite index rename: drop the old (install_id, occurred_at) index and
    # recreate it under a name matching the renamed column -- same index
    # definition (same columns, same order), not a redesign.
    op.drop_index("ix_usage_events_install_id_occurred_at", table_name="usage_events")
    op.create_index(
        "ix_usage_events_account_id_occurred_at", "usage_events", ["account_id", "occurred_at"]
    )

    # 3. Deferred FK completing the accounts<->byo_keys circular pair.
    op.create_foreign_key(
        "fk_accounts_byo_key_id", "accounts", "byo_keys", ["byo_key_id"], ["id"]
    )

    # 4. Drop installs (and its now-orphaned enum type -- see module docstring).
    op.drop_constraint("uq_installs_identity", "installs", type_="unique")
    op.drop_table("installs")
    _install_identity_type.drop(op.get_bind(), checkfirst=True)

    # 5. linked_identities -- FK to accounts.id.
    op.create_table(
        "linked_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("provider_email", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_unique_constraint(
        "uq_linked_identities_provider_subject",
        "linked_identities",
        ["provider", "provider_subject"],
    )
    op.create_index(
        "ix_linked_identities_provider_email", "linked_identities", ["provider_email"]
    )

    # 6. sessions -- FK to accounts.id.
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_via_provider", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_sessions_token_hash", "sessions", ["token_hash"])


def downgrade() -> None:
    # No data to restore here, deliberately: upgrade()'s test-data deletion isn't
    # reversible (and isn't meant to be -- see module docstring), and this migration
    # never wrote any accounts/linked_identities/sessions rows of its own that would need
    # undoing. This downgrade is a structural inverse only -- same tables, columns,
    # constraints, and index names as the pre-upgrade (0002) schema -- not a data
    # restoration, matching this migration's own upgrade()/module-docstring framing.

    # Reverse of upgrade step 6.
    op.drop_table("sessions")

    # Reverse of upgrade step 5.
    op.drop_index("ix_linked_identities_provider_email", table_name="linked_identities")
    op.drop_constraint(
        "uq_linked_identities_provider_subject", "linked_identities", type_="unique"
    )
    op.drop_table("linked_identities")

    # Reverse of upgrade step 4 -- recreate installs (and its enum type) exactly
    # as 0001_initial_schema.py originally created it, minus the
    # free_window_started_at column 0002_monetization_tiers already dropped
    # (this downgrade restores this migration's own prior state, i.e. 0002's
    # schema, not 0001's).
    # No explicit _install_identity_type.create() call here -- op.create_table's own column
    # DDL creates the enum type automatically as part of table creation (0001's own
    # documented lesson; an explicit .create() first collides with it, confirmed live when
    # this downgrade was first run). Only .drop() needs to be explicit (see upgrade() above),
    # since drop_table doesn't symmetrically drop the type the way create_table creates it.
    op.create_table(
        "installs",
        sa.Column("install_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("identity_type", _install_identity_type, nullable=False),
        sa.Column("identity_value", sa.Text(), nullable=False),
        sa.Column("byo_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_installs_identity", "installs", ["identity_type", "identity_value"]
    )
    # Restore installs' side of the circular installs<->byo_keys FK -- byo_keys itself
    # still exists throughout this downgrade (only its account_id column gets renamed back
    # to install_id further below), so this can be created immediately, matching
    # 0001_initial_schema.py's original deferred-FK step.
    op.create_foreign_key(
        "fk_installs_byo_key_id", "installs", "byo_keys", ["byo_key_id"], ["id"]
    )

    # Reverse of upgrade step 3.
    op.drop_constraint("fk_accounts_byo_key_id", "accounts", type_="foreignkey")

    # Reverse of upgrade step 2, each block undone in the opposite order to how
    # it was applied: drop the account_id FK/index, rename the column back,
    # recreate the original install_id FK to installs.install_id.
    op.drop_index("ix_usage_events_account_id_occurred_at", table_name="usage_events")
    op.drop_constraint("usage_events_account_id_fkey", "usage_events", type_="foreignkey")
    op.alter_column("usage_events", "account_id", new_column_name="install_id")
    op.create_foreign_key(
        "usage_events_install_id_fkey",
        "usage_events",
        "installs",
        ["install_id"],
        ["install_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_usage_events_install_id_occurred_at", "usage_events", ["install_id", "occurred_at"]
    )

    op.drop_constraint("conversations_account_id_fkey", "conversations", type_="foreignkey")
    op.alter_column("conversations", "account_id", new_column_name="install_id")
    op.create_foreign_key(
        "conversations_install_id_fkey",
        "conversations",
        "installs",
        ["install_id"],
        ["install_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("csv_statements_account_id_fkey", "csv_statements", type_="foreignkey")
    op.alter_column("csv_statements", "account_id", new_column_name="install_id")
    op.create_foreign_key(
        "csv_statements_install_id_fkey",
        "csv_statements",
        "installs",
        ["install_id"],
        ["install_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("subscriptions_account_id_fkey", "subscriptions", type_="foreignkey")
    op.alter_column("subscriptions", "account_id", new_column_name="install_id")
    op.create_foreign_key(
        "subscriptions_install_id_fkey",
        "subscriptions",
        "installs",
        ["install_id"],
        ["install_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("byo_keys_account_id_fkey", "byo_keys", type_="foreignkey")
    op.alter_column("byo_keys", "account_id", new_column_name="install_id")
    op.create_foreign_key(
        "byo_keys_install_id_fkey",
        "byo_keys",
        "installs",
        ["install_id"],
        ["install_id"],
        ondelete="CASCADE",
    )

    # Reverse of upgrade step 1.
    op.drop_table("accounts")

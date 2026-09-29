"""
Read-only audit for SECURITY.md SS7 (the removed email-based account linking): lists every
account with more than one linked_identities row. Resolution step (c) always creates exactly
one row per account, so every multi-row account was produced by the removed step (b) --
either a legitimate same-email link (e.g. the owner's own 2026-09-29 live testing) or a
takeover. Each result needs a human review; nothing here deletes or changes anything, and
results must never be acted on automatically. Run from inside backend/:

    .venv/Scripts/python.exe -m scripts.audit_linked_identities

DATABASE_URL is loaded exactly as the app loads it (db.base, backend/.env). The transaction
is set READ ONLY before any other statement, so Postgres itself rejects any write, and it's
rolled back at the end regardless.
"""

import sys

from sqlalchemy import text

from db.base import get_engine

# Verbatim from SECURITY.md SS7 -- keep the two in sync.
AUDIT_QUERY = """
SELECT account_id, count(*),
       array_agg(provider || ':' || provider_email || ' @ ' || created_at ORDER BY created_at)
FROM linked_identities GROUP BY account_id HAVING count(*) > 1;
"""


def main() -> int:
    with get_engine().connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            if conn.execute(text("SHOW transaction_read_only")).scalar_one() != "on":
                print("Refusing to continue: transaction is not read-only.", file=sys.stderr)
                return 1

            database = conn.execute(text("SELECT current_database()")).scalar_one()
            rows = conn.execute(text(AUDIT_QUERY)).all()
        finally:
            trans.rollback()

    print(f"Database: {database} (read-only transaction, rolled back)")
    print()
    for account_id, count, identities in sorted(rows, key=lambda r: str(r[0])):
        print(f"account {account_id} -- {count} linked identities:")
        for identity in identities:
            print(f"    {identity}")
        print()
    print(f"Total: {len(rows)} account(s) with more than one linked identity.")
    if rows:
        print("Each needs a human review (SECURITY.md SS7) -- do not delete rows automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

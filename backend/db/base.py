"""
Engine, session factory, and declarative Base for the Sheets Add-on backend's
database -- a separate, paid-tier Postgres instance from anything the existing
Streamlit app touches (that app has no database at all; see CLAUDE.md).

DATABASE_URL is read from the environment only, never hardcoded -- loaded via
python-dotenv from backend/.env if present (mirroring src/app/main.py's own
ANTHROPIC_API_KEY/SEC_USER_AGENT loading convention), falling back to an
already-set environment variable otherwise. get_database_url()/get_engine()
raise immediately if unset rather than silently defaulting to a local/sqlite
connection or a lazily-None engine, so a missing env var fails loudly instead
of quietly running migrations against the wrong database -- or not connecting
at all until some much later, harder-to-diagnose point.

No engine is constructed at import time -- doing so would either require
DATABASE_URL at import time (breaking any code that just wants Base/the ORM
models without connecting, e.g. Alembic's autogenerate target) or silently
swallow a missing env var behind a None. get_engine() instead builds the
engine lazily on its first real call, then caches and reuses that same
engine (and its connection pool) for every later get_session() call across
the process's lifetime -- the standard SQLAlchemy pattern of one Engine per
process, with each get_session() call still handing back a fresh, independent
Session drawing a connection from that shared pool. Before Phase C session 4's
connection-latency investigation, get_engine() built a brand-new Engine --
and so a brand-new, unpooled connection -- on every single call; see
get_engine()'s own docstring and NOTES.md for what that cost in practice and
why it went unnoticed until now.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_engine: Engine | None = None


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy backend/.env.example to backend/.env and fill it "
            "in, or set DATABASE_URL directly in the environment -- never hardcode it."
        )
    return url


class Base(DeclarativeBase):
    pass


def get_engine() -> Engine:
    """
    Builds the Engine once, lazily, and reuses it (and its connection pool) for every
    subsequent call -- previously this constructed a brand-new Engine, with its own
    unpooled connection, on every single call, so every DB touch anywhere in this codebase
    paid full connection-establishment cost every time; measured at ~5s/call against this
    backend's remote Postgres under degraded network conditions during Phase C session 4,
    and a real (if usually much smaller) cost even under normal conditions. See NOTES.md.

    A plain module-level global, checked and set inside this function, rather than
    @functools.lru_cache: get_database_url() re-reads os.environ on every call, and
    lru_cache would silently freeze in whichever DATABASE_URL was set on the first call --
    fine in production (DATABASE_URL doesn't change mid-process) but a footgun against a
    test that monkeypatch.setenv's it expecting the change to take effect. An explicit
    global is one line more code and has no such caveat to remember.

    Still lazy, not a real module-level singleton built at import time -- the original
    reasoning for that still holds (see the module docstring): a missing DATABASE_URL must
    fail loudly at first real use, not silently, and importing this module for Base/the ORM
    models alone (e.g. Alembic's autogenerate target) still needs no env var at all.

    pool_pre_ping=True: cheap to add now that a connection can actually be reused across
    calls (moot before, since a fresh connection is never stale) -- issues a lightweight
    liveness check before handing out a pooled connection and transparently reconnects if
    the server (or an intermediary, e.g. a hosting provider's proxy) silently dropped it
    while idle, rather than surfacing that as an OperationalError on whatever request
    happened to draw that connection next.
    """
    global _engine
    if _engine is None:
        # hide_parameters=True (session 10's security hardening pass): an uncaught DB
        # error's traceback would otherwise include SQLAlchemy's default
        # compiled-SQL-plus-bind-params rendering, which for a Turn insert means raw
        # question/final_answer text landing in whatever log sink eventually captures it
        # -- no aggregation is wired up yet, but this is cheap to set now rather than only
        # once that changes.
        _engine = create_engine(get_database_url(), hide_parameters=True, pool_pre_ping=True)
    return _engine


def get_session() -> Session:
    return sessionmaker(bind=get_engine())()

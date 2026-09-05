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

Deliberately no module-level `engine`/`SessionLocal` singleton: constructing
one on import would either require DATABASE_URL at import time (breaking any
code that just wants Base/the ORM models without connecting, e.g. Alembic's
autogenerate target) or silently swallow a missing env var behind a None. Call
get_engine()/get_session() when a real connection is actually needed instead.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


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
    return create_engine(get_database_url())


def get_session() -> Session:
    return sessionmaker(bind=get_engine())()

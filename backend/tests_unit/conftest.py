"""
backend/tests_unit/: pure backend tests that never touch a database (Phase D session 5).

Kept out of backend/tests/ on purpose: that directory's conftest.py builds a TestClient whose
fixtures perform real /v1/auth/exchange calls against backend/.env's DATABASE_URL -- today the
shared production database. Nothing here does. The autouse fixture below makes any attempt to
reach the database fail loudly instead: db.base.get_engine and app.main.get_session both raise,
so a test that needs a session must supply its own fake.

Run from backend/: `.venv/Scripts/python.exe -m pytest tests_unit`.
"""

import sys
from pathlib import Path

import pytest

# The repo root, for `src.*` (app/main.py adds it itself on import; ask_rules' tests need it
# without importing app.main).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import db.base  # noqa: E402


def _refuse(*args, **kwargs):
    raise AssertionError("backend/tests_unit must never touch the database")


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    monkeypatch.setattr(db.base, "get_engine", _refuse)
    import app.main as app_main

    monkeypatch.setattr(app_main, "get_session", _refuse)

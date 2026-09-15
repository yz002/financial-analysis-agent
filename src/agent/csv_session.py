"""
Registry for the "currently active CSV-uploaded business" DataFrame, so src/agent/tools.py can
read a normalized DataFrame (src/analysis/csv_statement.normalize's output) without importing
Streamlit -- tools.py is otherwise UI-framework-agnostic by design (usable standalone in tests
and one-liners, per CLAUDE.md), and run_agent/execute_tool have no notion of a "session" at all
(tool dispatch is pure name -> function(**tool_input)). src/app/main.py calls set_active_csv
right after a successful normalize(), and set_active_csv(None) whenever that upload is reset.

Backed by a contextvars.ContextVar rather than a plain module global. A ContextVar is
coroutine/thread-safe by construction: each thread (and each asyncio task) gets its own
independent value, isolated from every other thread/task, even though they all share this same
module and process. That's what makes this safe for the Sheets Add-on backend
(backend/app/main.py), which is a real multi-tenant, multi-request-concurrently service --
FastAPI's /v1/ask handler sets this ContextVar to one install's confirmed CSV statement for the
duration of that request's run_agent() call and resets it in a finally block, so two installs'
concurrent /v1/ask calls can never see or clobber each other's active CSV, no matter how their
requests interleave on the threadpool. For the existing single-threaded, sequential callers
(src/app/main.py's Streamlit app; tests/test_tools.py's fixture), a ContextVar's set/get behaves
exactly like the plain global it replaces -- this is a drop-in replacement, not a behavior change,
for anything already calling set_active_csv/get_active_csv.
"""

import contextvars

import pandas as pd

_active_csv_var: contextvars.ContextVar[pd.DataFrame | None] = contextvars.ContextVar(
    "active_csv_statement", default=None
)


def set_active_csv(df: pd.DataFrame | None) -> None:
    """Set (or clear, with None) the active CSV-derived statement DataFrame for the current
    context (thread/async task)."""
    _active_csv_var.set(df)


def get_active_csv() -> pd.DataFrame | None:
    """Return the active CSV-derived statement DataFrame for the current context, or None if
    nothing has been uploaded and confirmed yet (or it was since cleared)."""
    return _active_csv_var.get()


def set_active_csv_with_token(df: pd.DataFrame | None) -> contextvars.Token:
    """Like set_active_csv, but returns a Token that reset_active_csv can use to restore this
    context's prior value exactly -- for a caller (e.g. the backend's /v1/ask handler) that needs
    to guarantee its own context is left exactly as it found it, in a finally block, rather than
    unconditionally clearing to None (which set_active_csv(None) would do instead)."""
    return _active_csv_var.set(df)


def reset_active_csv(token: contextvars.Token) -> None:
    """Restore the active CSV ContextVar to the value it held before the matching
    set_active_csv_with_token call, using the Token that call returned."""
    _active_csv_var.reset(token)

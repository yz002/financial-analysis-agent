"""
In-process registry for the single active CSV-uploaded business, so src/agent/tools.py can
read a normalized DataFrame (src/analysis/csv_statement.normalize's output) without importing
Streamlit -- tools.py is otherwise UI-framework-agnostic by design (usable standalone in tests
and one-liners, per CLAUDE.md), and run_agent/execute_tool have no notion of a "session" at all
(tool dispatch is pure name -> function(**tool_input)). src/app/main.py calls set_active_csv
right after a successful normalize(), and set_active_csv(None) whenever that upload is reset.

This is a single, process-global slot, not one scoped per browser session -- a real concurrency
limitation, not just a persistence one, since two people hitting the same running process at the
same time could have their CSVs cross. See NOTES.md's "Known limitations / future work" section
for the full statement of that limitation; not addressed here.
"""

import pandas as pd

_active_csv_statement: pd.DataFrame | None = None


def set_active_csv(df: pd.DataFrame | None) -> None:
    """Set (or clear, with None) the single active CSV-derived statement DataFrame."""
    global _active_csv_statement
    _active_csv_statement = df


def get_active_csv() -> pd.DataFrame | None:
    """Return the active CSV-derived statement DataFrame, or None if nothing has been
    uploaded and confirmed yet (or it was since cleared)."""
    return _active_csv_statement

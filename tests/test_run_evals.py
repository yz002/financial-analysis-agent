"""
Dry-run test of evals/run_evals.py's wiring: does it produce the right files with the right
shape? agent.run_agent is monkeypatched to a canned response, so this never makes a real
Anthropic API call. Scoped to the "amd_nvda_comparison" question deliberately -- it's one of the
two questions with no precomputed ground truth (see evals/ground_truth.py), so this test also
never touches EDGAR, keeping it fully offline like the rest of the suite.
"""

import json
from unittest.mock import MagicMock

import pytest

from evals import run_evals


def _fake_run_agent(question, client=None, model=None, max_iterations=None):
    return {
        "question": question,
        "tool_calls": [],
        "final_answer": (
            "AMD and Nvidia both show strong margins; Nvidia looks better positioned given its "
            "wider gross margin."
        ),
        "hit_iteration_cap": False,
        "iterations_used": 1,
        "stop_reason": "end_turn",
        "figure_check": {
            "figures_checked": 0,
            "figures_traced": 0,
            "figures_untraced": 0,
            "all_traced": True,
            "figures": [],
        },
    }


def test_main_wiring_produces_summary_and_traces(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evals.anthropic, "Anthropic", lambda: MagicMock())
    monkeypatch.setattr(run_evals.agent_mod, "run_agent", _fake_run_agent)

    output_dir = tmp_path / "run"
    summary = run_evals.main(
        ["--runs", "1", "--questions", "amd_nvda_comparison", "--output-dir", str(output_dir)]
    )

    assert summary["total_runs"] == 1
    assert len(summary["questions"]) == 1
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "summary.md").exists()

    trace_files = list((output_dir / "traces").glob("*.json"))
    assert len(trace_files) == 1
    saved = json.loads(trace_files[0].read_text(encoding="utf-8"))
    assert saved["question_id"] == "amd_nvda_comparison"
    assert saved["run_agent_result"]["final_answer"].startswith("AMD and Nvidia")
    assert saved["input_tokens"] == 0  # fake client never invoked -- run_agent is stubbed directly


def test_multiple_runs_produce_one_trace_file_each(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evals.anthropic, "Anthropic", lambda: MagicMock())
    monkeypatch.setattr(run_evals.agent_mod, "run_agent", _fake_run_agent)

    output_dir = tmp_path / "run"
    summary = run_evals.main(
        ["--runs", "2", "--questions", "amd_nvda_comparison", "--output-dir", str(output_dir)]
    )

    assert summary["total_runs"] == 2
    assert len(list((output_dir / "traces").glob("*.json"))) == 2


def test_unknown_question_id_raises_systemexit(monkeypatch):
    monkeypatch.setattr(run_evals.anthropic, "Anthropic", lambda: MagicMock())
    with pytest.raises(SystemExit):
        run_evals.main(["--questions", "not_a_real_id"])

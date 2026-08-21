"""
Phase 6 evaluation harness. Runs README's seven target questions against the live agent
(run_agent, real Anthropic API calls) `--runs` times each, scores every run against the
per-question checks in scoring.py, and writes a full trace per run plus a machine- and
human-readable summary -- so a result can be inspected after the fact, not just scored.

This makes real API calls and costs real money. Confirm the run size (see --runs, --questions)
before invoking this for real; a `--runs 1 --questions <one id>` smoke pass is a cheap way to
check the wiring before committing to a fuller pass.

Usage (from the repo root, so evals/ resolves as a package):
    python -m evals.run_evals [--runs N] [--model MODEL] [--questions id1,id2] [--output-dir DIR]
"""

import argparse
import json
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from src.agent import agent as agent_mod

from . import ground_truth, scoring
from .questions import QUESTIONS
from .token_tracking import TrackedClient

DEFAULT_RUNS = 3
RESULTS_ROOT = Path(__file__).parent / "results"


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _jsonable(obj):
    """pandas Timestamps (from ground_truth.py) -> ISO strings; dicts/lists recursed into;
    anything already JSON-safe passed through."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def run_one(question, client: TrackedClient, model: str, max_iterations: int) -> dict:
    """Run `question` once against the live agent and score it. Returns everything needed to
    both reproduce (the raw run_agent result) and score-at-a-glance (wall clock, tokens, the
    check breakdown) this single run."""
    client.reset()
    started = time.monotonic()
    result = agent_mod.run_agent(question.text, client=client, model=model, max_iterations=max_iterations)
    elapsed = time.monotonic() - started

    gt = ground_truth.compute(question, run_result=result)
    score = scoring.score_question(question, result, gt)

    return {
        "question_id": question.id,
        "question_text": question.text,
        "wall_clock_seconds": elapsed,
        "input_tokens": client.total_input_tokens,
        "output_tokens": client.total_output_tokens,
        "iterations_used": result.get("iterations_used"),
        "hit_iteration_cap": result.get("hit_iteration_cap"),
        "tool_names_called": [c["tool_name"] for c in result.get("tool_calls") or []],
        "ground_truth": _jsonable(gt),
        "score": score,
        "run_agent_result": result,
    }


def _build_summary(per_question: list[dict]) -> dict:
    questions_out = []
    all_passed, all_grounding = [], []
    all_wall_clock, all_tokens_in, all_tokens_out = [], [], []
    all_tool_names: Counter = Counter()
    cap_hits = 0
    total_runs = 0

    for entry in per_question:
        question, runs = entry["question"], entry["runs"]
        passed_flags = [r["score"]["passed"] for r in runs]
        grounding_rates = [
            (r["score"]["grounding"]["figures_traced"] / r["score"]["grounding"]["figures_checked"])
            if r["score"]["grounding"]["figures_checked"]
            else 1.0
            for r in runs
        ]
        for r in runs:
            total_runs += 1
            all_tool_names.update(r["tool_names_called"])
            all_wall_clock.append(r["wall_clock_seconds"])
            all_tokens_in.append(r["input_tokens"])
            all_tokens_out.append(r["output_tokens"])
            if r["hit_iteration_cap"]:
                cap_hits += 1
        all_passed.extend(passed_flags)
        all_grounding.extend(grounding_rates)

        questions_out.append(
            {
                "id": question.id,
                "text": question.text,
                "category": question.category,
                "runs": len(runs),
                "pass_rate": statistics.mean(passed_flags) if passed_flags else None,
                "mean_grounding_rate": statistics.mean(grounding_rates) if grounding_rates else None,
                "per_run": [
                    {
                        "run_index": r["run_index"],
                        "passed": r["score"]["passed"],
                        "checks": r["score"]["checks"],
                        "grounding": r["score"]["grounding"],
                        "iterations_used": r["iterations_used"],
                        "hit_iteration_cap": r["hit_iteration_cap"],
                        "wall_clock_seconds": r["wall_clock_seconds"],
                        "input_tokens": r["input_tokens"],
                        "output_tokens": r["output_tokens"],
                        "tool_names_called": r["tool_names_called"],
                    }
                    for r in runs
                ],
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_runs": total_runs,
        "overall_pass_rate": statistics.mean(all_passed) if all_passed else None,
        "mean_grounding_rate": statistics.mean(all_grounding) if all_grounding else None,
        "iteration_cap_hit_rate": (cap_hits / total_runs) if total_runs else None,
        "mean_wall_clock_seconds": statistics.mean(all_wall_clock) if all_wall_clock else None,
        "total_input_tokens": sum(all_tokens_in),
        "total_output_tokens": sum(all_tokens_out),
        "tool_call_distribution": dict(all_tool_names),
        "questions": questions_out,
    }


def _pct(value) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


def _render_markdown(summary: dict) -> str:
    lines = [
        "# Evaluation results",
        "",
        f"Generated {summary['generated_at']} - {summary['total_runs']} total runs",
        "",
        "## Aggregate",
        "",
        f"- Overall pass rate: {_pct(summary['overall_pass_rate'])}",
        f"- Mean grounding rate: {_pct(summary['mean_grounding_rate'])}",
        f"- Iteration-cap hit rate: {_pct(summary['iteration_cap_hit_rate'])}",
        (
            f"- Mean wall-clock time per run: {summary['mean_wall_clock_seconds']:.1f}s"
            if summary["mean_wall_clock_seconds"] is not None
            else "- Mean wall-clock time per run: n/a"
        ),
        f"- Total tokens: {summary['total_input_tokens']:,} in / {summary['total_output_tokens']:,} out",
        f"- Tool-call distribution: {', '.join(f'{k}={v}' for k, v in summary['tool_call_distribution'].items()) or 'none'}",
        "",
        "## Per-question detail",
        "",
    ]
    for q in summary["questions"]:
        lines.append(f"### {q['id']} ({q['category']})")
        lines.append("")
        lines.append(f"> {q['text']}")
        lines.append("")
        lines.append(
            f"Pass rate: {_pct(q['pass_rate'])} ({q['runs']} runs) - "
            f"Mean grounding rate: {_pct(q['mean_grounding_rate'])}"
        )
        lines.append("")
        for run in q["per_run"]:
            status = "PASS" if run["passed"] else "FAIL"
            cap_note = " (hit iteration cap)" if run["hit_iteration_cap"] else ""
            lines.append(
                f"- Run {run['run_index']}: **{status}** - "
                f"{run['grounding']['figures_traced']}/{run['grounding']['figures_checked']} figures traced, "
                f"{run['iterations_used']} iterations{cap_note}, {run['wall_clock_seconds']:.1f}s"
            )
            for check in run["checks"]:
                mark = "x" if check["passed"] else " "
                lines.append(f"  - [{mark}] {check['name']}: {check['detail']}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"Repetitions per question (default {DEFAULT_RUNS}).")
    parser.add_argument(
        "--model",
        default=agent_mod.DEFAULT_MODEL,
        help="Model to evaluate (default: same as agent.py's production default -- this harness tests production behavior).",
    )
    parser.add_argument("--max-iterations", type=int, default=agent_mod.DEFAULT_MAX_ITERATIONS)
    parser.add_argument("--questions", default=None, help="Comma-separated question ids to run (default: all 7).")
    parser.add_argument("--output-dir", default=None, help="Defaults to evals/results/<UTC timestamp>/.")
    args = parser.parse_args(argv)

    questions = QUESTIONS
    if args.questions:
        wanted = set(args.questions.split(","))
        questions = [q for q in QUESTIONS if q.id in wanted]
        unknown = wanted - {q.id for q in questions}
        if unknown:
            raise SystemExit(f"Unknown question id(s): {sorted(unknown)}; valid ids: {[q.id for q in QUESTIONS]}")

    output_dir = Path(args.output_dir) if args.output_dir else RESULTS_ROOT / _run_id()
    traces_dir = output_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    client = TrackedClient(anthropic.Anthropic())

    per_question = []
    for question in questions:
        runs = []
        for i in range(1, args.runs + 1):
            run = run_one(question, client, args.model, args.max_iterations)
            run["run_index"] = i
            runs.append(run)
            trace_path = traces_dir / f"{question.id}_run{i}.json"
            trace_path.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
        per_question.append({"question": question, "runs": runs})

    summary = _build_summary(per_question)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (output_dir / "summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    print(f"Wrote results to {output_dir}")
    return summary


if __name__ == "__main__":
    main()

"""
Tests for src/agent/agent.py's manual tool-calling loop, fully offline: the
Anthropic client is a MagicMock whose `.messages.create` returns a
pre-scripted sequence of fake responses (SimpleNamespace stand-ins for the
real response/content-block objects, since run_agent only ever touches
`.stop_reason`, `.content`, and each block's `.type`/`.text`/`.name`/
`.input`/`.id`). No live API calls anywhere in this file.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent import agent, tools


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name, tool_input, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def _response(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _client_with_responses(responses):
    client = MagicMock()
    client.messages.create = MagicMock(side_effect=responses)
    return client


def test_single_tool_round_trip(monkeypatch):
    monkeypatch.setattr(tools, "execute_tool", lambda name, tool_input: json.dumps({"ok": True}))
    responses = [
        _response([_tool_use_block("get_market_data", {"ticker": "MSFT"})], "tool_use"),
        _response([_text_block("MSFT trades at $X.")], "end_turn"),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("What is MSFT trading at?", client=client)

    assert result["final_answer"] == "MSFT trades at $X."
    assert result["hit_iteration_cap"] is False
    assert result["iterations_used"] == 2
    assert result["stop_reason"] == "end_turn"
    assert len(result["tool_calls"]) == 1
    call = result["tool_calls"][0]
    assert call["iteration"] == 1
    assert call["tool_name"] == "get_market_data"
    assert call["tool_input"] == {"ticker": "MSFT"}
    assert call["tool_result"] == json.dumps({"ok": True})
    assert call["is_error"] is False


def test_multi_round_tool_calls(monkeypatch):
    monkeypatch.setattr(tools, "execute_tool", lambda name, tool_input: json.dumps({"tool": name}))
    responses = [
        _response([_tool_use_block("get_financial_statement", {"ticker": "MSFT"})], "tool_use"),
        _response([_tool_use_block("get_ratios", {"ticker": "MSFT"})], "tool_use"),
        _response([_text_block("Final answer.")], "end_turn"),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("Analyze MSFT.", client=client)

    assert result["iterations_used"] == 3
    assert result["hit_iteration_cap"] is False
    assert [c["tool_name"] for c in result["tool_calls"]] == [
        "get_financial_statement",
        "get_ratios",
    ]
    assert [c["iteration"] for c in result["tool_calls"]] == [1, 2]
    assert result["final_answer"] == "Final answer."


def test_iteration_cap_hit_sets_flag_and_note(monkeypatch):
    monkeypatch.setattr(tools, "execute_tool", lambda name, tool_input: json.dumps({"ok": True}))
    # Every round requests a tool call and never stops on its own.
    responses = [
        _response([_tool_use_block("get_market_data", {"ticker": "MSFT"})], "tool_use"),
        _response([_tool_use_block("get_market_data", {"ticker": "MSFT"})], "tool_use"),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("Analyze MSFT.", client=client, max_iterations=2)

    assert result["hit_iteration_cap"] is True
    assert result["iterations_used"] == 2
    assert "2-round tool-call limit" in result["final_answer"]
    assert len(result["tool_calls"]) == 2


def test_iteration_cap_note_includes_partial_text(monkeypatch):
    monkeypatch.setattr(tools, "execute_tool", lambda name, tool_input: json.dumps({"ok": True}))
    responses = [
        _response(
            [_text_block("Partial thought."), _tool_use_block("get_market_data", {"ticker": "MSFT"})],
            "tool_use",
        ),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("Analyze MSFT.", client=client, max_iterations=1)

    assert result["hit_iteration_cap"] is True
    assert result["final_answer"].startswith("Partial thought.")
    assert "tool-call limit" in result["final_answer"]


def test_unknown_tool_name_yields_invalid_input(monkeypatch):
    execute_tool_mock = MagicMock()
    monkeypatch.setattr(tools, "execute_tool", execute_tool_mock)
    responses = [
        _response([_tool_use_block("not_a_real_tool", {})], "tool_use"),
        _response([_text_block("Done.")], "end_turn"),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("Do something unsupported.", client=client)

    call = result["tool_calls"][0]
    assert call["is_error"] is True
    parsed = json.loads(call["tool_result"])
    assert parsed["error_type"] == "invalid_input"
    execute_tool_mock.assert_not_called()


def test_tool_crash_yields_source_error(monkeypatch):
    def boom(name, tool_input):
        raise RuntimeError("underlying tool exploded")

    monkeypatch.setattr(tools, "execute_tool", boom)
    responses = [
        _response([_tool_use_block("get_market_data", {"ticker": "MSFT"})], "tool_use"),
        _response([_text_block("Done.")], "end_turn"),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("What is MSFT trading at?", client=client)

    call = result["tool_calls"][0]
    assert call["is_error"] is True
    parsed = json.loads(call["tool_result"])
    assert parsed["error_type"] == "source_error"
    assert "underlying tool exploded" in parsed["error"]


def test_max_iterations_below_one_raises_value_error():
    with pytest.raises(ValueError):
        agent.run_agent("Anything.", client=MagicMock(), max_iterations=0)


def test_no_tool_use_returns_immediately_on_first_response():
    client = _client_with_responses(
        [_response([_text_block("Straight answer, no tools needed.")], "end_turn")]
    )
    result = agent.run_agent("A question that needs no tools.", client=client)
    assert result["final_answer"] == "Straight answer, no tools needed."
    assert result["iterations_used"] == 1
    assert result["tool_calls"] == []
    assert result["hit_iteration_cap"] is False


def test_final_answer_falls_back_to_placeholder_when_no_text(monkeypatch):
    client = _client_with_responses([_response([], "end_turn")])
    result = agent.run_agent("Silent response.", client=client)
    assert result["final_answer"] == "(Claude produced no text response.)"


def test_csv_and_ticker_tool_calls_in_one_round(monkeypatch):
    """Mechanics-only check for the CSV-vs-ticker comparison scenario (see the approved
    Session 2 plan's test-scope decision): confirms the loop can execute get_csv_ratios and
    get_ratios(ticker=...) together, within one iteration's multiple tool_use blocks, and that
    both results land in tool_calls correctly. Whether the model's own prose actually prefers
    scale-invariant ratios and caveats absolute figures is the model's judgment, not code, and
    is checked live (not here) per that decision."""
    monkeypatch.setattr(
        tools, "execute_tool", lambda name, tool_input: json.dumps({"tool": name, "input": tool_input})
    )
    responses = [
        _response(
            [
                _tool_use_block("get_csv_ratios", {"ratio_names": ["net_margin"]}, block_id="toolu_csv"),
                _tool_use_block(
                    "get_ratios",
                    {"ticker": "MSFT", "ratio_names": ["net_margin"]},
                    block_id="toolu_msft",
                ),
            ],
            "tool_use",
        ),
        _response([_text_block("Comparison done.")], "end_turn"),
    ]
    client = _client_with_responses(responses)

    result = agent.run_agent("How does my margin compare to Microsoft's?", client=client)

    assert result["iterations_used"] == 2
    assert result["hit_iteration_cap"] is False
    assert [c["tool_name"] for c in result["tool_calls"]] == ["get_csv_ratios", "get_ratios"]
    assert [c["iteration"] for c in result["tool_calls"]] == [1, 1]
    assert result["final_answer"] == "Comparison done."


def test_system_prompt_covers_csv_comparison_and_market_data_scope():
    """Cheap regression for "forgot to add this instruction" mistakes -- doesn't test that the
    model actually follows these instructions (checked live, per the Session 2 plan's test-scope
    decision), just that they genuinely exist in the prompt sent to it."""
    prompt = agent.SYSTEM_PROMPT
    assert "get_csv_statement" in prompt
    assert "get_csv_ratios" in prompt
    assert "scale-invariant" in prompt
    assert "get_market_data" in prompt and "never apply" in prompt

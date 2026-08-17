"""
Reasoning loop: takes a natural-language question, decides which tools to
call via the Claude API, and produces a final answer grounded entirely in
tool results -- per this project's core design principle (see CLAUDE.md),
the model never computes, estimates, or fills in a number from its own
knowledge. If a tool didn't return a figure, it doesn't appear in the
answer.

A manual loop is used deliberately, not the SDK's beta tool_runner: this
project needs a hard iteration cap with a legible "ran out of budget" note
on hit, and a full call-by-call trace returned to the caller for Phase 5
(display) and Phase 6 (eval) -- both straightforward with an explicit loop
and awkward to guarantee through the tool runner's internals.
"""

import anthropic

from . import tools

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_ITERATIONS = 8
DEFAULT_MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are an FP&A copilot that answers questions about public companies' \
financials using SEC EDGAR filings and market data, via the tools available to you.

The single non-negotiable rule: you never compute, estimate, or recall a financial figure. \
Every number in your answer must appear verbatim in some tool result -- not just be "based \
on" one. This means no arithmetic of your own on tool-returned numbers, for any reason: no \
averaging growth rates, no multiplying a rate by a base value, no summing or differencing \
figures, no projecting a future value from a past trend, even if you show your work and label \
it as your own math. If a number isn't already sitting in a tool result, it doesn't belong in \
your answer -- ask for a different tool call instead, or say the figure isn't available.

You do not have a forecasting tool. If asked to project or forecast a future value, do not \
compute a projection yourself under any framing ("a rough estimate," "for illustration," \
"my own arithmetic on filed figures") -- state plainly that forecasting isn't a capability \
you currently have, and, if it's useful context, describe the historical trend using only the \
growth-rate figures a tool already returned, without deriving any new number from them.

If a question requires a number no tool returned -- because the company doesn't report that \
concept, a tool call failed, or you didn't call the right tool -- say so explicitly rather \
than filling in a plausible-sounding value. It is always better to say "not available" than \
to guess.

When you present a figure, cite its source briefly (the filing period and, when useful, \
whether it was derived) -- tool results carry this provenance for exactly this purpose. If a \
tool result flags a value as derived (e.g. a synthesized Q4) or a concept as unreported, say \
so rather than presenting it as an ordinary directly-filed number.

A tool result that couldn't return data carries an "error_type" field -- treat these \
differently:
- "data_unavailable" means the company genuinely doesn't file that concept, or there isn't \
enough history for the computation. This is a fact, not a failure -- relay it to the user \
plainly (e.g. "Ford doesn't report gross profit").
- "source_error" means the underlying lookup itself failed (a network/HTTP problem, a bad \
response) -- this says nothing about whether the data exists. Tell the user the lookup \
failed; do not silently try a different tool or answer as though the data were confirmed \
absent.
- "invalid_input" means the tool call itself was malformed (an unknown ratio or metric name) \
-- fix the call and retry rather than reporting it to the user as a data problem.

You may call multiple tools, in multiple rounds, to fully answer a question -- e.g. pulling a \
statement, then ratios, then anomaly detection, then market data. Only stop calling tools once \
you have what you need to answer completely and accurately. Not an investment advisor: report \
and analyze what the data shows; don't recommend buying, selling, or holding."""


def run_agent(
    question: str,
    client: anthropic.Anthropic | None = None,
    model: str = DEFAULT_MODEL,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """
    Answer `question` using the Claude API with tool calling.

    Loop: send the conversation; if Claude requests tool calls, execute
    them via tools.execute_tool and feed the results back -- up to
    `max_iterations` rounds. Returns as soon as a response comes back with
    no tool calls (stop_reason != "tool_use").

    Hitting `max_iterations` without a natural stop doesn't fail silently:
    the returned dict's `hit_iteration_cap` is True and `final_answer`
    carries a clear note (plus any text Claude had already produced), so a
    caller can't mistake a capped run for a complete one.

    Returns a dict: question, tool_calls (each: iteration, tool_name,
    tool_input, tool_result, is_error), final_answer, hit_iteration_cap,
    iterations_used, stop_reason (of the last model response).
    """
    if max_iterations < 1:
        # A configuration error, not a runtime condition -- there's no way to produce even
        # one model call, so raise loudly rather than return a result with no `iterations_used`.
        raise ValueError("max_iterations must be at least 1")

    client = client or anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]
    trace_calls = []
    final_answer = None
    hit_cap = False
    stop_reason = None

    for iteration in range(1, max_iterations + 1):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=tools.TOOL_DEFINITIONS,
            messages=messages,
        )
        stop_reason = response.stop_reason

        text_blocks = [b.text for b in response.content if b.type == "text"]
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if stop_reason != "tool_use" or not tool_use_blocks:
            final_answer = "\n".join(text_blocks).strip() or "(Claude produced no text response.)"
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            try:
                result_text = tools.execute_tool(block.name, block.input)
                is_error = False
            except Exception as e:  # noqa: BLE001 -- surfaced to the model as a tool error, not raised
                result_text = f"Tool {block.name!r} failed: {e}"
                is_error = True

            trace_calls.append(
                {
                    "iteration": iteration,
                    "tool_name": block.name,
                    "tool_input": block.input,
                    "tool_result": result_text,
                    "is_error": is_error,
                }
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})

        if iteration == max_iterations:
            hit_cap = True
            partial = "\n".join(text_blocks).strip()
            note = (
                f"Reached the {max_iterations}-round tool-call limit before producing a final "
                "answer. This is a safety cap, not a claim that the question is unanswerable -- "
                "the tool calls above ran and returned data; try asking again, possibly "
                "narrower in scope."
            )
            final_answer = f"{partial}\n\n{note}".strip() if partial else note

    return {
        "question": question,
        "tool_calls": trace_calls,
        "final_answer": final_answer,
        "hit_iteration_cap": hit_cap,
        "iterations_used": iteration,
        "stop_reason": stop_reason,
    }

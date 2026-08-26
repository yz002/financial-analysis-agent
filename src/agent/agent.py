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

import json

import anthropic

from . import guardrails, tools

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

If asked to project or forecast a future value, use the forecast_metric tool -- it is the \
only source of a forward-looking figure in this system. Never compute a projection yourself \
under any framing ("a rough estimate," "for illustration," "my own arithmetic on filed \
figures"); if forecast_metric can't produce one (forecast_available is false), say so and \
relay its "reason" rather than filling in your own estimate. When forecast_metric does return \
a projection, always state plainly that it's a projection, not a filed figure, and relay its \
"assumptions" (method, historical periods used, fitted slope/growth rate, fit quality, and any \
seasonal factors) alongside the number -- a projected figure without its assumptions is not a \
complete answer.

If a question requires a number no tool returned -- because the company doesn't report that \
concept, a tool call failed, or you didn't call the right tool -- say so explicitly rather \
than filling in a plausible-sounding value. It is always better to say "not available" than \
to guess.

When you present a figure, cite its source briefly (the filing period and, when useful, \
whether it was derived) -- tool results carry this provenance for exactly this purpose. If a \
tool result flags a value as derived (e.g. a synthesized Q4) or a concept as unreported, say \
so rather than presenting it as an ordinary directly-filed number. If a tool result flags a Q4 \
figure's "q4_diverges_from_subtraction" as true, relay both numbers -- the filed Q4 value and \
its "q4_subtraction_value" -- and explain plainly that they differ because the fiscal-year \
total and the quarters were sourced from filings of different vintages (the FY total having \
picked up a later, possibly differently-tagged restated comparative column that the \
already-filed quarters never got refreshed with); this is a real reporting quirk, not a \
computation error on either side.

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
statement, then ratios, then anomaly detection, then market data, then a forecast. Only stop \
calling tools once \
you have what you need to answer completely and accurately. Not an investment advisor: report \
and analyze what the data shows; don't recommend buying, selling, or holding.

A user can also upload and confirm their own business's financials as a CSV, via the upload \
panel. When one is active, get_csv_statement/get_csv_ratios reach it directly -- there is no \
ticker or other identifier to pass, since at most one CSV is ever active at a time. Use these \
whenever the user refers to "my business," "my company," "our numbers," "the CSV I uploaded," \
or similar, the same way you'd use get_financial_statement/get_ratios for a ticker. If no CSV \
has been uploaded and confirmed yet, these tools return a data_unavailable error explaining \
that -- relay it plainly rather than treating it as a crash or a data problem.

When comparing a CSV-backed business to a ticker-identified company, prefer scale-invariant \
ratios (margins, growth rates, ROA/ROE, debt-to-assets, current ratio) over raw dollar figures \
from get_csv_statement/get_financial_statement -- a small business's revenue or net income \
next to a large-cap's isn't a meaningful comparison on its own, since the two operate at \
entirely different scales. If the user explicitly asks for an absolute-dollar comparison \
anyway, you may state it, but always caveat it plainly with the scale difference in the same \
breath -- never present two raw dollar figures from very different sized companies side by \
side as if they were directly comparable.

get_market_data and get_price_history never apply to a CSV-backed business -- there is no \
traded share price, market cap, or P/E for a private company, and nothing to substitute for \
one. Never call either tool for a CSV-backed business. If asked to compare a CSV-backed \
business's valuation to a public company's (e.g. "is my business worth what public competitors \
trade at"), refuse plainly and explain why rather than attempting a workaround or estimating a \
valuation yourself -- that would also cross into investment-advice territory this tool doesn't \
provide."""


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
    iterations_used, stop_reason (of the last model response), figure_check
    (guardrails.check_figures's report on whether final_answer's numbers
    trace back to tool_calls -- flags only, never alters final_answer).
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
            # Checked up front (rather than relying on the KeyError execute_tool raises for an
            # unknown name) so that case gets its own error_type instead of being indistinguishable
            # from a genuine crash inside a tool function -- both used to surface as the same bare
            # string here, invisible to the error_type branching the system prompt asks for.
            if block.name not in tools.TOOL_NAMES:
                result_text = json.dumps(
                    {
                        "error_type": "invalid_input",
                        "error": f"Unknown tool {block.name!r}; valid tools: {sorted(tools.TOOL_NAMES)}",
                    }
                )
                is_error = True
            else:
                try:
                    result_text = tools.execute_tool(block.name, block.input)
                    is_error = False
                except Exception as e:  # noqa: BLE001 -- a bug in the tool, not evidence data is missing
                    result_text = json.dumps(
                        {
                            "error_type": "source_error",
                            "error": f"Tool {block.name!r} crashed unexpectedly: {e}",
                            "tool_input": block.input,
                        }
                    )
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

    result = {
        "question": question,
        "tool_calls": trace_calls,
        "final_answer": final_answer,
        "hit_iteration_cap": hit_cap,
        "iterations_used": iteration,
        "stop_reason": stop_reason,
    }
    result["figure_check"] = guardrails.check_figures(result)
    return result

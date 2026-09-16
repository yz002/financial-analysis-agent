"""
Reconstructs raw Anthropic-API-shaped conversation history from stored `Turn`
rows, for seeding `run_agent`'s new `prior_messages` parameter (Phase B session
6; see the design doc's SS3.2 for why this must be a structural replay of the
original assistant tool_use / user tool_result blocks, never a model-generated
summary of prior turns).

`turns.tool_calls` (per the design doc's SS3 schema) stores each call as
exactly {iteration, tool_name, tool_input, tool_result, is_error} -- it does
not capture the Anthropic `tool_use_id` that pairs a tool_use block to its
tool_result block, since `agent.py`'s own trace-building never recorded it.
`build_prior_messages` fabricates a fresh id per call instead of leaving that
schema unchanged; this is safe because the Messages API only validates that a
tool_result's tool_use_id matches a tool_use id in the immediately preceding
assistant message of the request being sent right now, never that it matches
some real historical API call.
"""

import itertools

# A "starting tuning knob" per the design doc's SS3 -- how many of a
# conversation's most recent turns get replayed as seeded history. Revisit
# once real token costs from live multi-turn use are observed.
MAX_PRIOR_TURNS = 3


def build_prior_messages(turns) -> list[dict]:
    """
    Build the raw message list for `turns`, given oldest-to-newest and already
    capped to the desired count (capping/ordering is the caller's job -- this
    stays a pure, easily-tested transform). Each `turn` needs only `.id`,
    `.question`, `.final_answer`, and `.tool_calls` (an ORM `Turn` instance
    satisfies this without ever touching the database).
    """
    messages: list[dict] = []
    for turn in turns:
        messages.append({"role": "user", "content": turn.question})

        tool_calls = turn.tool_calls or []
        for iteration, calls in itertools.groupby(tool_calls, key=lambda c: c["iteration"]):
            calls = list(calls)
            tool_use_ids = [f"reconstructed_{turn.id}_{iteration}_{idx}" for idx in range(len(calls))]

            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": call["tool_name"],
                            "input": call["tool_input"],
                        }
                        for call, tool_use_id in zip(calls, tool_use_ids)
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": call["tool_result"],
                            "is_error": call["is_error"],
                        }
                        for call, tool_use_id in zip(calls, tool_use_ids)
                    ],
                }
            )

        # run_agent's own loop never appends its final text-only response to `messages`
        # (it breaks immediately after setting final_answer) -- so this trailing message
        # isn't a literal replay of anything run_agent stored, but is required for the
        # reconstructed history to end on an assistant turn before the new question is
        # appended, per the API's strict role-alternation requirement.
        messages.append({"role": "assistant", "content": turn.final_answer})

    return messages

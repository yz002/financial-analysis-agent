"""
Lightweight usage accounting for eval runs, without touching src/agent/agent.py.

run_agent's `client` parameter accepts anything exposing `.messages.create(...)` -- it doesn't
require the real `anthropic.Anthropic` class. TrackedClient wraps a real client, forwards every
call unchanged, and records each response's `.usage` so a caller can total tokens spent across a
whole run_agent call (which may issue several `.create()` calls, one per tool-calling iteration).
This lives entirely in evals/ rather than as a change to production code, since token accounting
is a Phase 6 eval need, not something the agent layer itself has any use for.
"""


class _TrackedMessages:
    def __init__(self, tracker: "TrackedClient"):
        self._tracker = tracker

    def create(self, **kwargs):
        response = self._tracker._client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._tracker.usage_log.append(usage)
        return response


class TrackedClient:
    def __init__(self, client):
        self._client = client
        self.usage_log: list = []
        self.messages = _TrackedMessages(self)

    def reset(self):
        """Clear accumulated usage -- call between eval runs so each run's totals don't include
        a prior run's tool-calling iterations."""
        self.usage_log = []

    @property
    def total_input_tokens(self) -> int:
        return sum(getattr(u, "input_tokens", 0) or 0 for u in self.usage_log)

    @property
    def total_output_tokens(self) -> int:
        return sum(getattr(u, "output_tokens", 0) or 0 for u in self.usage_log)

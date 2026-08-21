"""
Tests for evals/token_tracking.py, fully offline: the wrapped client is a MagicMock, same style
as tests/test_agent.py's fake Anthropic client. No network, no real Anthropic client.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from evals.token_tracking import TrackedClient


def _response(input_tokens, output_tokens):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))


def test_forwards_create_call_unchanged():
    inner = MagicMock()
    inner.messages.create = MagicMock(return_value=_response(10, 5))
    tracked = TrackedClient(inner)

    response = tracked.messages.create(model="claude-opus-5", max_tokens=100, messages=[])

    inner.messages.create.assert_called_once_with(model="claude-opus-5", max_tokens=100, messages=[])
    assert response.usage.input_tokens == 10


def test_accumulates_usage_across_multiple_calls():
    inner = MagicMock()
    inner.messages.create = MagicMock(side_effect=[_response(10, 5), _response(20, 8)])
    tracked = TrackedClient(inner)

    tracked.messages.create(model="m", max_tokens=1, messages=[])
    tracked.messages.create(model="m", max_tokens=1, messages=[])

    assert tracked.total_input_tokens == 30
    assert tracked.total_output_tokens == 13


def test_reset_clears_accumulated_usage():
    inner = MagicMock()
    inner.messages.create = MagicMock(return_value=_response(10, 5))
    tracked = TrackedClient(inner)

    tracked.messages.create(model="m", max_tokens=1, messages=[])
    assert tracked.total_input_tokens == 10

    tracked.reset()
    assert tracked.total_input_tokens == 0
    assert tracked.total_output_tokens == 0


def test_missing_usage_on_response_is_tolerated():
    inner = MagicMock()
    inner.messages.create = MagicMock(return_value=SimpleNamespace())
    tracked = TrackedClient(inner)

    tracked.messages.create(model="m", max_tokens=1, messages=[])

    assert tracked.total_input_tokens == 0
    assert tracked.total_output_tokens == 0

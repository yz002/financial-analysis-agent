"""
Blocks real Stripe and Anthropic API calls during tests (Phase D session 6).

backend/.env can hold a real Stripe test-mode key and a real Anthropic key, and the backend
test suite runs with it loaded. A test that forgets to mock one of those clients would then
make a real call -- creating Stripe objects, or spending Anthropic tokens -- and could still
pass. block_external_apis patches each SDK at its single request entry point:

- Stripe: every request method of stripe._api_requestor._APIRequestor, which every resource
  call (Subscription.retrieve, checkout.Session.create, billing_portal.Session.create, ...)
  goes through.
- Anthropic: SyncAPIClient.request / AsyncAPIClient.request, which every API call (messages,
  models.list, ...) goes through.

Not httpx2 itself: Starlette's TestClient sends the tests' own requests through httpx2 too.

A blocked call raises RealNetworkCallBlocked (an AssertionError, so the app's `except
stripe.StripeError` / `except anthropic.APIError` handlers don't swallow it) and is also
recorded, so a call inside a broad `except Exception` still fails the test at teardown.
Tests that mock a call (monkeypatching stripe.Subscription.retrieve, app_main.run_agent,
app_main._check_byo_key, ...) never reach these entry points and are unaffected.
"""

from anthropic._base_client import AsyncAPIClient, SyncAPIClient
from stripe._api_requestor import _APIRequestor

_STRIPE_REQUEST_METHODS = (
    "request",
    "request_async",
    "request_raw",
    "request_raw_async",
    "request_stream",
    "request_stream_async",
)


class RealNetworkCallBlocked(AssertionError):
    pass


def block_external_apis(monkeypatch, allow_stripe: bool = False) -> list[str]:
    """Patches the entry points for the rest of the test (monkeypatch undoes it). Returns the
    list that blocked attempts are recorded in, e.g. ["Stripe _APIRequestor.request"]."""
    attempts: list[str] = []

    def _blocked(label: str):
        def _raise(*args, **kwargs):
            attempts.append(label)
            raise RealNetworkCallBlocked(
                f"a test made a real {label} call; mock it (backend/tests_support/network_guard.py)"
            )

        return _raise

    if not allow_stripe:
        for name in _STRIPE_REQUEST_METHODS:
            monkeypatch.setattr(_APIRequestor, name, _blocked(f"Stripe _APIRequestor.{name}"))
    for cls in (SyncAPIClient, AsyncAPIClient):
        monkeypatch.setattr(cls, "request", _blocked(f"Anthropic {cls.__name__}.request"))
    return attempts

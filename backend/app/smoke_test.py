"""
One-off smoke test for sessions 2/3: confirm every endpoint responds with the
shape its Pydantic response model promises. Not a permanent pytest suite --
this session's scope is "confirm the route shape works end to end," matching
db/smoke_test.py's session-1 pattern. Run directly:

    backend/.venv/Scripts/python.exe -m app.smoke_test

from inside backend/. /v1/health needs DATABASE_URL set (backend/.env) to
reach a live Postgres connection. /v1/ask and /v1/csv/{id}/propose-mapping
are wired to real Anthropic calls (as of sessions 3 and 4 respectively), so
running this needs ANTHROPIC_API_KEY and SEC_USER_AGENT set, makes real
(billed) Anthropic API calls, and writes real conversations/turns/
csv_statements rows. Since Phase C session 4, every route below /v1/health
requires a real session token -- oauth_providers.verify_google_token is
monkeypatched here (never a real Google call) so /v1/auth/exchange can issue
one, the same idiom tests/test_auth_exchange.py uses.
"""

import sys
import uuid

from fastapi.testclient import TestClient

from . import main as app_main
from .main import app
from .oauth_providers import ProviderIdentity

client = TestClient(app)


def main() -> int:
    failures = []
    identity = ProviderIdentity(
        provider="google", subject=f"smoke-{uuid.uuid4()}", email=f"smoke-{uuid.uuid4()}@example.com"
    )
    app_main.oauth_providers.verify_google_token = lambda oauth_token: identity

    resp = client.get("/v1/health")
    print(f"GET /v1/health -> {resp.status_code} {resp.json()}")
    if resp.status_code != 200 or resp.json().get("db") != "ok":
        failures.append(f"/v1/health: expected 200 with db=ok, got {resp.status_code} {resp.json()}")

    resp = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "smoke-token"})
    print(f"POST /v1/auth/exchange -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/auth/exchange: expected 200, got {resp.status_code} {resp.text}")
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    headers = {"Authorization": f"Bearer {resp.json()['session_token']}"}

    resp = client.post(
        "/v1/ask",
        json={"question": "What was NVDA's revenue last quarter?"},
        headers=headers,
    )
    print(f"POST /v1/ask -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/ask: expected 200, got {resp.status_code} {resp.text}")

    resp = client.post(
        "/v1/csv/parse",
        json={"rows": [["period_end", "revenue"], ["2024-12-31", "1000"]], "filename": "Sheet1!A1:B2.csv"},
        headers=headers,
    )
    print(f"POST /v1/csv/parse -> {resp.status_code} {resp.json()}")
    if resp.status_code != 200 or not resp.json().get("csv_context_id"):
        failures.append(f"/v1/csv/parse: expected 200 with a csv_context_id, got {resp.status_code} {resp.json()}")
    csv_context_id = resp.json().get("csv_context_id", "stub-id")

    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping", headers=headers)
    print(f"POST /v1/csv/{{id}}/propose-mapping -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/csv/{{id}}/propose-mapping: expected 200, got {resp.status_code} {resp.text}")

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={"mapping": {"period_end": "period_end", "revenue": "revenue"}, "entity_name": "Acme Co"},
        headers=headers,
    )
    print(f"POST /v1/csv/{{id}}/confirm -> {resp.status_code} {resp.json()}")
    if resp.status_code != 200:
        failures.append(f"/v1/csv/{{id}}/confirm: expected 200, got {resp.status_code} {resp.text}")

    resp = client.get("/v1/usage", headers=headers)
    print(f"GET /v1/usage -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/usage: expected 200, got {resp.status_code} {resp.text}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS: all endpoints responded with the expected shape.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

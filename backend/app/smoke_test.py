"""
One-off smoke test for session 2: confirm every stubbed endpoint responds
with the shape its Pydantic response model promises. Not a permanent pytest
suite -- this session's scope is "confirm the route shape works end to end,"
matching db/smoke_test.py's session-1 pattern. Run directly:

    backend/.venv/Scripts/python.exe -m app.smoke_test

from inside backend/. /v1/health additionally needs DATABASE_URL set
(backend/.env) to reach a live Postgres connection; every other endpoint
needs nothing beyond the app itself.
"""

import sys

from fastapi.testclient import TestClient

from .main import app

client = TestClient(app)


def main() -> int:
    failures = []

    resp = client.get("/v1/health")
    print(f"GET /v1/health -> {resp.status_code} {resp.json()}")
    if resp.status_code != 200 or resp.json().get("db") != "ok":
        failures.append(f"/v1/health: expected 200 with db=ok, got {resp.status_code} {resp.json()}")

    resp = client.post(
        "/v1/ask",
        json={"question": "What was NVDA's revenue last quarter?"},
    )
    print(f"POST /v1/ask -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/ask: expected 200, got {resp.status_code} {resp.text}")

    resp = client.post(
        "/v1/csv/parse",
        json={"rows": [["period_end", "revenue"], ["2024-12-31", "1000"]], "filename": "Sheet1!A1:B2.csv"},
    )
    print(f"POST /v1/csv/parse -> {resp.status_code} {resp.json()}")
    if resp.status_code != 200 or not resp.json().get("csv_context_id"):
        failures.append(f"/v1/csv/parse: expected 200 with a csv_context_id, got {resp.status_code} {resp.json()}")
    csv_context_id = resp.json().get("csv_context_id", "stub-id")

    resp = client.post(f"/v1/csv/{csv_context_id}/propose-mapping")
    print(f"POST /v1/csv/{{id}}/propose-mapping -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/csv/{{id}}/propose-mapping: expected 200, got {resp.status_code} {resp.text}")

    resp = client.post(
        f"/v1/csv/{csv_context_id}/confirm",
        json={"mapping": {"period_end": "period_end", "revenue": "revenue"}, "entity_name": "Acme Co"},
    )
    print(f"POST /v1/csv/{{id}}/confirm -> {resp.status_code} {resp.json()}")
    if resp.status_code != 200:
        failures.append(f"/v1/csv/{{id}}/confirm: expected 200, got {resp.status_code} {resp.text}")

    resp = client.post(
        "/v1/install",
        json={"identity_type": "uuid", "identity_value": "test-uuid"},
    )
    print(f"POST /v1/install -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/install: expected 200, got {resp.status_code} {resp.text}")

    resp = client.get("/v1/usage")
    print(f"GET /v1/usage -> {resp.status_code}")
    if resp.status_code != 200:
        failures.append(f"/v1/usage: expected 200, got {resp.status_code} {resp.text}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS: all 7 endpoints responded with the expected stubbed shape.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

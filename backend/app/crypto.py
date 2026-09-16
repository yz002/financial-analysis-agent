"""
Encrypts/decrypts BYO Anthropic API keys at rest (design doc SS4's "BYO API key
encryption at rest" subsection): Fernet (AES-128-CBC + HMAC, symmetric, authenticated)
via the `cryptography` library, with the Fernet key held only as the
BYO_KEY_ENCRYPTION_KEY Render env var -- never alongside byo_keys.encrypted_key in
Postgres -- so a leaked DB backup alone doesn't expose plaintext keys.

BYO_KEY_ENCRYPTION_KEY is read fresh from os.environ inside _get_fernet, never cached
at module import -- matching db/base.py's get_database_url() / billing.py's Stripe
secret convention (fail loud at call time, not import time; also lets tests
monkeypatch.setenv cleanly).
"""

import os
import re

from cryptography.fernet import Fernet

# Anthropic API keys are currently issued as sk-ant-api03-<...>; the prefix check below
# is deliberately just "sk-ant-" (not the full "api03") so a future key-version bump
# doesn't need a code change here. This is a fast-fail format check only -- never a live
# Anthropic API call, which would spend the caller's own quota before they've confirmed
# they want to use this key at all -- so it catches obvious typos/pastes, not every
# invalid key.
_KEY_FORMAT = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")


def is_valid_byo_key_format(raw_key: str) -> bool:
    return bool(_KEY_FORMAT.match(raw_key.strip()))


def _get_fernet() -> Fernet:
    secret = os.environ.get("BYO_KEY_ENCRYPTION_KEY")
    if not secret:
        raise RuntimeError(
            "BYO_KEY_ENCRYPTION_KEY is not set. Copy backend/.env.example to backend/.env "
            "and fill it in with a generated Fernet key -- never hardcode it."
        )
    return Fernet(secret.encode())


def encrypt_byo_key(raw_key: str) -> bytes:
    return _get_fernet().encrypt(raw_key.encode())


def decrypt_byo_key(encrypted_key: bytes) -> str:
    return _get_fernet().decrypt(encrypted_key).decode()

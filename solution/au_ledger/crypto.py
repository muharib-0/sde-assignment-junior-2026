"""PII protection at rest.

Two different tools for two different jobs:

* Encryption (Fernet = AES-128-CBC + HMAC-SHA256): PII fields are stored as
  ciphertext, so a leaked database file reveals nothing. Encryption is
  randomised (same input gives different ciphertext each time), which means
  you can't look anything up by it...

* ...so for lookups we keep a *blind index*: HMAC-SHA256(secret, canonical phone).
  It's deterministic, so an event's phone and a lead's phone give the same value
  and can be joined. Because it's *keyed*, an attacker can't just hash all ~4 billion
  Indian mobile numbers to reverse it, which they could with a plain SHA-256.

Both keys are derived from one secret (env var AU_PII_SECRET). In production that
secret would live in a KMS / secrets manager, never next to the database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys

from cryptography.fernet import Fernet

DEV_SECRET = "dev-only-insecure-secret-change-me"
_warned = False


def _secret_from_env() -> bytes:
    global _warned
    secret = os.environ.get("AU_PII_SECRET")
    if not secret:
        if not _warned:
            print("warning: AU_PII_SECRET not set; using the built-in development secret",
                  file=sys.stderr)
            _warned = True
        secret = DEV_SECRET
    return secret.encode()


class PiiVault:
    def __init__(self, secret: bytes | None = None):
        secret = secret if secret is not None else _secret_from_env()
        enc_key = hmac.new(secret, b"au-ledger/encrypt/v1", hashlib.sha256).digest()
        self._idx_key = hmac.new(secret, b"au-ledger/blind-index/v1", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))

    @property
    def fingerprint(self) -> str:
        """Identifies the key without revealing it, so we can refuse to mix keys in one DB."""
        return hashlib.sha256(self._idx_key).hexdigest()[:16]

    def blind_index(self, canonical_phone: str) -> str:
        return hmac.new(self._idx_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data, sort_keys=True).encode())

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))

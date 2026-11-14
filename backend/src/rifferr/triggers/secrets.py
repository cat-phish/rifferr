"""Webhook secrets (TRIGGERS-17). Only SHA-256 hashes are stored; the secret itself is
shown once when created and never logged."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from rifferr.appdb import AppTriggers


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_secret(store: AppTriggers, profile: str, name: str) -> str:
    """Create or replace the secret of webhook ``name``; returns it (shown once)."""
    secret = secrets.token_urlsafe(32)
    store.set_webhook_hash(profile, name, _hash(secret))
    return secret


def credential(authorization: str | None) -> str | None:
    """The secret in ``Authorization: Bearer <secret>`` or Basic (the password)."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    value = value.strip()
    if scheme.lower() == "bearer":
        return value or None
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        _, sep, password = decoded.partition(":")
        return password if sep and password else None
    return None


def verify(store: AppTriggers, profile: str, name: str, candidate: str | None) -> bool:
    """Constant-time check of ``candidate`` against the stored hash."""
    stored = store.webhook_hash(profile, name)
    # Compare even without a stored hash or candidate, so timing doesn't tell them apart.
    digest = _hash(candidate or "")
    ok = hmac.compare_digest(digest, stored or "0" * 64)
    return ok and stored is not None and candidate is not None

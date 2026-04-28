"""Optional shared-password auth for the dashboard."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

SESSION_COOKIE = "l2g_session"
SESSION_TTL = 30 * 24 * 3600


def get_password() -> str | None:
    """Return the configured dashboard password, if auth is enabled."""
    return os.environ.get("L2G_PASSWORD") or None


def auth_enabled() -> bool:
    """Return True when dashboard password auth is enabled."""
    return bool(get_password())


def _secret() -> bytes:
    password = get_password()
    if not password:
        raise RuntimeError("L2G_PASSWORD not set")
    return hashlib.sha256(f"l2g-session-{password}".encode()).digest()


def sign_session() -> str:
    """Create a signed session cookie value."""
    timestamp = str(int(time.time()))
    signature = hmac.new(_secret(), f"v1.{timestamp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"v1.{timestamp}.{signature}"


def verify_session(cookie: str | None) -> bool:
    """Return True when auth is disabled or the cookie is valid."""
    if not auth_enabled():
        return True
    if not cookie:
        return False
    try:
        version, timestamp_raw, signature = cookie.split(".")
        if version != "v1":
            return False
        timestamp = int(timestamp_raw)
        if time.time() - timestamp > SESSION_TTL:
            return False
        expected = hmac.new(_secret(), f"v1.{timestamp_raw}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(signature, expected)
    except (TypeError, ValueError):
        return False


def check_password(candidate: str) -> bool:
    """Compare a candidate password against L2G_PASSWORD."""
    password = get_password()
    if not password:
        return False
    return hmac.compare_digest(candidate.encode(), password.encode())

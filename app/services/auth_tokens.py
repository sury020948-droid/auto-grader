"""Stateless auth tokens (HMAC-SHA256, stdlib only) + Google OAuth helpers."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config

_TOKEN_TTL_SECS = 30 * 24 * 3600  # 30 days

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def oauth_configured() -> bool:
    return bool(
        os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
    )


def _secret() -> bytes:
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env.encode()
    path = Path(config.data_dir()) / "session_secret.key"
    try:
        return path.read_text(encoding="utf-8").strip().encode()
    except OSError:
        key = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key.encode()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def issue_token(uid: int, ttl_secs: int = _TOKEN_TTL_SECS) -> str:
    payload = json.dumps(
        {"uid": uid, "exp": int(time.time()) + ttl_secs}, separators=(",", ":")
    ).encode()
    body = _b64e(payload)
    sig = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> int | None:
    """Return the user id for a valid, unexpired token; else None."""
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(sig, _b64e(expected)):
            return None
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    uid = payload.get("uid")
    return int(uid) if isinstance(uid, int) else None


def google_redirect_uri(origin: str) -> str:
    return origin.rstrip("/") + "/api/auth/google/callback"


def build_google_auth_url(origin: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": config.GOOGLE_CLIENT_ID,
            "redirect_uri": google_redirect_uri(origin),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        }
    )
    return f"{_GOOGLE_AUTH_URL}?{params}"


def exchange_code(code: str, origin: str) -> dict | None:
    """Exchange an authorization code for Google identity claims.

    The id_token comes straight from Google's token endpoint over HTTPS
    (server-to-server), so decoding its payload without signature
    verification is acceptable per Google's guidance.
    """
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": google_redirect_uri(origin),
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_resp = json.loads(resp.read())
    except (OSError, ValueError):
        return None
    id_token = token_resp.get("id_token")
    if not id_token or "." not in id_token:
        return None
    try:
        claims_b64 = id_token.split(".")[1]
        claims = json.loads(_b64d(claims_b64))
    except (ValueError, TypeError):
        return None
    return claims if isinstance(claims, dict) else None


def make_state() -> str:
    return secrets.token_urlsafe(24)


def verify_state(received: str, expected: str) -> bool:
    return bool(received) and hmac.compare_digest(received, expected)

"""Request-scoped authentication dependency.

When GOOGLE_CLIENT_ID/SECRET are configured, every request must carry a valid
Bearer token and resolves to its own user. Without OAuth configured (e.g. local
development), all traffic falls back to a single shared 'local' user so the app
stays usable offline — the frontend shows a prominent warning banner for this.
"""

from typing import Any

from fastapi import Depends, HTTPException, Request

from . import db as dal
from .db import get_conn
from .services import auth_tokens


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def oauth_enabled() -> bool:
    return auth_tokens.oauth_configured()


def ensure_local_user(conn) -> dict[str, Any]:
    user = dal.get_user_by_sub(conn, "local")
    if user:
        return user
    return dal.upsert_google_user(
        conn, "local", "local@auto-grader", "로컬 사용자", ""
    )


def get_current_user(request: Request, conn=Depends(get_conn)) -> dict[str, Any]:
    """Resolve the requesting user; 401 when OAuth is on and token invalid."""
    if not oauth_enabled():
        return ensure_local_user(conn)

    uid = auth_tokens.verify_token(_bearer_token(request))
    if uid is None:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    user = dal.get_user(conn, uid)
    if not user:
        raise HTTPException(status_code=401, detail="유효하지 않은 사용자입니다.")
    return user

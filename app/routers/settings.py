import os

from fastapi import APIRouter, Depends

from .. import config
from .. import db as dal
from ..db import get_conn
from ..deps import get_current_user
from ..errors import AppError
from ..schemas import ApiKeyPayload

router = APIRouter(tags=["settings"])


def _env_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
        or config.GEMINI_API_KEY
    )


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def _status(user: dict) -> dict:
    key = user.get("gemini_api_key") or ""
    if key:
        return {"set": True, "source": "user", "masked": _mask(key)}
    if _env_key():  # server-wide env fallback
        return {"set": True, "source": "server", "masked": _mask(_env_key())}
    return {"set": False, "source": None, "masked": None}


def resolve_user_key(
    user: dict,
    conn,
    header_key: str | None = None,
) -> str:
    """Per-request Gemini key precedence:
    X-Gemini-API-Key header > device's saved key > server env fallback.

    Any non-empty value is accepted here — the actual Google AI API response
    is the ultimate source of truth for key validity."""
    if header_key and header_key.strip():
        return header_key.strip()
    stored = user.get("gemini_api_key") or ""
    if stored:
        return stored
    return _env_key()


@router.get("/settings/api-key")
def get_api_key_status(user: dict = Depends(get_current_user)):
    """Report the current device's key status — never returns the raw key."""
    return _status(user)


@router.post("/settings/api-key")
def save_api_key(
    payload: ApiKeyPayload,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    """Accept any non-empty string as the key; Google's API validates it."""
    key = payload.api_key.strip()
    if not key:
        raise AppError(400, "API 키를 입력해 주세요.")
    dal.set_user_api_key(conn, int(user["id"]), key)
    return _status({**user, "gemini_api_key": key})


@router.delete("/settings/api-key")
def delete_api_key(user: dict = Depends(get_current_user), conn=Depends(get_conn)):
    dal.set_user_api_key(conn, int(user["id"]), "")
    return _status({**user, "gemini_api_key": ""})

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


def validate_gemini_key(key: str) -> str:
    """Gemini keys issued by Google AI Studio start with 'AIza'.
    Reject other Google credential formats (e.g. OAuth codes 'AQ...') early."""
    if not key.startswith("AIza"):
        raise AppError(
            400,
            "Gemini API 키는 'AIza'로 시작해야 합니다. 입력하신 값은 다른 종류의"
            " Google 자격증명(예: 'AQ...'로 시작하는 OAuth 코드)으로 보입니다."
            " Google AI Studio에서 발급한 키를 사용해 주세요.",
        )
    if len(key) < 30:
        raise AppError(400, "API 키 형식이 올바르지 않습니다 (길이가 너무 짧습니다).")
    return key


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
    X-Gemini-API-Key header > user's saved key > server env fallback."""
    if header_key and header_key.strip():
        return validate_gemini_key(header_key.strip())
    stored = user.get("gemini_api_key") or ""
    if stored:
        return stored
    return _env_key()


@router.get("/settings/api-key")
def get_api_key_status(user: dict = Depends(get_current_user)):
    """Report the current user's key status — never returns the raw key."""
    return _status(user)


@router.post("/settings/api-key")
def save_api_key(
    payload: ApiKeyPayload,
    user: dict = Depends(get_current_user),
    conn=Depends(get_conn),
):
    key = payload.api_key.strip()
    if not key:
        raise AppError(400, "API 키를 입력해 주세요.")
    validate_gemini_key(key)
    dal.set_user_api_key(conn, int(user["id"]), key)
    return _status({**user, "gemini_api_key": key})


@router.delete("/settings/api-key")
def delete_api_key(user: dict = Depends(get_current_user), conn=Depends(get_conn)):
    dal.set_user_api_key(conn, int(user["id"]), "")
    return _status({**user, "gemini_api_key": ""})

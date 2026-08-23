from fastapi import APIRouter

from .. import config
from ..errors import AppError
from ..schemas import ApiKeyPayload

router = APIRouter(tags=["settings"])


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def _status() -> dict:
    stored = config.stored_api_key()
    if stored:
        return {"set": True, "source": "app", "masked": _mask(stored)}
    if config.GEMINI_API_KEY:
        return {"set": True, "source": "env", "masked": _mask(config.GEMINI_API_KEY)}
    return {"set": False, "source": None, "masked": None}


@router.get("/settings/api-key")
def get_api_key_status():
    """Report whether a Gemini API key is active — never returns the raw key."""
    return _status()


@router.post("/settings/api-key")
def save_api_key(payload: ApiKeyPayload):
    key = payload.api_key.strip()
    if not key:
        raise AppError(400, "API 키를 입력해 주세요.")
    config.save_api_key(key)
    return _status()


@router.delete("/settings/api-key")
def delete_api_key():
    config.clear_api_key()
    return _status()

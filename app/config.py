import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get(
    "GOOGLE_API_KEY", ""
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_TIMEOUT_SECS = float(os.environ.get("GEMINI_TIMEOUT_SECS", "60"))
GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "2"))
GEMINI_TEMPERATURE = 0.0


def data_dir() -> Path:
    return Path(os.environ.get("AUTO_GRADER_DATA_DIR", str(BASE_DIR / "data")))


def db_path() -> Path:
    return data_dir() / "auto-grader.db"


def upload_dir() -> Path:
    return data_dir() / "uploads"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def _read_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(settings: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def stored_api_key() -> str:
    """API key saved from the in-app settings UI ('' when absent)."""
    v = _read_settings().get("gemini_api_key", "")
    return v if isinstance(v, str) else ""


def load_runtime_settings() -> None:
    """Reset GEMINI_API_KEY from env, then apply the saved override (if any)."""
    global GEMINI_API_KEY
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get(
        "GOOGLE_API_KEY", ""
    )
    key = stored_api_key()
    if key:
        GEMINI_API_KEY = key


def save_api_key(key: str) -> None:
    global GEMINI_API_KEY
    settings = _read_settings()
    settings["gemini_api_key"] = key
    _write_settings(settings)
    GEMINI_API_KEY = key


def clear_api_key() -> None:
    global GEMINI_API_KEY
    settings = _read_settings()
    settings.pop("gemini_api_key", None)
    _write_settings(settings)
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get(
        "GOOGLE_API_KEY", ""
    )

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_EXTRACT_IMAGES = 10

# --- Gemini (server-wide FALLBACK only; devices normally bring their own key) ---
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

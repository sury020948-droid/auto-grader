from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .config import STATIC_DIR, upload_dir
from .db import get_conn, init_db
from .deps import try_current_user
from .errors import AppError
from .routers import attempts, extraction, settings, workbooks
from .services import gemini


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    upload_dir().mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Smart Auto-Grader", version="3.0.0", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(workbooks.router, prefix="/api")
    app.include_router(extraction.router, prefix="/api")
    app.include_router(attempts.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")

    @app.get("/api/health")
    def health(
        user: dict | None = Depends(try_current_user),
        conn=Depends(get_conn),
    ):
        import os

        key = ""
        if user is not None:
            key = user.get("gemini_api_key") or ""
        key = (
            key
            or os.environ.get("GEMINI_API_KEY", "")
            or os.environ.get("GOOGLE_API_KEY", "")
        )
        return {
            "status": "ok",
            "gemini_available": gemini.available(key),
            "model": config.GEMINI_MODEL,
        }

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()

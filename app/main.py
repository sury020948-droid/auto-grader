from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .config import STATIC_DIR, upload_dir
from .db import init_db
from .errors import AppError
from .routers import attempts, extraction, settings, workbooks
from .services import gemini


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load_runtime_settings()
    init_db()
    upload_dir().mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Smart Auto-Grader", version="2.0.0", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(workbooks.router, prefix="/api")
    app.include_router(extraction.router, prefix="/api")
    app.include_router(attempts.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "gemini_available": gemini.available(),
            "model": config.GEMINI_MODEL,
        }

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()

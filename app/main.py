from contextlib import asynccontextmanager
import logging
import time
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api.router import api_router
from app.core.config import get_settings
from app.core.firebase import get_firebase_app
from app.core.logging import configure_logging
from app.services.background_jobs import background_jobs
from app.services.ai_request_queue import ai_request_queue
from app.services.notifications import notification_scheduler

settings = get_settings()
configure_logging()
logger = logging.getLogger("stylestack.api")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_firebase_app()
    ai_request_queue.start()
    background_jobs.start()
    notification_scheduler.start()
    logger.info("application_started environment=%s", settings.environment)
    yield
    background_jobs.stop()
    ai_request_queue.stop()
    notification_scheduler.stop()
    logger.info("application_stopped")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "request_failed method=%s path=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "request_completed method=%s path=%s status=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


app.include_router(api_router, prefix=settings.api_v1_prefix)

"""Page Pulse — production-grade URL audit service.

Built for Digital Heroes Training Task (https://digitalheroesco.com).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .auditor import URLAuditor
from .config import Settings, get_settings
from .logging_setup import bind_request_id, request_id_ctx, setup_logging
from .models import (
    AuditRequest,
    AuditResult,
    BatchAuditRequest,
    BatchAuditResponse,
    ErrorResponse,
    HealthResponse,
)
from .storage import (
    CacheStore,
    MemoryCache,
    MemoryRateLimiter,
    MongoCache,
    MongoRateLimiter,
    RateLimitStore,
)

VERSION = "1.0.0"
REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("page_pulse")


class RateLimitExceeded(Exception):
    def __init__(self, limit: int, reset_in: int) -> None:
        self.limit = limit
        self.reset_in = reset_in


class ServiceState:
    """Everything with a lifecycle, kept off module globals for testability."""

    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.auditor: URLAuditor | None = None
        self.cache: CacheStore | None = None
        self.rate_limiter: RateLimitStore | None = None
        self.storage_backend: str = "memory"
        self.started_at: float = time.time()
        self._mongo_client: Any = None

    async def startup(self) -> None:
        self.settings = get_settings()
        setup_logging(self.settings.log_level)
        self.started_at = time.time()

        self.auditor = URLAuditor(self.settings)
        await self.auditor.startup()

        if self.settings.use_mongo:
            from motor.motor_asyncio import AsyncIOMotorClient

            self._mongo_client = AsyncIOMotorClient(
                self.settings.mongo_url,
                maxPoolSize=200,
                serverSelectionTimeoutMS=5000,
            )
            db = self._mongo_client[self.settings.db_name]
            cache = MongoCache(db)
            await cache.ensure_indexes()
            self.cache = cache
            self.rate_limiter = MongoRateLimiter(db)
            self.storage_backend = "mongodb"
        else:
            self.cache = MemoryCache()
            self.rate_limiter = MemoryRateLimiter()
            self.storage_backend = "memory"

        logger.info(
            "service.started",
            extra={
                "version": VERSION,
                "storage": self.storage_backend,
                "max_concurrency": self.settings.max_concurrency,
                "cache_ttl_seconds": self.settings.cache_ttl_seconds,
            },
        )

    async def shutdown(self) -> None:
        if self.auditor:
            await self.auditor.shutdown()
        if self._mongo_client is not None:
            self._mongo_client.close()
            self._mongo_client = None
        logger.info("service.stopped")


state = ServiceState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await state.startup()
    try:
        yield
    finally:
        await state.shutdown()


app = FastAPI(
    title="Page Pulse",
    description="Production-grade URL audit service with caching, rate limiting and structured logging.",
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER, "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
)


# --------------------------------------------------------------------------- #
# Cross-cutting concerns
# --------------------------------------------------------------------------- #


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    token = bind_request_id(request_id)
    request.state.request_id = request_id
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request.unhandled",
            extra={"method": request.method, "path": request.url.path},
        )
        response = _error_response(500, "internal_error", "An unexpected error occurred.", request_id)
    finally:
        request_id_ctx.reset(token)

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers[REQUEST_ID_HEADER] = request_id
    logger.info(
        "request.completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


def _error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse.model_validate(
        {"error": {"code": code, "message": message, "request_id": request_id, "details": details}}
    )
    return JSONResponse(status_code=status, content=jsonable_encoder(body), headers=headers)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return _error_response(
        422,
        "validation_error",
        "Request payload failed validation.",
        _request_id(request),
        {"errors": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return _error_response(
        429,
        "rate_limit_exceeded",
        f"Client exceeded {exc.limit} requests per window.",
        _request_id(request),
        {"limit": exc.limit, "reset_in_seconds": exc.reset_in},
        headers={
            "X-RateLimit-Limit": str(exc.limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(exc.reset_in),
            "Retry-After": str(exc.reset_in),
        },
    )


def resolve_client_id(request: Request, header_client_id: str | None, body_client_id: str | None) -> str:
    """Header wins, then body, then peer IP. Never trust an empty string."""
    for candidate in (header_client_id, body_client_id):
        if candidate and candidate.strip():
            return candidate.strip()[:128]
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "anonymous"


async def enforce_rate_limit(request: Request, client_id: str, cost: int = 1) -> tuple[int, int]:
    settings = state.settings
    assert state.rate_limiter is not None

    allowed, remaining, reset_in = await state.rate_limiter.hit(
        client_id,
        settings.rate_limit_requests,
        settings.rate_limit_window_seconds,
        cost=cost,
    )

    if not allowed:
        logger.warning(
            "ratelimit.blocked",
            extra={
                "request_id": _request_id(request),
                "client_id": client_id,
                "cost": cost,
                "reset_in_seconds": reset_in,
            },
        )
        raise RateLimitExceeded(settings.rate_limit_requests, reset_in)

    return remaining, reset_in


def _cache_key(url: str) -> str:
    return "audit:" + hashlib.sha256(url.encode("utf-8")).hexdigest()


async def _audit_with_cache(url: str, request_id: str) -> AuditResult:
    """Cache lookup, then audit, then store. Failures are never cached."""
    assert state.cache is not None and state.auditor is not None

    key = _cache_key(url)
    cached = await state.cache.get(key)
    if cached is not None:
        logger.info("cache.hit", extra={"request_id": request_id, "url": url})
        result = AuditResult.model_validate(cached)
        result.cached = True
        result.request_id = request_id
        return result

    logger.info("cache.miss", extra={"request_id": request_id, "url": url})
    result = await state.auditor.audit(url, request_id)

    if result.error is None:
        await state.cache.set(key, jsonable_encoder(result), state.settings.cache_ttl_seconds)
    return result


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

api = APIRouter(prefix="/api")


@api.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="page-pulse",
        version=VERSION,
        storage=state.storage_backend,
        uptime_seconds=round(time.time() - state.started_at, 2),
    )


@api.get("/ready", tags=["ops"])
async def ready(request: Request):
    """Readiness differs from liveness: it proves the store answers."""
    try:
        assert state.cache is not None
        await state.cache.get("readiness-probe")
        return {"status": "ready", "storage": state.storage_backend}
    except Exception as exc:
        logger.error("readiness.failed", extra={"error": str(exc)})
        return _error_response(503, "not_ready", "Storage backend is unavailable.", _request_id(request))


@api.post(
    "/audit",
    response_model=AuditResult,
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    tags=["audit"],
)
async def audit_single(
    request: Request,
    payload: AuditRequest,
    x_client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    request_id = _request_id(request)
    client_id = resolve_client_id(request, x_client_id, payload.client_id)
    remaining, reset_in = await enforce_rate_limit(request, client_id)

    result = await _audit_with_cache(payload.url, request_id)

    return JSONResponse(
        content=jsonable_encoder(result),
        headers={
            "X-RateLimit-Limit": str(state.settings.rate_limit_requests),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_in),
            "X-Cache": "HIT" if result.cached else "MISS",
        },
    )


@api.post(
    "/audit/batch",
    response_model=BatchAuditResponse,
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    tags=["audit"],
)
async def audit_batch(
    request: Request,
    payload: BatchAuditRequest,
    x_client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    request_id = _request_id(request)
    max_batch = state.settings.max_batch_size

    if len(payload.urls) > max_batch:
        return _error_response(
            422,
            "batch_too_large",
            f"A batch may contain at most {max_batch} URLs.",
            request_id,
            {"submitted": len(payload.urls), "max": max_batch},
        )

    client_id = resolve_client_id(request, x_client_id, None)
    # A batch of N costs N units so one client cannot bypass the limit by batching.
    remaining, reset_in = await enforce_rate_limit(request, client_id, cost=len(payload.urls))

    results: list[AuditResult] = list(
        await asyncio.gather(*(_audit_with_cache(item.url, request_id) for item in payload.urls))
    )

    body = BatchAuditResponse(request_id=request_id, count=len(results), results=results)
    return JSONResponse(
        content=jsonable_encoder(body),
        headers={
            "X-RateLimit-Limit": str(state.settings.rate_limit_requests),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_in),
        },
    )


@api.post("/cache/purge", tags=["ops"])
async def purge_cache():
    assert state.cache is not None
    removed = await state.cache.purge_expired()
    logger.info("cache.purged", extra={"removed": removed})
    return {"removed": removed}


app.include_router(api)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"


@app.get("/", include_in_schema=False)
async def root(request: Request):
    """Serve the console when the static bundle is present, JSON otherwise."""
    wants_html = "text/html" in request.headers.get("accept", "")
    if wants_html and INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    return {
        "service": "page-pulse",
        "version": VERSION,
        "docs": "/docs",
        "health": "/api/health",
        "credit": "Built for Digital Heroes Training Task — https://digitalheroesco.com",
    }

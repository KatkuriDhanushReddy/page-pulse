"""Pydantic request/response models — the public API contract lives here."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_SCHEMES = {"http", "https"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditRequest(BaseModel):
    """A single audit request."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., max_length=2048, description="Absolute http(s) URL to audit")
    client_id: str | None = Field(
        default=None,
        max_length=128,
        description="Optional client identifier used for rate limiting",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")

        parsed = urlparse(value)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            raise ValueError("url must use the http:// or https:// scheme")
        if not parsed.hostname:
            raise ValueError("url must include a hostname")
        if "." not in parsed.hostname and parsed.hostname != "localhost":
            raise ValueError("url hostname must be fully qualified")
        return value


class BatchAuditRequest(BaseModel):
    """Wrapper so batch payloads can grow options without a breaking change."""

    model_config = ConfigDict(extra="forbid")

    urls: list[AuditRequest] = Field(..., min_length=1)


class SSLInfo(BaseModel):
    valid: bool
    issuer: str | None = None
    subject: str | None = None
    expires_at: str | None = None
    days_remaining: int | None = None
    error: str | None = None


class MetaTags(BaseModel):
    title: str | None = None
    description: str | None = None
    canonical: str | None = None
    robots: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    h1_count: int = 0


class PerformanceMetrics(BaseModel):
    total_time_ms: float
    ttfb_ms: float | None = None
    content_bytes: int | None = None
    redirect_count: int = 0
    truncated: bool = False


class AuditResult(BaseModel):
    """Successful (or gracefully degraded) audit outcome."""

    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    final_url: str | None = None
    status_code: int | None = None
    accessible: bool = False
    response_time_ms: float = 0.0
    ssl: SSLInfo | None = None
    meta: MetaTags | None = None
    performance: PerformanceMetrics
    headers: dict[str, str] = Field(default_factory=dict)
    error: dict[str, str] | None = None
    cached: bool = False
    checked_at: datetime = Field(default_factory=_utcnow)


class BatchAuditResponse(BaseModel):
    request_id: str
    count: int
    results: list[AuditResult]


class ErrorBody(BaseModel):
    """Every non-2xx response uses this shape."""

    code: str
    message: str
    request_id: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    storage: str
    uptime_seconds: float
    checked_at: datetime = Field(default_factory=_utcnow)


class RateLimitDecision(BaseModel):
    allowed: bool
    limit: int
    remaining: int
    reset_in_seconds: int

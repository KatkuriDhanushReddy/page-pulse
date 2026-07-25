"""Runtime configuration, loaded once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Service settings.

    Every knob the reviewer asked to be "configurable" lives here so there is a
    single place to audit defaults.
    """

    # Storage. When mongo_url is empty the service falls back to in-process
    # stores, which keeps local dev and CI free of external dependencies.
    mongo_url: str = field(default_factory=lambda: os.getenv("MONGO_URL", "").strip())
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "page_pulse"))

    # Outbound request behaviour.
    request_timeout_seconds: float = field(default_factory=lambda: _float("AUDIT_TIMEOUT_SECONDS", 10.0))
    max_concurrency: int = field(default_factory=lambda: _int("AUDIT_MAX_CONCURRENCY", 50))
    max_download_bytes: int = field(default_factory=lambda: _int("AUDIT_MAX_DOWNLOAD_BYTES", 2_000_000))
    max_redirects: int = field(default_factory=lambda: _int("AUDIT_MAX_REDIRECTS", 5))
    user_agent: str = field(
        default_factory=lambda: os.getenv("AUDIT_USER_AGENT", "PagePulse/1.0 (+https://digitalheroesco.com)")
    )

    # Caching.
    cache_ttl_seconds: int = field(default_factory=lambda: _int("CACHE_TTL_SECONDS", 300))

    # Rate limiting.
    rate_limit_requests: int = field(default_factory=lambda: _int("RATE_LIMIT_REQUESTS", 100))
    rate_limit_window_seconds: int = field(default_factory=lambda: _int("RATE_LIMIT_WINDOW_SECONDS", 3600))

    # Batch endpoint.
    max_batch_size: int = field(default_factory=lambda: _int("MAX_BATCH_SIZE", 10))

    # Safety: refuse to audit private/loopback addresses (SSRF guard).
    allow_private_targets: bool = field(
        default_factory=lambda: os.getenv("ALLOW_PRIVATE_TARGETS", "false").lower() in {"1", "true", "yes"}
    )

    cors_origins: str = field(default_factory=lambda: os.getenv("CORS_ORIGINS", "*"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def use_mongo(self) -> bool:
        return bool(self.mongo_url)


def get_settings() -> Settings:
    """Build settings from the current environment.

    Not cached on purpose: tests mutate the environment and expect a fresh read.
    """
    return Settings()

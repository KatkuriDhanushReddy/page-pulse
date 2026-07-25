"""Shared fixtures.

Every test runs against the in-memory storage backend and a mocked HTTP
transport, so the suite needs no network and no database.
"""

from __future__ import annotations

import httpx
import pytest

from app.auditor import URLAuditor
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Force deterministic settings for every test."""
    monkeypatch.setenv("MONGO_URL", "")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "100")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("MAX_BATCH_SIZE", "10")
    # The mocked transport never leaves the process, so DNS/SSRF checks are
    # disabled here and exercised explicitly in test_auditor.py.
    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "true")
    yield


HTML_FIXTURE = """
<!doctype html>
<html>
  <head>
    <title>  Example Domain  </title>
    <meta name="description" content="An illustrative page.">
    <meta name="robots" content="index,follow">
    <link rel="canonical" href="https://example.com/">
    <meta property="og:title" content="Example OG">
    <meta property="og:description" content="OG description">
  </head>
  <body><h1>Hello</h1><h1>Second</h1></body>
</html>
"""


def make_auditor(handler, settings: Settings | None = None) -> URLAuditor:
    """Build an auditor whose HTTP client is backed by ``handler``."""
    settings = settings or Settings()
    auditor = URLAuditor(settings)
    auditor._client = httpx.AsyncClient(  # noqa: SLF001 - deliberate test seam
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(settings.request_timeout_seconds),
        follow_redirects=True,
        max_redirects=settings.max_redirects,
    )
    return auditor


@pytest.fixture
def html_response_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html=HTML_FIXTURE,
            headers={"server": "test-server", "cache-control": "max-age=60"},
        )

    return handler

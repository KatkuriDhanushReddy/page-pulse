"""Auditor behaviour: happy path, parsing, and every failure branch."""

from __future__ import annotations

import httpx
import pytest

from app.auditor import URLAuditor
from app.config import Settings

from .conftest import HTML_FIXTURE, make_auditor

pytestmark = pytest.mark.asyncio


async def test_successful_audit_reports_status_and_timing(html_response_handler):
    auditor = make_auditor(html_response_handler)
    try:
        result = await auditor.audit("http://example.com", "req-1")
    finally:
        await auditor.shutdown()

    assert result.status_code == 200
    assert result.accessible is True
    assert result.error is None
    assert result.response_time_ms >= 0
    assert result.performance.content_bytes == len(HTML_FIXTURE.encode())
    assert result.headers["server"] == "test-server"


async def test_extracts_meta_tags(html_response_handler):
    auditor = make_auditor(html_response_handler)
    try:
        result = await auditor.audit("http://example.com", "req-2")
    finally:
        await auditor.shutdown()

    assert result.meta is not None
    assert result.meta.title == "Example Domain"
    assert result.meta.description == "An illustrative page."
    assert result.meta.canonical == "https://example.com/"
    assert result.meta.og_title == "Example OG"
    assert result.meta.h1_count == 2


async def test_skips_meta_parsing_for_non_html():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    auditor = make_auditor(handler)
    try:
        result = await auditor.audit("http://example.com/api", "req-3")
    finally:
        await auditor.shutdown()

    assert result.meta is None
    assert result.accessible is True


async def test_4xx_is_reachable_but_not_accessible():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, html="<html><title>gone</title></html>")

    auditor = make_auditor(handler)
    try:
        result = await auditor.audit("http://example.com/missing", "req-4")
    finally:
        await auditor.shutdown()

    assert result.status_code == 404
    assert result.accessible is False
    assert result.error is None  # the fetch itself succeeded


async def test_timeout_returns_structured_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    auditor = make_auditor(handler)
    try:
        result = await auditor.audit("http://slow.example.com", "req-5")
    finally:
        await auditor.shutdown()

    assert result.status_code is None
    assert result.accessible is False
    assert result.error["code"] == "upstream_timeout"


async def test_connection_failure_returns_structured_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    auditor = make_auditor(handler)
    try:
        result = await auditor.audit("http://down.example.com", "req-6")
    finally:
        await auditor.shutdown()

    assert result.error["code"] == "connection_failed"
    assert result.accessible is False


async def test_counts_redirects_and_records_final_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "http://example.com/final"})
        return httpx.Response(200, html="<html><title>final</title></html>")

    auditor = make_auditor(handler)
    try:
        result = await auditor.audit("http://example.com/", "req-7")
    finally:
        await auditor.shutdown()

    assert result.performance.redirect_count == 1
    assert result.final_url.endswith("/final")


async def test_redirect_loop_is_capped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.com/loop"})

    auditor = make_auditor(handler)
    try:
        result = await auditor.audit("http://example.com/loop", "req-8")
    finally:
        await auditor.shutdown()

    assert result.error["code"] == "too_many_redirects"


async def test_large_body_is_truncated(monkeypatch):
    monkeypatch.setenv("AUDIT_MAX_DOWNLOAD_BYTES", "1024")
    settings = Settings()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><title>big</title>" + "x" * 5000 + "</html>")

    auditor = make_auditor(handler, settings)
    try:
        result = await auditor.audit("http://example.com/big", "req-9")
    finally:
        await auditor.shutdown()

    assert result.performance.truncated is True
    assert result.performance.content_bytes == 1024


async def test_private_targets_are_refused(monkeypatch, html_response_handler):
    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "false")
    settings = Settings()
    auditor = make_auditor(html_response_handler, settings)
    try:
        result = await auditor.audit("http://localhost:8000/admin", "req-10")
    finally:
        await auditor.shutdown()

    assert result.error["code"] == "target_not_allowed"


async def test_concurrency_never_exceeds_the_semaphore(monkeypatch):
    monkeypatch.setenv("AUDIT_MAX_CONCURRENCY", "3")
    settings = Settings()

    import asyncio

    peak = 0
    active = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal peak, active
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return httpx.Response(200, html="<html><title>ok</title></html>")

    auditor = make_auditor(handler, settings)
    try:
        await asyncio.gather(*(auditor.audit(f"http://example.com/{i}", f"c-{i}") for i in range(12)))
    finally:
        await auditor.shutdown()

    assert peak <= 3


async def test_audit_before_startup_raises():
    auditor = URLAuditor(Settings())
    with pytest.raises(RuntimeError):
        await auditor.audit("http://example.com", "req-11")

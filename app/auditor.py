"""The audit engine: fetch a URL, measure it, and describe what came back."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

from .config import Settings
from .models import AuditResult, MetaTags, PerformanceMetrics, SSLInfo

logger = logging.getLogger(__name__)

_CERT_DATE_FORMAT = "%b %d %H:%M:%S %Y %Z"


class TargetNotAllowed(Exception):
    """Raised when a URL resolves to an address we refuse to fetch."""


class URLAuditor:
    """Performs one audit at a time per semaphore slot.

    A single ``httpx.AsyncClient`` is reused for connection pooling; creating a
    client per request is the most common cause of socket exhaustion in
    services like this one.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        limits = httpx.Limits(
            max_connections=self._settings.max_concurrency * 2,
            max_keepalive_connections=self._settings.max_concurrency,
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.request_timeout_seconds),
            follow_redirects=True,
            max_redirects=self._settings.max_redirects,
            limits=limits,
            headers={"User-Agent": self._settings.user_agent},
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def in_flight(self) -> int:
        """Approximate number of audits currently holding a slot."""
        return self._settings.max_concurrency - self._semaphore._value  # noqa: SLF001

    # ------------------------------------------------------------------ #

    async def audit(self, url: str, request_id: str) -> AuditResult:
        if self._client is None:
            raise RuntimeError("URLAuditor.startup() was not awaited")

        started = time.perf_counter()

        try:
            await self._assert_target_allowed(url)
        except TargetNotAllowed as exc:
            return self._failure(url, request_id, "target_not_allowed", str(exc), started)

        async with self._semaphore:
            try:
                response = await self._client.get(url)
            except httpx.TimeoutException:
                return self._failure(
                    url,
                    request_id,
                    "upstream_timeout",
                    f"target did not respond within {self._settings.request_timeout_seconds}s",
                    started,
                )
            except httpx.TooManyRedirects:
                return self._failure(
                    url, request_id, "too_many_redirects", "redirect limit exceeded", started
                )
            except httpx.ConnectError as exc:
                return self._failure(url, request_id, "connection_failed", str(exc), started)
            except httpx.HTTPError as exc:
                return self._failure(url, request_id, "upstream_error", str(exc), started)

            ttfb_ms = (time.perf_counter() - started) * 1000
            body, body_bytes, truncated = self._read_body(response)
            elapsed_ms = (time.perf_counter() - started) * 1000

            meta = self._extract_meta(body) if self._looks_like_html(response) else None
            ssl_info = await self._inspect_certificate(str(response.url))

            result = AuditResult(
                request_id=request_id,
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                accessible=200 <= response.status_code < 400,
                response_time_ms=round(elapsed_ms, 2),
                ssl=ssl_info,
                meta=meta,
                performance=PerformanceMetrics(
                    total_time_ms=round(elapsed_ms, 2),
                    ttfb_ms=round(ttfb_ms, 2),
                    content_bytes=body_bytes,
                    redirect_count=len(response.history),
                    truncated=truncated,
                ),
                headers=self._safe_headers(response),
                cached=False,
            )

            logger.info(
                "audit.completed",
                extra={
                    "request_id": request_id,
                    "url": url,
                    "status_code": result.status_code,
                    "duration_ms": result.response_time_ms,
                    "redirects": len(response.history),
                },
            )
            return result

    # ------------------------------------------------------------------ #

    async def _assert_target_allowed(self, url: str) -> None:
        """Block SSRF into loopback/link-local/private ranges."""
        if self._settings.allow_private_targets:
            return

        host = urlparse(url).hostname
        if not host:
            raise TargetNotAllowed("url has no hostname")

        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise TargetNotAllowed(f"dns resolution failed for {host}: {exc.strerror or exc}") from exc

        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
            ):
                raise TargetNotAllowed(f"{host} resolves to a non-public address ({address})")

    def _read_body(self, response: httpx.Response) -> tuple[str, int, bool]:
        """Return (text, byte_length, truncated).

        Bodies are capped so a hostile target cannot exhaust our memory.
        """
        raw = response.content or b""
        truncated = len(raw) > self._settings.max_download_bytes
        if truncated:
            raw = raw[: self._settings.max_download_bytes]
        return raw.decode(response.encoding or "utf-8", errors="replace"), len(raw), truncated

    @staticmethod
    def _looks_like_html(response: httpx.Response) -> bool:
        return "html" in response.headers.get("content-type", "").lower()

    @staticmethod
    def _safe_headers(response: httpx.Response) -> dict[str, str]:
        interesting = {
            "content-type",
            "content-length",
            "server",
            "cache-control",
            "content-encoding",
            "strict-transport-security",
            "x-frame-options",
            "location",
        }
        return {k: v for k, v in response.headers.items() if k.lower() in interesting}

    @staticmethod
    def _extract_meta(html: str) -> MetaTags | None:
        try:
            tree = HTMLParser(html)
        except Exception as exc:  # pragma: no cover - selectolax is very tolerant
            logger.warning("meta.parse_failed", extra={"error": str(exc)})
            return None

        def attr(selector: str, name: str = "content") -> str | None:
            node = tree.css_first(selector)
            if node is None:
                return None
            value = node.attributes.get(name)
            return value.strip() if value else None

        title_node = tree.css_first("title")
        return MetaTags(
            title=title_node.text(strip=True) if title_node else None,
            description=attr('meta[name="description"]'),
            canonical=attr('link[rel="canonical"]', "href"),
            robots=attr('meta[name="robots"]'),
            og_title=attr('meta[property="og:title"]'),
            og_description=attr('meta[property="og:description"]'),
            h1_count=len(tree.css("h1")),
        )

    async def _inspect_certificate(self, url: str) -> SSLInfo | None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None

        host = parsed.hostname
        port = parsed.port or 443
        loop = asyncio.get_running_loop()

        def _probe() -> SSLInfo:
            context = ssl.create_default_context()
            with (
                socket.create_connection((host, port), timeout=5) as sock,
                context.wrap_socket(sock, server_hostname=host) as tls,
            ):
                cert = tls.getpeercert() or {}

            def flatten(field: str) -> str | None:
                entries = dict(item for rdn in cert.get(field, ()) for item in rdn)
                return entries.get("organizationName") or entries.get("commonName")

            not_after = cert.get("notAfter")
            expires_at, days_remaining = None, None
            if not_after:
                parsed_expiry = datetime.strptime(not_after, _CERT_DATE_FORMAT).replace(tzinfo=UTC)
                expires_at = parsed_expiry.isoformat()
                days_remaining = (parsed_expiry - datetime.now(UTC)).days

            return SSLInfo(
                valid=True,
                issuer=flatten("issuer"),
                subject=flatten("subject"),
                expires_at=expires_at,
                days_remaining=days_remaining,
            )

        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _probe), timeout=8)
        except (ssl.SSLError, ssl.CertificateError) as exc:
            return SSLInfo(valid=False, error=str(exc))
        except (TimeoutError, OSError) as exc:
            # A reachable page with an unreachable TLS probe is worth reporting,
            # but it must not fail the whole audit.
            return SSLInfo(valid=False, error=f"tls probe failed: {exc}")

    @staticmethod
    def _failure(url: str, request_id: str, code: str, message: str, started: float) -> AuditResult:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.warning(
            "audit.failed",
            extra={"request_id": request_id, "url": url, "error_code": code, "duration_ms": elapsed_ms},
        )
        return AuditResult(
            request_id=request_id,
            url=url,
            status_code=None,
            accessible=False,
            response_time_ms=elapsed_ms,
            performance=PerformanceMetrics(total_time_ms=elapsed_ms),
            error={"code": code, "message": message},
            cached=False,
        )

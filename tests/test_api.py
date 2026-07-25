"""End-to-end tests through the ASGI app, with the network stubbed out."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import AuditResult, MetaTags, PerformanceMetrics


class StubAuditor:
    """Deterministic stand-in for URLAuditor; counts real (uncached) audits."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_with: str | None = None

    async def audit(self, url: str, request_id: str) -> AuditResult:
        self.calls.append(url)
        if self.fail_with:
            return AuditResult(
                request_id=request_id,
                url=url,
                status_code=None,
                accessible=False,
                performance=PerformanceMetrics(total_time_ms=1.0),
                error={"code": self.fail_with, "message": "stubbed failure"},
            )
        return AuditResult(
            request_id=request_id,
            url=url,
            final_url=url,
            status_code=200,
            accessible=True,
            response_time_ms=12.5,
            meta=MetaTags(title="Example Domain", h1_count=1),
            performance=PerformanceMetrics(total_time_ms=12.5, content_bytes=256),
            headers={"server": "test"},
        )

    async def shutdown(self) -> None:  # pragma: no cover - lifespan calls this
        return None


@pytest.fixture
def client(monkeypatch) -> Iterator[tuple[TestClient, StubAuditor]]:
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "5")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("MAX_BATCH_SIZE", "3")

    stub = StubAuditor()
    with TestClient(main.app) as test_client:
        main.state.auditor = stub  # replace the real auditor created by lifespan
        yield test_client, stub


def test_health_reports_backend(client):
    test_client, _ = client
    response = test_client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["storage"] == "memory"
    assert response.headers["X-Request-ID"]


def test_readiness_probe(client):
    test_client, _ = client
    assert test_client.get("/api/ready").json()["status"] == "ready"


def test_audit_returns_result_with_rate_limit_headers(client):
    test_client, _ = client
    response = test_client.post("/api/audit", json={"url": "https://example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["status_code"] == 200
    assert body["cached"] is False
    assert body["meta"]["title"] == "Example Domain"
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "4"
    assert response.headers["X-Cache"] == "MISS"


def test_repeat_audit_is_served_from_cache(client):
    test_client, stub = client
    payload = {"url": "https://cached.example.com"}

    first = test_client.post("/api/audit", json=payload)
    second = test_client.post("/api/audit", json=payload)

    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert second.headers["X-Cache"] == "HIT"
    assert stub.calls == ["https://cached.example.com"]  # only fetched once


def test_failed_audits_are_not_cached(client):
    test_client, stub = client
    stub.fail_with = "upstream_timeout"
    payload = {"url": "https://broken.example.com"}

    test_client.post("/api/audit", json=payload)
    test_client.post("/api/audit", json=payload)

    assert len(stub.calls) == 2


def test_upstream_failure_still_returns_200_with_error_body(client):
    test_client, stub = client
    stub.fail_with = "connection_failed"

    response = test_client.post("/api/audit", json={"url": "https://down.example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["accessible"] is False
    assert body["error"]["code"] == "connection_failed"


def test_invalid_url_returns_structured_422(client):
    test_client, _ = client
    response = test_client.post("/api/audit", json={"url": "not-a-url"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["request_id"]
    assert "errors" in error["details"]


def test_rate_limit_returns_429_with_retry_after(client):
    test_client, _ = client
    headers = {"X-Client-ID": "greedy"}

    for _ in range(5):
        ok = test_client.post("/api/audit", json={"url": "https://example.com"}, headers=headers)
        assert ok.status_code == 200

    blocked = test_client.post("/api/audit", json={"url": "https://example.com"}, headers=headers)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limit_exceeded"
    assert blocked.headers["Retry-After"]
    assert blocked.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limits_are_isolated_per_client(client):
    test_client, _ = client
    for _ in range(5):
        test_client.post("/api/audit", json={"url": "https://example.com"}, headers={"X-Client-ID": "a"})

    blocked = test_client.post(
        "/api/audit", json={"url": "https://example.com"}, headers={"X-Client-ID": "a"}
    )
    allowed = test_client.post(
        "/api/audit", json={"url": "https://example.com"}, headers={"X-Client-ID": "b"}
    )

    assert blocked.status_code == 429
    assert allowed.status_code == 200


def test_batch_audit_returns_every_result(client):
    test_client, _ = client
    response = test_client.post(
        "/api/audit/batch",
        json={"urls": [{"url": "https://a.example.com"}, {"url": "https://b.example.com"}]},
        headers={"X-Client-ID": "batch-user"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {item["url"] for item in body["results"]} == {"https://a.example.com", "https://b.example.com"}


def test_batch_over_limit_is_rejected(client):
    test_client, _ = client
    response = test_client.post(
        "/api/audit/batch",
        json={"urls": [{"url": f"https://{i}.example.com"} for i in range(4)]},
        headers={"X-Client-ID": "batch-big"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "batch_too_large"


def test_batch_consumes_one_rate_limit_unit_per_url(client):
    test_client, _ = client
    headers = {"X-Client-ID": "batch-cost"}

    first = test_client.post(
        "/api/audit/batch",
        json={"urls": [{"url": f"https://{i}.example.com"} for i in range(3)]},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Remaining"] == "2"

    # 3 units requested, only 2 left → reject without consuming the remainder.
    second = test_client.post(
        "/api/audit/batch",
        json={"urls": [{"url": f"https://x{i}.example.com"} for i in range(3)]},
        headers=headers,
    )
    assert second.status_code == 429

    still = test_client.post(
        "/api/audit",
        json={"url": "https://still-allowed.example.com"},
        headers=headers,
    )
    assert still.status_code == 200
    assert still.headers["X-RateLimit-Remaining"] == "1"


def test_incoming_request_id_is_echoed(client):
    test_client, _ = client
    response = test_client.get("/api/health", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


def test_cache_purge_endpoint(client):
    test_client, _ = client
    assert test_client.post("/api/cache/purge").json() == {"removed": 0}


def test_root_carries_the_required_credit_line(client):
    test_client, _ = client
    assert "Digital Heroes" in test_client.get("/").json()["credit"]

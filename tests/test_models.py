"""Input validation is the first line of defence — pin its behaviour."""

import pytest
from pydantic import ValidationError

from app.models import AuditRequest, BatchAuditRequest


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?q=1",
        "https://sub.domain.example.co.uk/a/b",
    ],
)
def test_accepts_absolute_http_urls(url):
    assert AuditRequest(url=url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "example.com",  # no scheme
        "ftp://example.com",  # unsupported scheme
        "javascript:alert(1)",  # injection attempt
        "https://",  # no host
        "   ",  # blank
        "https://intranet",  # not fully qualified
    ],
)
def test_rejects_unsupported_urls(url):
    with pytest.raises(ValidationError):
        AuditRequest(url=url)


def test_strips_surrounding_whitespace():
    assert AuditRequest(url="  https://example.com  ").url == "https://example.com"


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        AuditRequest(url="https://example.com", depth=5)


def test_rejects_absurdly_long_urls():
    with pytest.raises(ValidationError):
        AuditRequest(url="https://example.com/" + "a" * 3000)


def test_batch_requires_at_least_one_url():
    with pytest.raises(ValidationError):
        BatchAuditRequest(urls=[])

"""Extraction contract/security observations, using only mocked HTTP providers."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v1 import articles
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.rate_limit import require_rate_limit
from app.factory import create_app
from app.services import article_extraction as extraction
from app.services import article_provenance as provenance
from app.services.article_fetch import UnsafeDestination
from app.services.rate_limiter import GENERAL_API_LIMIT, ARTICLE_EXTRACTION_LIMIT


class Body(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"<html>Offline article</html>"


def html_response(status=200, **headers):
    return httpx.Response(status, headers={"content-type": "text/html", **headers}, stream=Body())


async def run_extract(handler, monkeypatch, url="https://news.example/article", text="Article. " * 40):
    monkeypatch.setattr(extraction.trafilatura, "extract", lambda *a, **kw: text)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await extraction.extract_article(url, client)


@pytest.mark.parametrize("value,expected", [
    ("https://news.example/article", "https://news.example/article"),
    ("https://news.example/article?unknown-secret=SYNTHETIC#private", "https://news.example/article"),
    ("https://news.example/article?x=1&new_vendor_credential=SYNTHETIC", "https://news.example/article"),
    ("https://news.example/article#SYNTHETIC", "https://news.example/article"),
    ("https://user:SYNTHETIC@news.example/article", None),
    ("https://news.example/auth/SYNTHETIC", None),
    ("https://news.example/a;credential=SYNTHETIC", None),
    ("https://news.example/%53YNTHETIC", None),
    ("https://news.example/" + "x" * 129, None),
    ("http://127.0.0.1/private", None),
    ("http://[::1]/private", None),
    ("http://metadata.google.internal/", None),
    ("https://news.example/a\nprivate", None),
    ("javascript:alert(1)", None),
    ("not-a-url", None),
    ("https://news.google.com/rss/articles/OPAQUE?secret=SYNTHETIC", None),
])
def test_conservative_url_disclosure(value, expected):
    assert provenance.disclose_url(value) == expected
    if expected is not None:
        url = httpx.URL(expected)
        assert url.is_absolute_url and not url.userinfo and not url.query and not url.fragment


@pytest.mark.parametrize("size,disclosed", [(2047, True), (2048, True), (2049, False)])
def test_url_utf8_bound(size, disclosed):
    prefix = "https://news.example/"
    path = ("a/" * 1100)[:size - len(prefix)]
    url = prefix + path
    assert len(url.encode("utf-8")) == size
    assert (provenance.disclose_url(url) is not None) is disclosed


@pytest.mark.asyncio
async def test_no_redirects_has_exact_contract_and_observed_utc_time(monkeypatch):
    before = datetime.now(timezone.utc)
    result = await run_extract(lambda request: html_response(), monkeypatch)
    after = datetime.now(timezone.utc)
    p = result["fetch_provenance"]
    timestamp = datetime.fromisoformat(p["response_received_at"].replace("Z", "+00:00"))
    assert before <= timestamp <= after and timestamp.utcoffset().total_seconds() == 0
    assert p == {
        "schema_version": 1, "final_url": "https://news.example/article",
        "redirect_chain": [], "redirect_chain_complete": True,
        "response_received_at": p["response_received_at"],
        "text_length_chars": len(result["article_text"]),
        "text_length_utf8_bytes": len(result["article_text"].encode("utf-8")),
        "text_truncated": False, "response_host": "news.example",
        "publisher_canonical_url": None, "intended_article_resolved": None,
        "source_identity_status": "unverified", "urls_redacted": False,
    }


@pytest.mark.parametrize("count", [1, 5, 6])
@pytest.mark.asyncio
async def test_redirect_chain_exact_limit_and_no_sixth_acceptance(count, monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) <= count:
            return html_response(302, location=f"/hop-{len(requests)}?vendor_secret=SYNTHETIC")
        return html_response()
    result = await run_extract(respond, monkeypatch)
    p = result["fetch_provenance"]
    assert len(p["redirect_chain"]) == min(count, 5)
    assert len(requests) == min(count, 5) + 1
    assert p["redirect_chain_complete"] is (count <= 5)
    assert result["extraction_status"] == ("success" if count <= 5 else "request_error")
    assert p["urls_redacted"] is True
    for i, entry in enumerate(p["redirect_chain"]):
        assert entry == {"source_url": f"https://news.example/{'article' if i == 0 else f'hop-{i}'}",
                         "target_url": f"https://news.example/hop-{i + 1}", "status_code": 302}
    if count > 5:
        assert p["final_url"] is None and p["response_received_at"] is None


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.asyncio
async def test_relative_and_cross_origin_redirect_observations(status, monkeypatch):
    def respond(request):
        if request.url.host == "news.example":
            return html_response(status, location="http://other.example/final")
        return html_response()
    result = await run_extract(respond, monkeypatch)
    p = result["fetch_provenance"]
    assert p["redirect_chain"] == [{"source_url": "https://news.example/article",
        "target_url": "http://other.example/final", "status_code": status}]
    assert p["final_url"] == "http://other.example/final"
    assert p["response_host"] == "other.example"
    assert p["publisher_canonical_url"] is None and p["intended_article_resolved"] is None
    assert p["source_identity_status"] == "unverified"


@pytest.mark.parametrize("target", ["http://127.0.0.1/secret", "http://user:SYNTHETIC@news.example/", "file:///secret"])
@pytest.mark.asyncio
async def test_rejected_redirect_not_disclosed_or_accepted(target, monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return html_response(302, location=target)
    result = await run_extract(respond, monkeypatch)
    p = result["fetch_provenance"]
    assert len(requests) == 1
    assert p["redirect_chain"] == [] and not p["redirect_chain_complete"]
    assert p["final_url"] is p["response_host"] is p["response_received_at"] is None
    assert result["url"] is None and p["urls_redacted"]
    assert "SYNTHETIC" not in json.dumps(result)


@pytest.mark.parametrize("error,status,message", [
    (httpx.ConnectError("SYNTHETIC peer=10.0.0.2?credential=private"), "request_error", "Article request failed."),
    (httpx.ReadTimeout("SYNTHETIC cookies=private"), "timeout", "Request timed out."),
    (UnsafeDestination("SYNTHETIC DNS=10.0.0.2"), "request_error", "Unsafe article destination."),
    (RuntimeError("SYNTHETIC Authorization: private"), "failed", "Unexpected extraction error."),
    (extraction.ArticleDownloadError("SYNTHETIC unrecognized category"), "request_error", "Article request failed."),
])
@pytest.mark.parametrize("after_redirect", [False, True])
@pytest.mark.asyncio
async def test_failed_connection_never_invents_terminal_details(error, status, message, after_redirect, monkeypatch, caplog):
    calls = []
    def respond(request):
        calls.append(request)
        if after_redirect and len(calls) == 1:
            return html_response(302, location="https://other.example/next?private=SYNTHETIC")
        raise error
    with caplog.at_level(logging.DEBUG):
        result = await run_extract(respond, monkeypatch)
    p = result["fetch_provenance"]
    assert result["extraction_status"] == status and result["extraction_error"] == message
    assert p["final_url"] is p["response_received_at"] is p["response_host"] is None
    assert not p["redirect_chain_complete"]
    assert p["text_length_chars"] == p["text_length_utf8_bytes"] == 0
    assert "SYNTHETIC" not in json.dumps(result) + caplog.text
    assert "10.0.0.2" not in json.dumps(result) + caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("status", [403, 404, 500])
@pytest.mark.asyncio
async def test_http_error_is_terminal_response_not_success(status, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        result = await run_extract(lambda request: html_response(status, **{"set-cookie": "SYNTHETIC"}),
            monkeypatch, url="https://news.example/article?secret=SYNTHETIC")
    p = result["fetch_provenance"]
    assert result["extraction_status"] == "http_error" and result["extraction_error"] == f"HTTP {status}"
    assert p["final_url"] == result["url"] == "https://news.example/article"
    assert p["redirect_chain_complete"] and p["response_received_at"] is not None
    assert "SYNTHETIC" not in json.dumps(result) + caplog.text
    assert "?secret" not in caplog.text


@pytest.mark.parametrize("text,status,truncated", [
    (None, "no_content", False), ("short", "insufficient_content", False),
    ("a" * 20000, "success", False), ("a" * 20001, "success", True),
    ("\U0001f600" * 20001, "success", True), ("界é" * 11000, "success", True),
    ("  " + "a" * 20000 + " \n", "success", False),
])
@pytest.mark.asyncio
async def test_returned_text_lengths_and_core_truncation(text, status, truncated, monkeypatch):
    result = await run_extract(lambda request: html_response(), monkeypatch, text=text)
    p = result["fetch_provenance"]
    assert result["extraction_status"] == status
    returned = result["article_text"] or ""
    assert p["text_length_chars"] == len(returned) <= 20000
    assert p["text_length_utf8_bytes"] == len(returned.encode("utf-8")) <= 80000
    assert p["text_truncated"] is truncated


@pytest.mark.parametrize("terminal_wrapper", [True, False])
@pytest.mark.asyncio
async def test_google_news_wrappers_are_never_publisher_verification(terminal_wrapper, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        if not terminal_wrapper and len(calls) == 1:
            return html_response(302, location="https://publisher.example/article")
        return html_response()
    result = await run_extract(respond, monkeypatch, url="https://news.google.com/rss/articles/OPAQUE")
    p = result["fetch_provenance"]
    assert result["url"] is None
    assert p["publisher_canonical_url"] is None
    assert p["intended_article_resolved"] is (False if terminal_wrapper else None)
    assert p["source_identity_status"] == ("google_news_wrapper_unresolved" if terminal_wrapper else "unverified")
    assert p["final_url"] == (None if terminal_wrapper else "https://publisher.example/article")


@pytest.mark.asyncio
async def test_unreachable_google_wrapper_remains_unresolved(monkeypatch):
    def fail(request):
        raise httpx.ConnectError("private")
    result = await run_extract(fail, monkeypatch, url="https://news.google.com/rss/articles/OPAQUE")
    p = result["fetch_provenance"]
    assert p["source_identity_status"] == "google_news_wrapper_unresolved"
    assert p["intended_article_resolved"] is False and p["final_url"] is None


@pytest.mark.asyncio
async def test_terminal_headers_observed_even_when_body_is_rejected(monkeypatch):
    result = await run_extract(lambda request: html_response(**{"content-length": "99999999"}), monkeypatch)
    p = result["fetch_provenance"]
    assert result["extraction_status"] == "request_error"
    assert p["redirect_chain_complete"] and p["response_received_at"] is not None
    assert p["text_length_chars"] == 0 and p["text_truncated"] is False


@pytest.mark.asyncio
async def test_read_timeout_after_headers_retains_only_observed_terminal_details(monkeypatch):
    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadTimeout("SYNTHETIC private read details")
    result = await run_extract(lambda request: httpx.Response(200,
        headers={"content-type": "text/html"}, stream=InterruptedBody()), monkeypatch)
    p = result["fetch_provenance"]
    assert result["extraction_status"] == "timeout" and result["article_text"] is None
    assert p["redirect_chain_complete"] and p["final_url"] == "https://news.example/article"
    assert p["response_received_at"] is not None and p["text_length_chars"] == 0


@pytest.mark.asyncio
async def test_redirect_without_location_does_not_invent_terminal_provenance(monkeypatch):
    result = await run_extract(lambda request: html_response(302), monkeypatch)
    p = result["fetch_provenance"]
    assert result["extraction_status"] == "http_error"
    assert not p["redirect_chain_complete"] and p["final_url"] is p["response_received_at"] is None


@pytest.mark.asyncio
async def test_unsupported_content_does_not_echo_untrusted_header(monkeypatch, caplog):
    result = await run_extract(lambda request: httpx.Response(200,
        headers={"content-type": "application/SYNTHETIC"}), monkeypatch)
    assert result["extraction_status"] == "unsupported_content"
    assert result["extraction_error"] == "Unsupported content type."
    assert "SYNTHETIC" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_parser_failure_and_logs_are_sanitized_in_worker_thread(monkeypatch, caplog):
    def parse(*args, **kwargs):
        logging.getLogger("trafilatura.core").warning("SYNTHETIC private source")
        raise RuntimeError("SYNTHETIC private exception")
    monkeypatch.setattr(extraction.trafilatura, "extract", parse)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: html_response())) as client:
        with caplog.at_level(logging.DEBUG):
            result = await extraction.extract_article("https://news.example/article?private=SYNTHETIC", client)
    assert result["extraction_status"] == "failed"
    assert result["extraction_error"] == "Unexpected extraction error."
    assert result["fetch_provenance"]["redirect_chain_complete"]
    assert "SYNTHETIC" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_cancellation_restores_dependency_logging(monkeypatch, caplog):
    entered = asyncio.Event()
    async def respond(request):
        entered.set()
        await asyncio.Event().wait()
    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(run_extract(respond, monkeypatch))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        logging.getLogger("httpx").info("Logging remains available after cancellation")
    assert "Logging remains available after cancellation" in caplog.text


@pytest.mark.asyncio
async def test_maximum_url_chain_fits_whole_provenance_budget(monkeypatch):
    prefix = "https://news.example/"
    path = ("a/" * 1100)[:2048 - len(prefix)]
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        return html_response(302, location=prefix + path) if count <= 5 else html_response()
    result = await run_extract(respond, monkeypatch, url=prefix + path)
    p = result["fetch_provenance"]
    assert len(p["redirect_chain"]) == 5
    assert len(json.dumps(p).encode("utf-8")) <= 32768
    assert len(p["final_url"].encode("utf-8")) == 2048


@pytest.mark.asyncio
async def test_oversized_optional_provenance_is_omitted_without_changing_legacy_fields(monkeypatch):
    monkeypatch.setattr(provenance, "MAX_PROVENANCE_BYTES", 1)
    result = await run_extract(lambda request: html_response(), monkeypatch)
    assert "fetch_provenance" not in result
    assert result["extraction_status"] == "success" and result["url"] == "https://news.example/article"


@pytest.mark.asyncio
async def test_library_log_suppression_is_context_local_and_resets(monkeypatch, caplog):
    entered, release = asyncio.Event(), asyncio.Event()
    async def respond(request):
        entered.set()
        await release.wait()
        logging.getLogger("httpcore.http11").debug("Set-Cookie: SYNTHETIC")
        return html_response()
    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(run_extract(respond, monkeypatch,
            url="https://news.example/article?private=SYNTHETIC"))
        await entered.wait()
        logging.getLogger("httpx").info("Unrelated request remains visible")
        release.set()
        await task
        logging.getLogger("httpx").info("After extraction remains visible")
    assert "SYNTHETIC" not in caplog.text
    assert "Unrelated request remains visible" in caplog.text
    assert "After extraction remains visible" in caplog.text


@pytest.fixture
def endpoint(monkeypatch):
    app = create_app()
    app.include_router(articles.router, prefix="/api/v1")
    auth = AuthenticatedApplication(application=SimpleNamespace(id="test-app"),
        api_key=SimpleNamespace(id="test-key", scopes=["articles:extract"]))
    app.dependency_overrides[get_current_application] = lambda: auth
    for bucket, limit in [("general", GENERAL_API_LIMIT), ("article_extraction", ARTICLE_EXTRACTION_LIMIT)]:
        app.dependency_overrides[require_rate_limit(bucket=bucket, rate_limit=limit)] = lambda: auth
    return app, auth


@pytest.mark.parametrize("url,status", [
    ("https://news.example/article?private=SYNTHETIC", "success"),
    ("https://user:SYNTHETIC@news.example/article", "request_error"),
    ("https://news.example/%53YNTHETIC", "success"),
])
def test_actual_endpoint_returns_safe_url_or_null_and_preserves_contract(endpoint, url, status, monkeypatch):
    app, _ = endpoint
    client_class = httpx.AsyncClient
    monkeypatch.setattr(extraction.httpx, "AsyncClient", lambda **kwargs:
        client_class(transport=httpx.MockTransport(lambda request: html_response())))
    monkeypatch.setattr(extraction.trafilatura, "extract", lambda *a, **kw: "Article. " * 40)
    with TestClient(app) as client:
        response = client.post("/api/v1/articles/extract", json={"url": url})
    assert response.status_code == 200
    result = response.json()
    assert {"url", "article_text", "extraction_status", "extraction_error"} <= result.keys()
    assert "fetch_provenance" in result and "SYNTHETIC" not in json.dumps(result)
    assert result["extraction_status"] == status
    if result["url"] is not None:
        assert httpx.URL(result["url"]).is_absolute_url
    if status == "success":
        assert result["article_text"]


@pytest.mark.parametrize("mode,expected", [("missing_auth", 401), ("missing_scope", 403)])
def test_endpoint_auth_and_scope_are_enforced_before_fetch(endpoint, mode, expected):
    app, auth = endpoint
    if mode == "missing_auth":
        app.dependency_overrides.pop(get_current_application)
    else:
        auth.api_key.scopes = ["sources:read"]
    with patch.object(articles, "extract_articles", new_callable=AsyncMock) as fetch:
        with TestClient(app) as client:
            response = client.post("/api/v1/articles/extract", json={"url": "https://news.example/article"})
    assert response.status_code == expected
    fetch.assert_not_called()


@pytest.mark.parametrize("result,exception,message", [
    ([], None, "Article extraction returned no results."),
    (None, RuntimeError("SYNTHETIC https://user:private@news.example/?secret=private"),
        "Article extraction service unavailable."),
])
def test_endpoint_http_500_behavior_and_safe_logging(endpoint, result, exception, message, caplog):
    app, _ = endpoint
    with patch.object(articles, "extract_articles", new_callable=AsyncMock,
        return_value=result, side_effect=exception):
        with TestClient(app) as client:
            response = client.post("/api/v1/articles/extract", json={"url": "https://news.example/article"})
    assert response.status_code == 500 and response.json() == {"detail": message}
    assert "SYNTHETIC" not in response.text + caplog.text
    assert all(record.exc_info is None for record in caplog.records)

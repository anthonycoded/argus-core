"""Exact coverage byte accounting without full-body validation allocations."""

import gc
import json
import tracemalloc
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import httpcore
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import JSONResponse

from app.clients import classification_contracts as module
from app.clients.argus_core_client import CoreClientError, InferenceResult, get_core_discovery_client, get_core_inference_client
from app.clients.classification_contracts import ClassificationCoverage, CoverageProvenance, CoverageSource, UnclassifiedCandidate, _json_utf8_size
from app.database import Base, get_db
from app.global_chess_factory import create_app
from app.models import ResearchSource, Topic
from app.routes.research import router
from app.services import research_service


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Size tests must not contact live services")))


@pytest.fixture(scope="module")
def maximal_row():
    # HttpUrl's 2083-byte input bound is tighter than allowing four bytes for
    # every one of the URL field's 2048 codepoints. Quotes add JSON escaping.
    url = "http://\U00010400/" + "\U0001f600" * 10 + "\u4e00" + '"' * 2028
    source = CoverageSource(
        title="\x00" * 1000, url=url, publisher="\x00" * 255,
        published_at=datetime.max.replace(tzinfo=timezone.utc), snippet="\x00" * 65536,
        source_type="google_news", provenance=CoverageProvenance(
            provider="google_news", canonical_url=url, original_url="\x00" * 2048,
            feed_id="npr-politics", snapshot_id="f" * 32, entry_index=499,
        ),
    )
    return UnclassifiedCandidate(status="not_attempted", reason="authentication_failed", source=source)


def make_coverage(row, count=1):
    return ClassificationCoverage(
        complete=False, attempted=150, classified=150, not_attempted=count,
        stop_reason="authentication_failed", retry_after="Fri, 31 Dec 9999 23:59:59 GMT",
        unclassified=[row] * count,
    )


def test_maximum_legal_coverage_and_serializer_overhead(maximal_row, monkeypatch):
    assert module.MAX_OBSERVATIONS == 225 and module.MAX_COVERAGE_BYTES == 96 * 1024 * 1024
    assert len(maximal_row.source.url) == 2048 and len(maximal_row.source.url.encode("utf-8")) == 2083
    row_size = len(JSONResponse(maximal_row.model_dump(mode="json")).body)
    one = make_coverage(maximal_row)
    one_size = len(JSONResponse(one.model_dump(mode="json")).body)
    assert row_size == 421616 and one_size == 421798
    # The actual API serializer supplies the record and wrapper sizes. Adding
    # 224 records adds 224 commas; changing 1 to 225 adds two counter digits.
    # No giant response allocation is needed to establish this exact maximum.
    expected = one_size + 224 * (row_size + 1) + 2
    monkeypatch.setattr(ClassificationCoverage, "model_dump_json", Mock(
        side_effect=AssertionError("Validation attempted whole-body JSON serialization")))
    coverage = make_coverage(maximal_row, 225)
    assert _json_utf8_size(coverage.model_dump(mode="json")) == expected == 94864008
    assert all(row.source.snippet == maximal_row.source.snippet for row in coverage.unclassified)
    assert expected < module.MAX_COVERAGE_BYTES


@pytest.mark.parametrize("text", [
    "", "plain ASCII", 'quotes " and slash \\ and /', "".join(chr(n) for n in range(32)),
    "\x7f\x80\u00a0", "Greek Ελληνικά and 中文", "\u2028\u2029", "\U0001f600\U0010ffff",
    "a\x00\U0001f600\n\"\\" * 100, "\x00" * 65536, "\U0001f600" * 65536,
])
def test_counter_matches_previous_and_http_serializers(text):
    row = UnclassifiedCandidate(status="not_attempted", reason="prompt_too_large",
                                source=CoverageSource(title="Title", url="https://example.com/", snippet=text))
    coverage = make_coverage(row)
    expected = len(coverage.model_dump_json().encode("utf-8"))
    assert _json_utf8_size(coverage.model_dump(mode="json")) == expected
    assert len(JSONResponse(coverage.model_dump(mode="json")).body) == expected


@pytest.mark.parametrize("offset,accepted", [(-1, False), (0, True), (1, True)])
def test_exact_inclusive_byte_boundary(monkeypatch, maximal_row, offset, accepted):
    expected = len(JSONResponse(make_coverage(maximal_row).model_dump(mode="json")).body)
    monkeypatch.setattr(module, "MAX_COVERAGE_BYTES", expected + offset)
    if accepted:
        assert make_coverage(maximal_row).not_attempted == 1
    else:
        with pytest.raises(ValidationError, match="Classification coverage exceeds its response bound"):
            make_coverage(maximal_row)


@pytest.mark.parametrize("field", ["title", "publisher", "snippet", "url"])
def test_existing_source_field_limits(maximal_row, field):
    with pytest.raises(ValidationError):
        CoverageSource(**{**maximal_row.source.model_dump(), field: getattr(maximal_row.source, field) + "x"})


@pytest.mark.parametrize("field", ["canonical_url", "original_url", "snapshot_id", "entry_index"])
def test_existing_provenance_limits(maximal_row, field):
    value = 500 if field == "entry_index" else getattr(maximal_row.source.provenance, field) + "x"
    with pytest.raises(ValidationError):
        CoverageProvenance(**{**maximal_row.source.provenance.model_dump(), field: value})


def test_existing_cardinality_and_invalid_unicode_limits(maximal_row):
    with pytest.raises(ValidationError):make_coverage(maximal_row, 226)
    for invalid in ("\ud800", "\udfff"):
        with pytest.raises(ValidationError):
            CoverageSource(title="Title", url="https://example.com/", snippet=invalid)


def _profile(operation):
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:tracemalloc.start()
    try:
        gc.collect()
        initial = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        result = operation()
        return result, tracemalloc.get_traced_memory()[1] - initial
    finally:
        if not already_tracing:tracemalloc.stop()


def test_counting_allocations_do_not_scale_as_a_whole_json_body(maximal_row):
    # Eight maximal records provide a clear comparison without deliberately
    # allocating the old validator's ~724-MiB maximum-object working set in CI.
    coverage = make_coverage(maximal_row, 8)
    old_size, old_peak = _profile(lambda: len(coverage.model_dump_json().encode("utf-8")))
    new_size, new_peak = _profile(lambda: _json_utf8_size(coverage.model_dump(mode="json")))
    assert new_size == old_size
    assert new_peak < 8 * 1024 * 1024
    assert new_peak * 3 < old_peak
    maximum_size, maximum_peak = _profile(lambda: _json_utf8_size(make_coverage(maximal_row, 225).model_dump(mode="json")))
    assert maximum_size == 94864008 and maximum_peak < 8 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "partial", "error"])
async def test_actual_success_partial_error_response_compatibility(monkeypatch, mode):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(Topic(id=1, title="Topic", url="https://example.com/topic", source="Fixture", status="approved",
                     summary="Preserved", relevance_score=10, freshness_score=20, score=13))
        db.commit()
    description = 'Original \x00 description \U0001f600 with "quotes" and \\ slash'
    sources = [dict(title=f"Article {i}", url=f"https://example.com/{i}", publisher="Example",
                    published_at=None, snippet=description, source_type="rss") for i in range(3)]
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=sources))
    monkeypatch.setattr(research_service, "discover_google_news_sources", AsyncMock(return_value=[]))
    valid = InferenceResult(json.dumps({"classification": "relevant", "rationale": "Supplied information"}), "actual-model")
    failure = CoreClientError(429, "AI inference rate limit exceeded.", "30", reason="rate_limited")
    inference = AsyncMock()
    if mode == "success":inference.generate_structured.return_value = valid
    else:inference.generate_structured.side_effect = [valid, failure] if mode == "partial" else [failure]
    app = create_app(routers=(router,))
    def session():
        with sessions() as db:yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_core_inference_client] = lambda: inference
    app.dependency_overrides[get_core_discovery_client] = lambda: AsyncMock()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as http:
            response = await http.post("/topics/1/research")
        assert response.status_code == (429 if mode == "error" else 200)
        assert response.headers["content-type"] == "application/json"
        assert int(response.headers["content-length"]) == len(response.content)
        data = response.json()
        assert response.content == JSONResponse(data).body
        coverage = data["classification_coverage"]
        # Extract the coverage value from the actual HTTP body, including all
        # escaping. raw_decode handles embedded quotes, braces and Unicode.
        marker = '"classification_coverage":'
        start = response.text.index(marker) + len(marker)
        _, end = json.JSONDecoder().raw_decode(response.text[start:])
        assert _json_utf8_size(coverage) == len(response.text[start:start + end].encode("utf-8"))
        if mode == "success":assert coverage["complete"] and data["inserted"] == 3
        else:
            assert response.headers["Retry-After"] == "30" and not coverage["complete"]
            assert all(item["source"]["snippet"] == description for item in coverage["unclassified"])
            assert coverage["failed"] == 1 and coverage["not_attempted"] == (1 if mode == "partial" else 2)
        if mode == "partial":assert response.headers["X-Research-Partial"] == "true" and data["inserted"] == 1
        if mode == "error":assert data["detail"] == failure.detail
        with sessions() as db:
            assert len(list(db.scalars(select(ResearchSource)))) == {"success": 3, "partial": 1, "error": 0}[mode]
    finally:engine.dispose()

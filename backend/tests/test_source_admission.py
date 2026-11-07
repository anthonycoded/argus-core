"""Offline canonicalization, command validation and integration-boundary tests."""

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core import source_admission as module
from app.core.source_admission import (
    AdmissionCommand, AdmissionError, SERIALIZATION_VERSION, TrustedAdmissionExecution,
    canonical_facts, facts_digest, stored_facts,
)
from app.core.source_admission_repository import advisory_key
from app.core.source_catalog import PublicSourceFacts, URL_VERSION

REGISTRY = UUID("4f13392a-13a4-4ab9-b3a0-62a5fca20001")  # Synthetic test namespace only.


def facts(**changes):
    values = dict(registry_id=REGISTRY, source_id="source-a", canonical_url="https://example.com/article",
        domain="example.com", title="Public title", publisher=None, published_at=None,
        source_type="news", content_hash=None, content_hash_version=None)
    return PublicSourceFacts(**(values | changes))


def approval(**changes):
    snapshot = changes.pop("approved_facts", facts())
    return AdmissionCommand(**(dict(operation_id=uuid4(), source_id=snapshot.source_id,
        expected_version=0, action="approve", reason_code="public_facts_reviewed",
        normalization_version=URL_VERSION, serialization_version=SERIALIZATION_VERSION,
        fingerprint_version=snapshot.content_hash_version, approved_facts=snapshot,
        facts_digest=facts_digest(snapshot)) | changes))


def test_canonical_spec_and_digest_are_explicit():
    raw = canonical_facts(facts())
    assert raw == json.dumps(facts().model_dump(mode="json"), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert b'"publisher":null' in raw and b'"published_at":null' in raw
    assert facts_digest(facts()) == hashlib.sha256(b"approved-facts-json-v1\n" + raw).hexdigest()


def test_equivalent_json_and_utc_dates_have_identical_digests():
    instant = datetime(2026, 1, 1, 12, 0, 0, 120, tzinfo=timezone.utc)
    a = facts(published_at=instant)
    wire = a.model_dump(mode="json")
    wire["published_at"] = instant.astimezone(timezone(timedelta(hours=5))).isoformat()
    wire["registry_id"] = str(REGISTRY).upper()
    b = stored_facts(dict(reversed(list(wire.items()))))
    assert canonical_facts(a) == canonical_facts(b)
    assert b'2026-01-01T12:00:00.000120Z' in canonical_facts(a)


@pytest.mark.parametrize("change", [dict(title="Different"), dict(publisher="Publisher"),
    dict(source_type="rss"), dict(content_hash="a"*64), dict(canonical_url="https://example.com/other"),
    dict(title="caf\u00e9"), dict(title="cafe\u0301")])
def test_material_differences_change_digest(change):
    assert facts_digest(facts(**change)) != facts_digest(facts())


def test_unicode_is_not_silently_normalized():
    assert facts_digest(facts(title="caf\u00e9")) != facts_digest(facts(title="cafe\u0301"))


@pytest.mark.parametrize("field", ["metadata", "snippets", "notes", "credentials", "research_query", "topic_id", "provenance"])
def test_private_or_unknown_snapshot_fields_are_rejected(field):
    wire = facts().model_dump(mode="json") | {field: "PRIVATE VALUE"}
    with pytest.raises(AdmissionError, match="Invalid approved facts"):
        stored_facts(wire)


def test_oversized_invalid_and_bypassed_snapshots_are_rejected(monkeypatch):
    with pytest.raises(AdmissionError): stored_facts({"title": "x"*40000})
    with pytest.raises(AdmissionError): canonical_facts(facts().model_copy(update={"title": " "}))
    with pytest.raises(AdmissionError): canonical_facts(facts(registry_id=UUID(int=0)))
    monkeypatch.setattr(module, "MAX_FACTS_BYTES", len(canonical_facts(facts())) - 1)
    with pytest.raises(AdmissionError): canonical_facts(facts())


@pytest.mark.parametrize("change", [dict(policy_version="future"), dict(normalization_version="future"),
    dict(serialization_version="future"), dict(fingerprint_version="future"), dict(facts_digest="0"*64),
    dict(source_id="other"), dict(reason_code="public_access_revoked"), dict(expected_version=True),
    dict(expected_version=-1), dict(review_reference="https://secret.example/token"), dict(metadata={"public":True})])
def test_invalid_approval_commands_fail_closed(change):
    with pytest.raises(ValidationError): approval(**change)


@pytest.mark.parametrize("action,reason", [("reject","public_disclosure_rejected"),("revoke","public_access_revoked")])
def test_nonapproval_never_carries_snapshot(action, reason):
    value = AdmissionCommand(operation_id=uuid4(), source_id="source-a", expected_version=1,
        action=action, reason_code=reason)
    assert value.approved_facts is None
    with pytest.raises(ValidationError):
        AdmissionCommand(**(approval().model_dump() | dict(action=action, reason_code=reason)))


def test_hash_provenance_is_unknown_unless_explicitly_attested():
    value = approval(approved_facts=facts(content_hash="a"*64))
    assert value.fingerprint_version is None and value.approved_facts.content_hash_version is None
    with pytest.raises(ValidationError): value.expected_version = 2
    with pytest.raises(ValidationError): value.approved_facts.title = "Changed"


def test_trusted_context_is_typed_but_does_not_issue_authority():
    value = TrustedAdmissionExecution(registry_id=REGISTRY, actor_issuer="offline-tests", actor_subject="synthetic-reviewer")
    assert value.actor_subject == "synthetic-reviewer"
    with pytest.raises(ValidationError):
        TrustedAdmissionExecution(registry_id=REGISTRY, actor_issuer="caller", actor_subject="", api_key="private")


def test_advisory_keys_are_stable_domain_separated_and_registry_bound():
    key = advisory_key("stream", REGISTRY, "source-a")
    assert key == 8429063958697608671  # Frozen algorithm vector, not Python hash().
    assert key != advisory_key("operation", REGISTRY, "source-a")
    assert key != advisory_key("stream", UUID(int=2), "source-a")
    assert key != advisory_key("stream", REGISTRY, "source-b")


def test_no_runtime_authority_or_route_integration():
    root = Path(__file__).parents[1] / "app"
    allowed = {"core/source_admission.py", "core/source_admission_repository.py", "core/models/source_admission.py"}
    for path in root.rglob("*.py"):
        if path.relative_to(root).as_posix() in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "app.core.source_admission_repository", str(path)
                assert not (node.module == "app.core.source_admission" and
                    any(item.name == "TrustedAdmissionExecution" for item in node.names)), str(path)


@pytest.mark.asyncio
async def test_legacy_source_api_cannot_create_admission(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.api.v1.router import api_router
    from app.core.auth import get_current_application
    from app.core.database import get_db
    from app.core.models import CoreBase, SourceCatalogDB, SourceAdmissionEventDB
    from app.core import rate_limit
    from app.factory import create_app
    from app.services.rate_limiter import RateLimitDecision

    engine=create_engine("sqlite://",poolclass=StaticPool,connect_args={"check_same_thread":False})
    CoreBase.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine)
    app=create_app(routers=(api_router,))
    def session():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=session
    app.dependency_overrides[get_current_application]=lambda:SimpleNamespace(
        scopes=["sources:read","sources:write"],application=SimpleNamespace(id="synthetic-app"),
        api_key=SimpleNamespace(id="synthetic-key"))
    monkeypatch.setattr(rate_limit,"enforce_rate_limit",AsyncMock(return_value=RateLimitDecision(True,120,119,0)))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://offline.test") as client:
            response=await client.post("/api/v1/sources/register",json=dict(title="Public title",
                url="https://example.com/article",metadata={"admission":"public","actor_subject":"caller"}))
            assert response.status_code==200 and response.json()["status"]=="created"
            registered=await client.get("/api/v1/sources/"+response.json()["source_id"])
            assert registered.status_code==200
            assert registered.json()["metadata"]=={"admission":"public","actor_subject":"caller"}
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(SourceCatalogDB))==0
            assert db.scalar(select(func.count()).select_from(SourceAdmissionEventDB))==0
    finally:engine.dispose()

"""Phase 5D.2A: deterministic policy tests, no databases, HTTP or providers."""

import copy
import socket
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import product
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.source_catalog import (
    HASH_VERSION, URL_VERSION, AdmissionState, CatalogContext, CatalogEntry, CatalogInputError,
    ContentFingerprint, IdentityDecision, IdentityQuery, PublicSourceFacts, TrustedAdmission,
    decide_identity, fingerprint_full_content, project_public_source,
)
from app.utils.deduplication import canonical_source_url, content_hash

REGISTRY = UUID("12345678-1234-4234-8234-123456789abc")
OTHER_REGISTRY = UUID("22345678-1234-4234-8234-123456789abc")
A = "https://example.com/a"
B = "https://another.example/b"
FINGERPRINT = fingerprint_full_content("Full article observation, not a snippet.")
DIFFERENT = fingerprint_full_content("Different full article observation.")


@pytest.fixture(autouse=True)
def no_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Policy tests must not access persistent or network resources")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    from sqlalchemy.engine import Engine
    monkeypatch.setattr(Engine, "connect", forbidden)


def context(app="global-chess", allowed=True, registry=REGISTRY):
    return CatalogContext(registry, app, allowed)


def entry(identifier="source-a", url=A, digest=FINGERPRINT.value, *, state=AdmissionState.PUBLIC,
          version=HASH_VERSION, registry=REGISTRY, **changes):
    record = dict(id=identifier, canonical_url=url, domain=url.split("/")[2], title="Observed public title",
                  publisher="Recorded publisher", published_at=datetime(2026,11,7,tzinfo=timezone.utc),
                  source_type="rss", content_hash=digest, url="https://original.example/unused",
                  metadata={"notes":"PRIVATE NOTES", "api_key":"PRIVATE KEY", "visibility":"public"})
    record.update(changes)
    return CatalogEntry(record, TrustedAdmission(registry, identifier, state, version if digest is not None else None))


def query(fingerprint=FINGERPRINT):
    return IdentityQuery(canonical_url=A, observed_url=A+"/?utm_source=rss#original",
                         observed_publisher="Application observed publisher", fingerprint=fingerprint)


@pytest.mark.parametrize("case,outcome,verification,conflict,review", [
    ("none", "not_found", "not_found", None, False),
    ("none_no_hash", "not_found", "not_supplied", None, False),
    ("url_only", "url_matched", "not_supplied", None, False),
    ("url_hash", "url_matched", "matched", None, False),
    ("same_record", "url_matched", "matched", None, False),
    ("missing_hash", "url_matched", "missing", None, True),
    ("unknown_version", "url_matched", "version_unknown", None, True),
    ("hash_disagreement", "conflict", "disagrees", "url_content_disagreement", True),
    ("url_a_hash_b", "conflict", "disagrees", "url_hash_identity_disagreement", True),
    ("url_missing_hash_b", "conflict", "disagrees", "url_hash_identity_disagreement", True),
    ("related_content", "related_content", "matched", None, True),
])
def test_complete_identity_matrix(case, outcome, verification, conflict, review):
    url = hashed = None
    request = query(None) if case in {"none_no_hash","url_only"} else query()
    if case not in {"none","none_no_hash","related_content"}: url = entry()
    if case == "same_record": hashed = entry()
    elif case == "missing_hash": url = entry(digest=None)
    elif case == "unknown_version": url = entry(version=None)
    elif case == "hash_disagreement": url = entry(digest=DIFFERENT.value)
    elif case == "url_a_hash_b": url = entry(digest=DIFFERENT.value); hashed = entry("source-b", B)
    elif case == "url_missing_hash_b": url = entry(digest=None); hashed = entry("source-b", B)
    elif case == "related_content": hashed = entry("source-b", B)
    decision = decide_identity(request, context(), url_match=url, content_match=hashed)
    assert (decision.outcome, decision.content_verification, decision.conflict_code, decision.requires_review) == (
        outcome, verification, conflict, review)
    assert decision.source is not None if outcome == "url_matched" else decision.source is None
    assert decision.query.observed_url.endswith("/?utm_source=rss#original")
    assert decision.query.observed_publisher == "Application observed publisher"
    if decision.source is not None: assert decision.source.publisher == "Recorded publisher"
    assert IdentityDecision.model_validate_json(decision.model_dump_json()) == decision


@pytest.mark.parametrize("app,state,allowed", list(product(
    ["argus","global-chess"], list(AdmissionState), [True,False])))
def test_admission_and_application_visibility_matrix(app, state, allowed):
    facts = project_public_source(entry(state=state), context(app, allowed))
    assert (facts is not None) == (allowed and state is AdmissionState.PUBLIC)


def test_approved_catalog_is_shared_without_application_editorial_data():
    source = entry()
    argus = decide_identity(query(), context("argus"), url_match=source)
    chess = decide_identity(query(), context("global-chess"), url_match=source)
    assert argus.model_dump_json() == chess.model_dump_json()
    assert "application_id" not in argus.model_dump_json()


@pytest.mark.parametrize("state", [AdmissionState.PRIVATE,AdmissionState.UNREVIEWED])
@pytest.mark.parametrize("slot", ["url", "content", "both"])
def test_hidden_matches_are_byte_identical_to_absent_matches(state, slot):
    hidden_url = entry(identifier="HIDDEN-SOURCE-ID", digest=DIFFERENT.value, state=state)
    hidden_content = entry("HIDDEN-CONTENT-ID", B, state=state)
    args = dict(url_match=hidden_url if slot in {"url","both"} else None,
                content_match=hidden_content if slot in {"content","both"} else None)
    result = decide_identity(query(), context(), **args)
    assert result.model_dump_json() == decide_identity(query(), context()).model_dump_json()
    assert "HIDDEN" not in repr(result) and DIFFERENT.value not in result.model_dump_json()
    assert B not in result.model_dump_json() and result.conflict_code is None


@pytest.mark.parametrize("state", [AdmissionState.PRIVATE,AdmissionState.UNREVIEWED])
def test_hidden_hash_conflict_does_not_change_visible_url_decision(state):
    visible = entry(digest=None)
    baseline = decide_identity(query(), context(), url_match=visible)
    result = decide_identity(query(), context(), url_match=visible, content_match=entry("hidden",B,state=state))
    assert result.model_dump_json() == baseline.model_dump_json()


@pytest.mark.parametrize("visible_kind", ["absent","matching","missing","different","unversioned"])
@pytest.mark.parametrize("hidden_kind", ["private","unreviewed","another_registry"])
@pytest.mark.parametrize("app", ["argus","global-chess"])
def test_hidden_hash_noninterference_across_all_visible_url_states(visible_kind, hidden_kind, app):
    url = None if visible_kind == "absent" else entry(
        digest=None if visible_kind == "missing" else DIFFERENT.value if visible_kind == "different" else FINGERPRINT.value,
        version=None if visible_kind == "unversioned" else HASH_VERSION)
    hidden = entry("HIDDEN",B,state=AdmissionState.PRIVATE if hidden_kind == "private" else
        AdmissionState.UNREVIEWED if hidden_kind == "unreviewed" else AdmissionState.PUBLIC,
        registry=OTHER_REGISTRY if hidden_kind == "another_registry" else REGISTRY)
    baseline = decide_identity(query(),context(app),url_match=url)
    decision = decide_identity(query(),context(app),url_match=url,content_match=hidden)
    assert decision.model_dump_json() == baseline.model_dump_json()


def test_hidden_url_does_not_turn_public_related_content_into_a_conflict():
    visible_hash = entry("source-b",B)
    baseline = decide_identity(query(),context(),content_match=visible_hash)
    result = decide_identity(query(),context(),url_match=entry(state=AdmissionState.PRIVATE),content_match=visible_hash)
    assert result.model_dump_json() == baseline.model_dump_json() and result.outcome == "related_content"


class UnreadableHiddenRecord(Mapping):
    def __getitem__(self, key): raise AssertionError("Hidden facts were accessed")
    def __iter__(self): raise AssertionError("Hidden facts were accessed")
    def __len__(self): raise AssertionError("Hidden facts were accessed")


@pytest.mark.parametrize("state,allowed,registry", [
    (AdmissionState.PRIVATE,True,REGISTRY), (AdmissionState.UNREVIEWED,True,REGISTRY),
    (AdmissionState.PUBLIC,False,REGISTRY), (AdmissionState.PUBLIC,True,OTHER_REGISTRY),
])
def test_visibility_precedes_even_reading_malformed_hidden_facts(state, allowed, registry):
    hidden = CatalogEntry(UnreadableHiddenRecord(),TrustedAdmission(registry,"hidden",state,HASH_VERSION))
    assert project_public_source(hidden,context(allowed=allowed)) is None
    assert decide_identity(query(),context(allowed=allowed),url_match=hidden,content_match=hidden).outcome == "not_found"


def test_admission_is_bound_to_source_and_registry_not_metadata():
    record = entry().record
    forged = CatalogEntry(record,TrustedAdmission(REGISTRY,"another-source",AdmissionState.PUBLIC,HASH_VERSION))
    assert project_public_source(forged,context()) is None
    unreviewed = CatalogEntry(record,TrustedAdmission(REGISTRY,"source-a"))
    assert project_public_source(unreviewed,context()) is None
    assert project_public_source(entry(registry=OTHER_REGISTRY),context()) is None


class MetadataTrap(dict):
    def __getitem__(self, key):
        if key in {"metadata","metadata_json","url","created_at","notes","relevance"}:
            raise AssertionError("Nonpublic fields were accessed")
        return super().__getitem__(key)


def test_projection_is_an_explicit_allowlist_and_does_not_touch_metadata():
    original = entry()
    record = MetadataTrap(original.record,metadata_json=object(),notes="PRIVATE",relevance=99)
    facts = project_public_source(CatalogEntry(record,original.admission),context())
    assert set(facts.model_dump()) == {"registry_id","source_id","canonical_url","normalization_version",
        "domain","title","publisher","published_at","source_type","content_hash","content_hash_version","claims_verification"}
    assert facts.claims_verification == "not_independently_verified"
    assert "PRIVATE" not in facts.model_dump_json() and "original.example" not in facts.model_dump_json()


def test_legacy_hash_version_is_not_invented_and_inputs_are_unchanged():
    source = entry(version=None); before = copy.deepcopy(source.record)
    facts = project_public_source(source,context())
    assert facts.content_hash == source.record["content_hash"] and facts.content_hash_version is None
    decide_identity(query(),context(),url_match=source)
    assert source.record == before


def test_legacy_default_ports_and_query_order_are_preserved_exactly():
    stored = "https://example.com:443/a?b=2&a=1"
    source = entry(url=stored)
    facts = project_public_source(source,context())
    assert facts.source_id == source.record["id"] and facts.canonical_url == stored
    assert facts.content_hash == FINGERPRINT.value and facts.domain == "example.com:443"


def test_admission_does_not_upgrade_observed_claims_to_verified_facts():
    facts = project_public_source(entry(),context())
    assert facts.claims_verification == "not_independently_verified"
    with pytest.raises(ValidationError):
        PublicSourceFacts(**{**facts.model_dump(),"claims_verification":"verified"})


@pytest.mark.parametrize("url", [A+"/",A+"?utm_source=legacy","https://EXAMPLE.com/a"])
def test_legacy_canonical_keys_are_never_silently_recanonicalized(url):
    source = entry(url=url); before = copy.deepcopy(source.record)
    with pytest.raises(CatalogInputError,match="require review"):project_public_source(source,context())
    assert source.record == before
    with pytest.raises(ValidationError):IdentityQuery(canonical_url=url,observed_url=url)


@pytest.mark.parametrize("content", ["", " ", "\t\r\n", "\u2003\u00a0", None, 42, "x"*250001, "\ud800"])
def test_invalid_or_whitespace_content_is_rejected_before_hashing(content, monkeypatch):
    def unexpected(_): raise AssertionError("Invalid content reached hashing")
    monkeypatch.setattr("app.core.source_catalog.content_hash",unexpected)
    with pytest.raises(CatalogInputError):fingerprint_full_content(content)


def test_supported_fingerprint_preserves_current_hash_semantics():
    result = fingerprint_full_content("  FULL   Article\nText. ")
    assert result.value == content_hash("full article text.") and result.version == HASH_VERSION
    assert canonical_source_url(A+"/?utm_source=test") == A


@pytest.mark.parametrize("kwargs", [dict(version="future",value=FINGERPRINT.value),
    dict(version=HASH_VERSION,value="F"*64),dict(version=HASH_VERSION,value="0"*63),
    dict(version=HASH_VERSION,value=content_hash("")),
    dict(version=HASH_VERSION,value=FINGERPRINT.value,basis="snippet")])
def test_fingerprint_contract_requires_known_version_and_full_nonempty_claim(kwargs):
    with pytest.raises(ValidationError):ContentFingerprint(**kwargs)


@pytest.mark.parametrize("changes", [dict(normalization_version="future"),dict(observed_publisher="x"*501),
    dict(metadata={"private":"notes"}),dict(snippet="PRIVATE"),dict(notes="PRIVATE"),dict(fingerprint={"value":"bad"})])
def test_query_rejects_editorial_fields_and_malformed_contracts(changes):
    values = dict(canonical_url=A,observed_url=A,**changes)
    with pytest.raises(ValidationError) as error:IdentityQuery(**values)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("url", ["http://localhost/a","http://127.0.0.1/a","http://169.254.169.254/a",
    "http://168.63.129.16/a","https://user:secret@example.com/a",A+"?%61pi_key=secret",A+"?token=",
    A+"/bad%",A+"/has space",A+"/back\\slash","http://service.internal/a",A+"/"+"x"*2048])
def test_nonpublic_or_malformed_urls_are_not_projectable(url):
    with pytest.raises(ValidationError):IdentityQuery(canonical_url=url,observed_url=url)


@pytest.mark.parametrize("field,value", [("title"," "),("title","x"*1001),("publisher","x"*501),
    ("source_type","x"*101),("content_hash","not-a-hash"),("published_at",datetime(2026,11,7)),
    ("domain","incorrect.example"),("id","../private")])
def test_malformed_admitted_facts_fail_with_constant_diagnostic(field,value):
    original = entry(); record = {**original.record,field:value}
    if field == "id":
        assert project_public_source(CatalogEntry(record,original.admission),context()) is None
    else:
        with pytest.raises(CatalogInputError) as error:project_public_source(CatalogEntry(record,original.admission),context())
        assert str(error.value) == "Admitted source facts require review"


@pytest.mark.parametrize("case", ["url_slot","hash_value","hash_version","no_query_hash","split_snapshot","missing_url_slot"])
def test_inconsistent_admitted_lookup_inputs_are_rejected(case):
    request = query(); url = hashed = None
    if case == "url_slot":url = entry(url=B)
    elif case == "hash_value":hashed = entry("source-b",B,digest=DIFFERENT.value)
    elif case == "hash_version":hashed = entry("source-b",B,version=None)
    elif case == "no_query_hash":request=query(None);hashed=entry("source-b",B)
    elif case == "split_snapshot":url=entry();hashed=entry(title="Changed claim")
    else:hashed=entry()
    with pytest.raises(CatalogInputError):decide_identity(request,context(),url_match=url,content_match=hashed)


@pytest.mark.parametrize("factory", [lambda:CatalogContext(str(REGISTRY),"argus",True),
    lambda:CatalogContext(UUID(int=0),"argus",True),lambda:CatalogContext(REGISTRY,"argus","true"),
    lambda:TrustedAdmission(REGISTRY,"source-a","public"),lambda:TrustedAdmission(REGISTRY,"../private"),
    lambda:TrustedAdmission(REGISTRY,"source-a",fingerprint_version="future"),
    lambda:TrustedAdmission(REGISTRY,"source-a",policy_version="future"),
    lambda:CatalogEntry({}, {"metadata":{"public":True}})])
def test_untrusted_or_invalid_admission_context_is_rejected(factory):
    with pytest.raises(CatalogInputError):factory()


def test_runtime_requires_typed_inputs_and_outputs_are_frozen():
    with pytest.raises(CatalogInputError):decide_identity({},context())
    with pytest.raises(CatalogInputError):project_public_source(entry(),{})
    facts = project_public_source(entry(),context())
    with pytest.raises(ValidationError):facts.title="modified"
    with pytest.raises(ValidationError):PublicSourceFacts(**facts.model_dump(),metadata={"private":"notes"})


def test_decision_schema_rejects_forged_matches_conflicts_and_registry_mixups():
    baseline = decide_identity(query(),context(),url_match=entry()).model_dump()
    for changes in [dict(requires_review=True),dict(content_verification="not_supplied"),dict(outcome="not_found"),
                    dict(registry_id=OTHER_REGISTRY),dict(source=None),dict(conflict_code="url_content_disagreement")]:
        with pytest.raises(ValidationError):IdentityDecision(**{**baseline,**changes})

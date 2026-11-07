"""Internal PostgreSQL repository. Deliberately unused by routes and startup.

TrustedAdmissionExecution is a caller assertion, not an authority mechanism.
Deploy restricted credentials and an approved authority adapter before use.
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.models import SourceCatalogDB, SourceAdmissionEventDB, SourceRecordDB
from app.core.source_admission import (
    AdmissionCommand, AdmissionError, AdmissionEvent, TrustedAdmissionExecution,
    canonical_facts, stored_facts,
)
from app.core.source_catalog import PublicSourceFacts

catalogs = SourceCatalogDB.__table__
events = SourceAdmissionEventDB.__table__
sources = SourceRecordDB.__table__


class AdmissionConflict(AdmissionError):
    pass


class AdmissionWriteOutcomeUnknown(AdmissionError):
    """Reconcile operation_id explicitly; never automatically replay the append."""


def advisory_key(kind: str, registry_id: UUID, identifier: str) -> int:
    # Fixed namespace and field boundary; Python's process-randomized hash is forbidden.
    raw = b"argus:source-admission:lock:v1\0" + kind.encode("ascii") + b"\0" + registry_id.bytes + identifier.encode("ascii")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _registry(connection, expected: UUID) -> UUID:
    if type(expected) is not UUID or expected.int == 0:
        raise AdmissionError("Expected registry UUID required")
    found = connection.execute(select(catalogs.c.registry_id).where(catalogs.c.singleton == 1)).scalar_one_or_none()
    if found is None or found != expected:
        raise AdmissionError("Registry identity missing or mismatched")
    return found


def _event(row) -> AdmissionEvent:
    values = dict(row)
    facts = stored_facts(values["approved_facts"]) if values["approved_facts"] is not None else None
    command = AdmissionCommand(**{key: values[key] for key in AdmissionCommand.model_fields if key != "approved_facts"}, approved_facts=facts)
    return AdmissionEvent(event_id=values["event_id"], registry_id=values["registry_id"],
        decision_version=values["decision_version"], actor_issuer=values["actor_issuer"],
        actor_subject=values["actor_subject"], decided_at=values["decided_at"], command=command)


def _same(event, command, context):
    if (event.registry_id != context.registry_id or event.actor_issuer != context.actor_issuer
            or event.actor_subject != context.actor_subject or event.command != command):
        raise AdmissionConflict("Operation ID already belongs to a different command")
    return event


class AdmissionRepository:
    def __init__(self, engine: Engine, expected_registry_id: UUID):
        if engine.dialect.name != "postgresql":
            raise AdmissionError("Admission storage requires PostgreSQL")
        if type(expected_registry_id) is not UUID or expected_registry_id.int == 0:
            raise AdmissionError("Expected registry UUID required")
        self.engine = engine
        self.expected_registry_id = expected_registry_id

    def registry_identity(self) -> UUID:
        with self.engine.connect() as connection:
            return _registry(connection, self.expected_registry_id)

    def history(self, source_id: str, *, before_version: int | None = None, limit: int = 50) -> tuple[AdmissionEvent, ...]:
        import re
        if (type(source_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", source_id)
                or type(limit) is not int or not 1 <= limit <= 100
                or (before_version is not None and (type(before_version) is not int or before_version <= 0))):
            raise AdmissionError("Invalid bounded history request")
        with self.engine.connect() as connection:
            _registry(connection, self.expected_registry_id)
            query = select(events).where(events.c.registry_id == self.expected_registry_id, events.c.source_id == source_id)
            if before_version is not None:
                query = query.where(events.c.decision_version < before_version)
            return tuple(_event(row) for row in connection.execute(query.order_by(events.c.decision_version.desc()).limit(limit)).mappings())

    def latest(self, source_id: str) -> AdmissionEvent | None:
        rows = self.history(source_id, limit=1)
        return rows[0] if rows else None

    def reconcile(self, operation_id: UUID) -> AdmissionEvent | None:
        if type(operation_id) is not UUID:
            raise AdmissionError("Operation UUID required")
        with self.engine.connect() as connection:
            _registry(connection, self.expected_registry_id)
            row = connection.execute(select(events).where(events.c.operation_id == operation_id,
                events.c.registry_id == self.expected_registry_id)).mappings().one_or_none()
            return _event(row) if row else None

    def append(self, command: AdmissionCommand, *, execution: TrustedAdmissionExecution) -> AdmissionEvent:
        if type(command) is not AdmissionCommand or type(execution) is not TrustedAdmissionExecution:
            raise AdmissionError("Typed trusted execution context and command required")
        # Revalidate even frozen objects: model_construct() is not a validation boundary.
        command = AdmissionCommand.model_validate(command.model_dump())
        execution = TrustedAdmissionExecution.model_validate(execution.model_dump())
        if execution.registry_id != self.expected_registry_id:
            raise AdmissionError("Registry identity missing or mismatched")
        committing = False
        try:
            with self.engine.connect() as connection:
                transaction = connection.begin()
                try:
                    _registry(connection, self.expected_registry_id)
                    # Operation lock first; same-ID requests reconcile even across streams.
                    for kind, registry, identifier in (
                        ("operation", UUID(int=0), command.operation_id.hex),
                        ("stream", execution.registry_id, command.source_id),
                    ):
                        if not connection.execute(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                {"key": advisory_key(kind, registry, identifier)}).scalar_one():
                            raise AdmissionConflict("Admission operation is busy")
                    previous = connection.execute(select(events).where(events.c.operation_id == command.operation_id)).mappings().one_or_none()
                    if previous:
                        result = _same(_event(previous), command, execution)
                        transaction.rollback()
                        return result
                    source = connection.execute(select(*(sources.c[name] for name in (
                        "id", "canonical_url", "domain", "title", "publisher", "published_at", "source_type", "content_hash"
                    ))).where(sources.c.id == command.source_id)).mappings().one_or_none()
                    if source is None:
                        raise AdmissionError("Source unavailable for admission")
                    latest = connection.execute(select(events.c.decision_version, events.c.action).where(
                        events.c.registry_id == execution.registry_id, events.c.source_id == command.source_id
                    ).order_by(events.c.decision_version.desc()).limit(1)).one_or_none()
                    if command.expected_version != (latest.decision_version if latest else 0):
                        raise AdmissionConflict("Admission version changed")
                    if command.action == "revoke" and (latest is None or latest.action != "approve"):
                        raise AdmissionConflict("Only an approval can be revoked")
                    if command.action == "reject" and latest is not None and latest.action == "approve":
                        raise AdmissionConflict("Public access requires explicit revocation")
                    if command.action == "approve":
                        facts = PublicSourceFacts(registry_id=execution.registry_id, source_id=source["id"],
                            canonical_url=source["canonical_url"], domain=source["domain"], title=source["title"],
                            publisher=source["publisher"], published_at=source["published_at"], source_type=source["source_type"],
                            content_hash=source["content_hash"], content_hash_version=command.fingerprint_version)
                        if canonical_facts(facts) != canonical_facts(command.approved_facts):
                            raise AdmissionConflict("Reviewed facts changed")
                    values = command.model_dump(exclude={"approved_facts"})
                    values["approved_facts"] = json.loads(canonical_facts(command.approved_facts)) if command.approved_facts else None
                    values.update(event_id=uuid4(), registry_id=execution.registry_id,
                        decision_version=command.expected_version + 1,
                        actor_issuer=execution.actor_issuer, actor_subject=execution.actor_subject)
                    row = connection.execute(insert(events).values(**values).returning(events)).mappings().one()
                    result = _event(row)
                    committing = True
                    transaction.commit()
                    return result
                except BaseException:
                    if transaction.is_active:
                        transaction.rollback()
                    raise
        except SQLAlchemyError:
            if committing:
                raise AdmissionWriteOutcomeUnknown("Admission commit outcome unknown; reconcile operation ID") from None
            raise AdmissionError("Admission write failed; transaction rolled back") from None

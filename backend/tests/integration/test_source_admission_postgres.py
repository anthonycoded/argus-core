"""Admission behavior only in Alembic-built, uniquely disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, insert, inspect, select, text
from sqlalchemy.engine import RootTransaction
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.core.models import SourceCatalogDB, SourceAdmissionEventDB, SourceRecordDB
from app.core.source_admission import AdmissionCommand, AdmissionError, TrustedAdmissionExecution, SERIALIZATION_VERSION, facts_digest
from app.core.source_admission_repository import AdmissionConflict, AdmissionRepository, AdmissionWriteOutcomeUnknown
from app.core.source_catalog import PublicSourceFacts, URL_VERSION

TABLE = SourceAdmissionEventDB.__table__
REGISTRY = UUID("4f13392a-13a4-4ab9-b3a0-62a5fca20001")  # Synthetic namespace only.


def facts(**changes):
    return PublicSourceFacts(**(dict(registry_id=REGISTRY, source_id="source-a",
        canonical_url="https://example.com/article", domain="example.com", title="Public title",
        publisher=None, published_at=None, source_type="news", content_hash=None, content_hash_version=None) | changes))


def approval(**changes):
    snapshot=changes.pop("approved_facts",facts())
    return AdmissionCommand(**(dict(operation_id=uuid4(),source_id=snapshot.source_id,
        expected_version=0,action="approve",reason_code="public_facts_reviewed",
        normalization_version=URL_VERSION,serialization_version=SERIALIZATION_VERSION,
        fingerprint_version=snapshot.content_hash_version,approved_facts=snapshot,
        facts_digest=facts_digest(snapshot)) | changes))


def config(connection):
    # A file-backed Config invokes fileConfig(disable_existing_loggers=True) in
    # env.py, corrupting later caplog tests. Match the existing migration fixture.
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture
def database(migration_engine):
    with migration_engine.begin() as connection:
        command.upgrade(config(connection), "head")
        connection.execute(insert(SourceCatalogDB).values(singleton=1, registry_id=REGISTRY))
        connection.execute(insert(SourceRecordDB).values(id="source-a", title="Public title",
            url="https://example.com/article", canonical_url="https://example.com/article", domain="example.com",
            source_type="news", metadata_json={"private": "synthetic editorial fixture"}))
    return migration_engine


@pytest.fixture
def context():
    return TrustedAdmissionExecution(registry_id=REGISTRY, actor_issuer="offline-tests", actor_subject="synthetic-reviewer")


def nonapproval(action, version):
    return AdmissionCommand(operation_id=uuid4(), source_id="source-a", expected_version=version,
        action=action, reason_code="public_access_revoked" if action == "revoke" else "public_disclosure_rejected")


def test_empty_storage_and_identity_are_fail_closed(migration_engine):
    with migration_engine.begin() as connection:
        command.upgrade(config(connection), "head")
        assert connection.execute(select(TABLE)).first() is None
        assert connection.execute(select(SourceCatalogDB)).first() is None
    repo = AdmissionRepository(migration_engine, REGISTRY)
    with pytest.raises(AdmissionError, match="missing or mismatched"): repo.registry_identity()
    with migration_engine.begin() as connection:
        connection.execute(insert(SourceCatalogDB).values(singleton=1, registry_id=REGISTRY))
    assert repo.registry_identity() == REGISTRY
    assert AdmissionRepository(migration_engine, REGISTRY).registry_identity() == REGISTRY
    with pytest.raises(AdmissionError): AdmissionRepository(migration_engine, UUID(int=0))
    with pytest.raises(AdmissionError): AdmissionRepository(migration_engine, UUID(int=5)).registry_identity()
    assert repo.latest("source-a") is None


@pytest.mark.parametrize("values", [dict(singleton=2, registry_id=REGISTRY), dict(singleton=1, registry_id=UUID(int=0))])
def test_registry_constraints(migration_engine, values):
    with migration_engine.begin() as connection:
        command.upgrade(config(connection), "head")
    with pytest.raises(IntegrityError):
        with migration_engine.begin() as connection:connection.execute(insert(SourceCatalogDB).values(**values))


def test_transitions_history_snapshots_and_sources_unchanged(database, context):
    repo = AdmissionRepository(database, REGISTRY)
    with database.connect() as connection:
        before = dict(connection.execute(select(SourceRecordDB.__table__)).mappings().one())
    first = repo.append(approval(), execution=context)
    second = repo.append(nonapproval("revoke", 1), execution=context)
    third = repo.append(nonapproval("reject", 2), execution=context)
    fourth = repo.append(approval(expected_version=3), execution=context)
    assert [e.decision_version for e in (first,second,third,fourth)] == [1,2,3,4]
    assert second.command.approved_facts is None and third.command.approved_facts is None
    assert repo.latest("source-a") == fourth
    assert repo.history("source-a", limit=2) == (fourth, third)
    assert repo.history("source-a", before_version=3, limit=2) == (second, first)
    assert first.decided_at.utcoffset().total_seconds() == 0
    with database.connect() as connection:
        assert dict(connection.execute(select(SourceRecordDB.__table__)).mappings().one()) == before


@pytest.mark.parametrize("options", [dict(limit=0),dict(limit=101),dict(limit=True),dict(before_version=0)])
def test_bounded_history(database, options):
    with pytest.raises(AdmissionError): AdmissionRepository(database, REGISTRY).history("source-a", **options)


def test_optimistic_version_state_and_trust_checks(database, context):
    repo = AdmissionRepository(database, REGISTRY)
    with pytest.raises(AdmissionError): repo.append(approval(), execution={"actor_subject":"caller"})
    with pytest.raises(AdmissionConflict): repo.append(nonapproval("revoke",0), execution=context)
    repo.append(approval(), execution=context)
    with pytest.raises(AdmissionConflict): repo.append(approval(), execution=context)
    with pytest.raises(AdmissionConflict): repo.append(nonapproval("reject",1), execution=context)
    assert len(repo.history("source-a")) == 1


def test_reviewed_facts_must_match_and_unknown_hashes_stay_unknown(database, context):
    repo = AdmissionRepository(database, REGISTRY)
    with pytest.raises(AdmissionConflict):repo.append(approval(approved_facts=facts(title="Changed")), execution=context)
    with database.begin() as connection:
        connection.execute(SourceRecordDB.__table__.update().values(content_hash="a"*64))
    saved = repo.append(approval(approved_facts=facts(content_hash="a"*64)), execution=context)
    assert saved.command.fingerprint_version is None


def test_operation_reconciliation_is_idempotent_but_never_merges_commands(database, context):
    repo = AdmissionRepository(database, REGISTRY)
    proposed = approval()
    assert repo.reconcile(proposed.operation_id) is None
    result = repo.append(proposed, execution=context)
    assert repo.append(proposed, execution=context) == result
    assert repo.reconcile(proposed.operation_id) == result
    with pytest.raises(AdmissionConflict):
        repo.append(approval(operation_id=proposed.operation_id, expected_version=1), execution=context)
    other_actor = context.model_copy(update={"actor_subject":"different-synthetic-reviewer"})
    with pytest.raises(AdmissionConflict):repo.append(proposed, execution=other_actor)
    assert len(repo.history("source-a")) == 1


def test_lost_commit_acknowledgment_is_unknown_until_explicit_reconciliation(database, context, monkeypatch):
    repo = AdmissionRepository(database, REGISTRY)
    proposed = approval()
    original = RootTransaction.commit
    once = []
    def lost_ack(transaction):
        original(transaction)
        if not once:
            once.append(True)
            raise OperationalError("isolated lost commit acknowledgement", {}, Exception("offline fixture"))
    with monkeypatch.context() as patch:
        patch.setattr(RootTransaction, "commit", lost_ack)
        with pytest.raises(AdmissionWriteOutcomeUnknown):repo.append(proposed, execution=context)
    receipt = repo.reconcile(proposed.operation_id)
    assert receipt is not None and receipt.decision_version == 1
    assert len(repo.history("source-a")) == 1


def test_failed_sql_write_rolls_back_without_source_changes(database, context):
    def fail(conn,cursor,statement,parameters,ctx,many):
        if statement.startswith("INSERT INTO core_source_admission_events"):
            conn.exec_driver_sql("SELECT 1/0")
    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(AdmissionError, match="rolled back"):
            AdmissionRepository(database, REGISTRY).append(approval(), execution=context)
    finally:event.remove(database,"before_cursor_execute",fail)
    assert AdmissionRepository(database, REGISTRY).latest("source-a") is None


def test_concurrent_same_stream_does_not_duplicate_versions(database, context):
    barrier = Barrier(2)
    def append():
        barrier.wait(timeout=5)
        try:return AdmissionRepository(database, REGISTRY).append(approval(), execution=context)
        except AdmissionConflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(lambda _:append(),range(2)))
    assert sum(value is not None for value in values) == 1
    assert [e.decision_version for e in AdmissionRepository(database,REGISTRY).history("source-a")] == [1]


@pytest.mark.parametrize("change", [dict(decision_version=0),dict(expected_version=0),dict(action="invalid"),
    dict(policy_version="future"),dict(normalization_version="future"),dict(serialization_version="future"),
    dict(fingerprint_version="future"),dict(facts_digest="bad"),dict(approved_facts=None),
    dict(approved_facts={"metadata":{"public":True}}),dict(registry_id=UUID(int=8)),dict(source_id="missing")])
def test_database_constraints_reject_invalid_direct_inserts(database, context, change):
    AdmissionRepository(database,REGISTRY).append(approval(),execution=context)
    with database.connect() as connection:
        values=dict(connection.execute(select(TABLE)).mappings().one())
    values.update(event_id=uuid4(),operation_id=uuid4(),decision_version=2,expected_version=1)
    values.update(change)
    with pytest.raises(IntegrityError):
        with database.begin() as connection:connection.execute(insert(TABLE).values(**values))


def test_restricted_writer_can_append_but_cannot_rewrite_history(database, context):
    role="argus_admission_test_"+uuid4().hex
    with database.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        connection.exec_driver_sql(f'GRANT SELECT ON core_source_catalogs,core_source_admission_events TO "{role}"')
        connection.exec_driver_sql(f'GRANT INSERT ON core_source_admission_events TO "{role}"')
        connection.exec_driver_sql(f'GRANT SELECT (id,canonical_url,domain,title,publisher,published_at,source_type,content_hash) ON core_sources TO "{role}"')
    def assume(connection):connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
    event.listen(database,"begin",assume)
    try:
        saved=AdmissionRepository(database,REGISTRY).append(approval(),execution=context)
        assert saved.decision_version==1
        for sql in ("UPDATE core_source_admission_events SET actor_subject='changed'",
                    "DELETE FROM core_source_admission_events", "TRUNCATE core_source_admission_events"):
            with pytest.raises(DBAPIError) as error:
                with database.begin() as connection:connection.exec_driver_sql(sql)
            assert error.value.orig.sqlstate=="42501"
    finally:
        event.remove(database,"begin",assume)
        with database.begin() as connection:
            connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
            connection.exec_driver_sql(f'DROP ROLE "{role}"')


@pytest.mark.parametrize("table", ["core_source_catalogs","core_source_admission_events"])
def test_populated_storage_blocks_downgrade(database, context, table):
    if table.endswith("events"):
        AdmissionRepository(database,REGISTRY).append(approval(),execution=context)
    with pytest.raises(RuntimeError, match="populated"):
        with database.begin() as connection:command.downgrade(config(connection),"dea3e0498bf6")
    with database.connect() as connection:
        assert set(inspect(connection).get_table_names()) >= {"core_source_catalogs","core_source_admission_events"}
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "61c2a9e47b30"


@pytest.mark.parametrize("mutation", ["UPDATE core_source_admission_events SET actor_subject='changed'",
    "DELETE FROM core_source_admission_events", "TRUNCATE core_source_admission_events",
    "INSERT INTO core_source_admission_events DEFAULT VALUES", "DELETE FROM core_source_catalogs"])
def test_restricted_runtime_role_cannot_mutate(database, context, mutation):
    AdmissionRepository(database,REGISTRY).append(approval(),execution=context)
    role="argus_admission_test_"+uuid4().hex
    # Ephemeral NOLOGIN role; SET ROLE stays on this verified disposable connection.
    with database.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        connection.exec_driver_sql(f'GRANT SELECT ON core_source_catalogs,core_source_admission_events TO "{role}"')
    try:
        with database.connect() as connection:
            transaction=connection.begin()
            connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
            assert connection.execute(select(TABLE.c.event_id)).first()
            with pytest.raises(DBAPIError) as error:connection.exec_driver_sql(mutation)
            assert error.value.orig.sqlstate == "42501"
            transaction.rollback()
    finally:
        with database.begin() as connection:
            connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
            connection.exec_driver_sql(f'DROP ROLE "{role}"')

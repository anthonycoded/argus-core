import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.models import CoreBase, SourceRecordDB
from app.services.source_registry import (
    register_source,
    register_source_batch,
)

@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    CoreBase.metadata.create_all(bind=engine)

    TestingSession = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    with TestingSession() as session:
        yield session

    engine.dispose()


def test_register_new_source(db_session):
    result = register_source(
        db_session,
        title="Example article",
        url="https://example.com/article?utm_source=test",
        content="Example article content.",
        publisher="Example News",
        source_type="news",
    )

    assert result["status"] == "created"
    assert result["canonical_url"] == "https://example.com/article"

    saved = db_session.get(SourceRecordDB, result["source_id"])
    assert saved is not None
    assert saved.title == "Example article"


def test_detect_duplicate_canonical_url(db_session):
    first = register_source(
        db_session,
        title="First article",
        url="https://example.com/article?utm_source=one",
    )

    second = register_source(
        db_session,
        title="Same article",
        url="https://example.com/article?utm_source=two",
    )

    assert first["status"] == "created"
    assert second["status"] == "duplicate"
    assert second["reason"] == "canonical_url"
    assert second["source_id"] == first["source_id"]


def test_detect_duplicate_content(db_session):
    first = register_source(
        db_session,
        title="First article",
        url="https://example.com/first",
        content="The same article content.",
    )

    second = register_source(
        db_session,
        title="Republished article",
        url="https://another-example.com/republished",
        content="  THE SAME ARTICLE CONTENT.  ",
    )

    assert first["status"] == "created"
    assert second["status"] == "duplicate"
    assert second["reason"] == "content_hash"


def test_persist_source_across_sessions(db_session):
    result = register_source(
        db_session,
        title="Persistent article",
        url="https://example.com/persistent",
    )

    source_id = result["source_id"]

    # Open a separate session against the same test database.
    TestingSession = sessionmaker(bind=db_session.get_bind())

    with TestingSession() as second_session:
        saved = second_session.get(SourceRecordDB, source_id)

    assert saved is not None
    assert saved.title == "Persistent article"

    
def test_register_source_batch(db_session):
    sources = [
        {
            "title": "Ukraine update",
            "url": "https://example.com/ukraine",
            "content": "Ukraine article content.",
            "publisher": "Example News",
            "source_type": "news",
        },
        {
            "title": "Iran update",
            "url": "https://example.com/iran",
            "content": "Iran article content.",
            "publisher": "Example News",
            "source_type": "news",
        },
    ]

    result = register_source_batch(db_session, sources)

    assert result["status"] == "completed"
    assert result["total"] == 2
    assert result["created"] == 2
    assert result["duplicates"] == 0
    assert result["failed"] == 0
    assert len(result["results"]) == 2


def test_register_source_batch_detects_duplicates(db_session):
    sources = [
        {
            "title": "First article",
            "url": "https://example.com/article",
        },
        {
            "title": "Duplicate article",
            "url": "https://example.com/article?utm_source=test",
        },
    ]

    result = register_source_batch(db_session, sources)

    assert result["status"] == "completed"
    assert result["total"] == 2
    assert result["created"] == 1
    assert result["duplicates"] == 1
    assert result["failed"] == 0


def test_register_source_batch_handles_individual_failures(db_session):
    sources = [
        {
            "title": "Valid article",
            "url": "https://example.com/valid",
        },
        {
            "url": "https://example.com/invalid",
        },
        {
            "title": "Another valid article",
            "url": "https://example.com/another",
        },
    ]

    result = register_source_batch(db_session, sources)

    assert result["status"] == "completed_with_errors"
    assert result["total"] == 3
    assert result["created"] == 2
    assert result["duplicates"] == 0
    assert result["failed"] == 1

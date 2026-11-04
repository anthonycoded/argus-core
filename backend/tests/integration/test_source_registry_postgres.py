
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.core.models import SourceRecordDB
from app.services.source_registry import register_source
from app.utils.deduplication import content_hash


def test_concurrent_duplicate_content_registration(
    postgres_test_session_factory,
):
    content = "A shared article used to test concurrent registration."
    fingerprint = content_hash(content)

    barrier = Barrier(2)

    def synchronize_inserts(session, flush_context, instances):
        if session.info.get("concurrency_test_ready"):
            session.info["concurrency_test_ready"] = False
            barrier.wait(timeout=15)

    def register(url):
        with postgres_test_session_factory() as db:
            db.info["concurrency_test_ready"] = True

            return register_source(
                db,
                title="Concurrent registration test",
                url=url,
                content=content,
                publisher="Integration Test",
                source_type="test",
            )

    event.listen(Session, "before_flush", synchronize_inserts)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    register,
                    "https://example.com/concurrent-one",
                ),
                executor.submit(
                    register,
                    "https://example.com/concurrent-two",
                ),
            ]

            results = [future.result(timeout=30) for future in futures]

    finally:
        event.remove(Session, "before_flush", synchronize_inserts)

    statuses = sorted(result["status"] for result in results)

    assert statuses == ["created", "duplicate"]

    duplicate = next(
        result for result in results
        if result["status"] == "duplicate"
    )

    assert duplicate["reason"] == "content_hash"
    assert duplicate["source_id"]

    with postgres_test_session_factory() as db:
        matching_records = db.scalar(
            select(func.count())
            .select_from(SourceRecordDB)
            .where(SourceRecordDB.content_hash == fingerprint)
        )

    assert matching_records == 1

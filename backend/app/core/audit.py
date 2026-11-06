"""Best-effort request audit persistence, independent of endpoint transactions."""

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter
from threading import BoundedSemaphore

import anyio
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.models.audit_log import AuditLogDB

logger = logging.getLogger(__name__)


def create_audit_session_factory(database_url: str):
    """Lazy, bounded pool; never change the endpoint engine's timeout policy."""
    dialect = make_url(database_url).get_backend_name()
    connect_args = {}
    if dialect == "postgresql":
        connect_args = {
            "connect_timeout": 2,
            "options": "-c statement_timeout=1000 -c lock_timeout=500 -c idle_in_transaction_session_timeout=2000",
        }
    elif dialect == "sqlite":
        connect_args = {"timeout": 1, "check_same_thread": False}
    engine = create_engine(
        database_url, poolclass=QueuePool, pool_size=2, max_overflow=0,
        pool_timeout=1, connect_args=connect_args,
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class RequestAuditMiddleware:
    def __init__(self, app: ASGIApp, session_factory: Callable[[], Session], timeout_seconds: float = 3):
        self.app = app
        self.session_factory = session_factory
        self.timeout_seconds = timeout_seconds
        # A timed-out worker may still be in a driver call. Bound actual workers,
        # not just awaiting tasks, so an outage cannot create unlimited writes.
        self.workers = BoundedSemaphore(2)

    def persist(self, values: dict) -> None:
        # Never reuse the request session: an endpoint rollback must not erase
        # the audit record, and an audit failure must not affect endpoint data.
        if not self.workers.acquire(blocking=False):
            raise RuntimeError("Audit writer capacity exhausted")
        try:
            with self.session_factory() as db:
                db.add(AuditLogDB(**values))
                db.commit()
        finally:
            self.workers.release()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/api/v1" or path.startswith("/api/v1/")
        ):
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        timestamp = datetime.now(timezone.utc)
        status_code = 500

        async def audit_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, audit_send)
        finally:
            route = scope.get("route")
            normalized_path = getattr(route, "path", "/<unmatched>")
            if not (normalized_path == "/api/v1/health" and status_code < 400):
                state = scope.get("state", {})
                values = {
                    "application_id": state.get("audit_application_id"),
                    "api_key_id": state.get("audit_api_key_id"),
                    "method": scope.get("method", "")[:10],
                    "path": normalized_path[:500],
                    "status_code": status_code,
                    "duration_ms": min(max(0, int((perf_counter() - started) * 1000)), 2147483647),
                    "created_at": timestamp,
                }
                try:
                    with anyio.fail_after(self.timeout_seconds):
                        await anyio.to_thread.run_sync(self.persist, values, abandon_on_cancel=True)
                except Exception:
                    # SQLAlchemy exception strings can contain statement values
                    # and connection details. Do not log them or request data.
                    logger.error("Request audit persistence failed; record dropped.")

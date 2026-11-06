from app.api.v1.router import api_router
from app.core.audit import create_audit_session_factory
from app.core.config import settings
from app.core.database import initialize_core_database
from app.core.redis import close_redis
from app.factory import create_app
from starlette.concurrency import run_in_threadpool

audit_sessions = create_audit_session_factory(settings.database_url)


async def close_core_resources():
    try:
        await close_redis()
    finally:
        await run_in_threadpool(audit_sessions.kw["bind"].dispose)

app = create_app(
    title="Argus-Core",
    version="0.1.0",
    routers=(api_router,),
    initialize_database=initialize_core_database,
    on_shutdown=close_core_resources,
    audit_session_factory=audit_sessions,
)

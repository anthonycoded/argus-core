from app.api.v1.router import api_router
from app.core.database import initialize_core_database
from app.core.redis import close_redis
from app.factory import create_app

app = create_app(
    title="Argus-Core",
    version="0.1.0",
    routers=(api_router,),
    initialize_database=initialize_core_database,
    on_shutdown=close_redis,
)
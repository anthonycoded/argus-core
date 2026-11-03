from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings
from app.core.models.base import CoreBase


DATABASE_URL = settings.database_url

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from backend/.env")


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def initialize_core_database() -> None:
    from app.core.models import SourceRecordDB  # noqa: F401

    CoreBase.metadata.create_all(bind=engine)
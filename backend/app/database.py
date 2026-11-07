
"""Global Chess-owned database resources; no Core database or settings imports."""

from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.global_chess_config import GlobalChessDatabaseConfigurationError, global_chess_settings


class Base(DeclarativeBase):
    """Metadata for Global Chess's five application tables only."""


DATABASE_URL = global_chess_settings.database_connection_url()

try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
except (ArgumentError, ValueError):
    raise GlobalChessDatabaseConfigurationError(
        "Invalid GLOBAL_CHESS_DATABASE_URL or development compatibility URL. "
        "Configure the intended database explicitly."
    ) from None

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


"""
Backward-compatible database imports.

New application code should import from app.core.database.
"""

from app.core.database import (
    Base,
    DATABASE_URL,
    SessionLocal,
    engine,
    get_db,
)

__all__ = [
    "Base",
    "DATABASE_URL",
    "SessionLocal",
    "engine",
    "get_db",
]

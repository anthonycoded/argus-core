"""Global Chess metadata only: no settings, engines, sessions or Core imports."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Metadata for Global Chess's five application tables only."""

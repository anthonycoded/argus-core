from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.models.application import ApplicationDB


def register_application(
    db: Session,
    *,
    name: str,
    slug: str,
    description: str | None = None,
) -> ApplicationDB:
    """Register a consumer application."""

    existing = db.scalar(
        select(ApplicationDB).where(ApplicationDB.slug == slug)
    )

    if existing:
        raise ValueError(f"Application '{slug}' is already registered.")

    application = ApplicationDB(
        name=name,
        slug=slug,
        description=description,
    )

    db.add(application)

    try:
        db.commit()
        db.refresh(application)
    except IntegrityError:
        db.rollback()
        raise ValueError(
            f"Application '{slug}' is already registered."
        )

    return application


def get_application_by_slug(
    db: Session,
    slug: str,
) -> ApplicationDB | None:
    """Retrieve an application by its unique slug."""

    return db.scalar(
        select(ApplicationDB).where(ApplicationDB.slug == slug)
    )
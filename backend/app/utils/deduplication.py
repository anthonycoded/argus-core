import hashlib
import re
from urllib.parse import urlsplit

from app.utils.source_normalization import normalize_url


def canonical_source_url(url: str) -> str:
    """Return a normalized canonical source URL."""
    return normalize_url(url)


def content_hash(content: str) -> str:
    """Generate a SHA-256 hash for normalized article content."""
    normalized = re.sub(r"\s+", " ", content).strip().lower()

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def source_domain(url: str) -> str:
    """Extract the domain from a source URL."""
    return urlsplit(normalize_url(url)).netloc.lower()

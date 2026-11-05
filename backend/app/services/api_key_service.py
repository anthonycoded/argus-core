
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedAPIKey:
    plaintext_key: str
    key_prefix: str
    key_hash: str


def hash_api_key(secret: str) -> str:
    """Return the SHA-256 hash of an API key."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(environment: str = "live") -> GeneratedAPIKey:
    """
    Generate a cryptographically secure API key.

    The plaintext key is returned once and must never be persisted.
    """

    if environment not in {"live", "test"}:
        raise ValueError("Environment must be 'live' or 'test'.")

    secret = secrets.token_urlsafe(32)

    plaintext_key = f"argus_{environment}_{secret}"

    key_prefix = plaintext_key[:16]

    return GeneratedAPIKey(
        plaintext_key=plaintext_key,
        key_prefix=key_prefix,
        key_hash=hash_api_key(plaintext_key),
    )


def verify_api_key(plaintext_key: str, stored_hash: str) -> bool:
    """Verify a supplied API key against its stored hash."""

    if not plaintext_key or not stored_hash:
        return False

    candidate_hash = hash_api_key(plaintext_key)

    return secrets.compare_digest(candidate_hash, stored_hash)

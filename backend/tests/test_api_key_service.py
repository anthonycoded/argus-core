
import pytest

from app.services.api_key_service import (
    generate_api_key,
    hash_api_key,
    verify_api_key,
)


def test_generate_api_key():
    generated = generate_api_key()

    assert generated.plaintext_key.startswith("argus_live_")
    assert generated.key_prefix == generated.plaintext_key[:16]
    assert len(generated.key_hash) == 64
    assert generated.key_hash != generated.plaintext_key


def test_generated_api_keys_are_unique():
    first = generate_api_key()
    second = generate_api_key()

    assert first.plaintext_key != second.plaintext_key
    assert first.key_hash != second.key_hash


def test_verify_valid_api_key():
    generated = generate_api_key()

    assert verify_api_key(
        generated.plaintext_key,
        generated.key_hash,
    )


def test_reject_invalid_api_key():
    generated = generate_api_key()

    assert not verify_api_key(
        "argus_live_invalid_secret",
        generated.key_hash,
    )


def test_reject_empty_api_key():
    assert not verify_api_key("", "somehash")
    assert not verify_api_key("somekey", "")


def test_hash_api_key_is_deterministic():
    secret = "argus_live_example_secret"

    assert hash_api_key(secret) == hash_api_key(secret)


def test_reject_invalid_environment():
    with pytest.raises(ValueError):
        generate_api_key("production")

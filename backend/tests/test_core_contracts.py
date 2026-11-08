"""Core-owned contract assertions retained from retired consumer integration tests."""

import hashlib

import pytest
from pydantic import ValidationError

from app.api.v1.ai import AIRequest
from app.api.v1.schemas import SourceRegistrationRequest
from app.utils.deduplication import canonical_source_url, content_hash


@pytest.mark.parametrize("system_size,user_size,valid", [
    (10000, 30000, True), (10001, 1, False), (1, 30001, False),
])
def test_core_ai_prompt_boundaries(system_size, user_size, valid):
    payload = dict(system_prompt="s" * system_size, user_prompt="u" * user_size,
                   response_schema={"type": "object"}, timeout=120, temperature=0.2)
    if valid:
        request = AIRequest.model_validate(payload)
        assert request.system_prompt == payload["system_prompt"]
        assert request.user_prompt == payload["user_prompt"]
    else:
        with pytest.raises(ValidationError):
            AIRequest.model_validate(payload)


def test_core_registration_normalization_and_full_content_fingerprint():
    for url, expected in [
        ("https://example.com/article/?utm_source=rss#observed", "https://example.com/article"),
        ("https://EXAMPLE.com:443/a/?id=2&utm_source=x&b=1#fragment", "https://example.com/a?id=2&b=1"),
        ("https://example.com", "https://example.com/"),
    ]:
        request = SourceRegistrationRequest(title="Public", url=url)
        assert canonical_source_url(str(request.url)) == expected
    assert content_hash("  A\n Full ARTICLE. ") == hashlib.sha256(b"a full article.").hexdigest()

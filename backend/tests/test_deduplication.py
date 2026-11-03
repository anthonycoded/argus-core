from app.utils.deduplication import (
    canonical_source_url,
    content_hash,
    source_domain,
)


def test_canonical_source_url_removes_tracking_parameters():
    url = "https://example.com/article?utm_source=rss&fbclid=123"

    result = canonical_source_url(url)

    assert result == "https://example.com/article"


def test_canonical_source_url_matches_different_tracking_urls():
    url1 = "https://example.com/article?utm_source=rss"
    url2 = "https://example.com/article?fbclid=123"

    assert canonical_source_url(url1) == canonical_source_url(url2)


def test_content_hash_matches_identical_content():
    content1 = "This is an example article."
    content2 = "This is an example article."

    assert content_hash(content1) == content_hash(content2)


def test_content_hash_ignores_case_and_whitespace():
    content1 = "This is an example article."
    content2 = "  THIS IS AN EXAMPLE   ARTICLE.  "

    assert content_hash(content1) == content_hash(content2)


def test_content_hash_detects_different_content():
    content1 = "Article about Ukraine."
    content2 = "Article about Taiwan."

    assert content_hash(content1) != content_hash(content2)


def test_source_domain_extracts_domain():
    url = "https://www.example.com/article?utm_source=rss"

    assert source_domain(url) == "www.example.com"
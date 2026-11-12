"""Bounded, deliberately unverified public observations of an article fetch."""

import json
import logging
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.services.article_fetch import UnsafeDestination, validate_url

MAX_URL_BYTES = 2048
MAX_PROVENANCE_BYTES = 32 * 1024
MAX_REPORTED_REDIRECTS = 5


def disclose_url(value: str | httpx.URL) -> str | None:
    """Remove *all* queries/fragments; withhold unsafe or opaque representations.

    This is a display reference, never an equivalent resource/cache identity.
    No secret-parameter blacklist is used. Encoded, token-like and credential-like
    paths are withheld rather than guessing whether their contents are public.
    """
    try:
        raw = str(value)
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            return None
        url = validate_url(value)
        path = url.raw_path.split(b"?", 1)[0].decode("ascii")
        if not re.fullmatch(r"[/A-Za-z0-9_.~-]*", path):
            return None
        if any(len(segment) > 128 for segment in path.split("/")):
            return None
        # Opaque wrapper IDs and credential endpoints are not public article paths.
        if is_google_wrapper(url) or any(
            segment.lower() in {"token", "tokens", "secret", "secrets", "auth", "oauth", "login", "password", "credentials"}
            for segment in path.split("/")
        ):
            return None
        safe = str(url.copy_with(query=None, fragment=None))
        return safe if len(safe.encode("utf-8")) <= MAX_URL_BYTES else None
    except (UnsafeDestination, httpx.InvalidURL, TypeError, ValueError, UnicodeError):
        return None


def is_google_wrapper(value: str | httpx.URL) -> bool:
    try:
        return httpx.URL(value).host.rstrip(".").lower() == "news.google.com"
    except (httpx.InvalidURL, TypeError, ValueError):
        return False


@dataclass
class FetchProvenance:
    final_url: str | None = None
    redirect_chain: list[dict] = field(default_factory=list)
    redirect_chain_complete: bool = False
    response_received_at: str | None = None
    response_host: str | None = None
    urls_redacted: bool = False
    wrapper_unresolved: bool = False
    text_truncated: bool = False

    def url(self, value: str | httpx.URL) -> str | None:
        safe = disclose_url(value)
        self.urls_redacted |= safe != str(value)
        return safe

    def redirect(self, source: httpx.URL, target: httpx.URL, status: int) -> None:
        if len(self.redirect_chain) < MAX_REPORTED_REDIRECTS:
            self.redirect_chain.append({
                "source_url": self.url(source),
                "target_url": self.url(target),
                "status_code": status,
            })

    def terminal(self, url: httpx.URL) -> None:
        # Called at terminal response headers, before status/body/parser checks.
        self.response_received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.redirect_chain_complete = True
        self.final_url = self.url(url)
        # A withheld URL must not disclose its hostname through another field.
        self.response_host = url.host if self.final_url is not None else None
        self.wrapper_unresolved = is_google_wrapper(url)

    def withhold_urls(self) -> None:
        self.final_url = self.response_host = None
        for entry in self.redirect_chain:
            entry["source_url"] = entry["target_url"] = None
        self.urls_redacted = True

    def public(self, text: str | None) -> dict | None:
        result = {
            "schema_version": 1,
            "final_url": self.final_url,
            "redirect_chain": self.redirect_chain,
            "redirect_chain_complete": self.redirect_chain_complete,
            "response_received_at": self.response_received_at,
            "text_length_chars": len(text or ""),
            "text_length_utf8_bytes": len((text or "").encode("utf-8")),
            "text_truncated": self.text_truncated,
            "response_host": self.response_host,
            "publisher_canonical_url": None,
            "intended_article_resolved": False if self.wrapper_unresolved else None,
            "source_identity_status": (
                "google_news_wrapper_unresolved" if self.wrapper_unresolved else
                "unverified" if self.redirect_chain_complete else "not_established"
            ),
            "urls_redacted": self.urls_redacted,
        }
        # Check the larger default JSON representation (including ASCII escapes).
        # Omit optional provenance entirely if a future change exceeds its budget.
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_PROVENANCE_BYTES:
            return None
        return result


_private_logs = ContextVar("article_private_logs", default=False)


class _PrivateFetchFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX INFO includes raw URLs; httpcore DEBUG includes headers/errors.
        # The parser dependencies may also log source data. Keep our own fixed
        # categories, but suppress dependency detail only in this async context.
        return not _private_logs.get()


_filter = _PrivateFetchFilter()
for _name in set(logging.Logger.manager.loggerDict) | {
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
}:
    if _name.split(".")[0] in {"httpx", "httpcore", "trafilatura", "courlan", "htmldate", "justext"}:
        logging.getLogger(_name).addFilter(_filter)


@asynccontextmanager
async def private_fetch_logs():
    token = _private_logs.set(True)
    try:
        yield
    finally:
        _private_logs.reset(token)

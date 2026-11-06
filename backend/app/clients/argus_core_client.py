"""Global Chess's authenticated Core client; no Core service or ORM imports."""

import asyncio
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from app.global_chess_config import global_chess_settings
from app.clients.discovery_contracts import CoreDiscoveryResult, GoogleNewsDiscoveryRequest
from app.clients.rss_discovery_contracts import RSSPage, RSSRequest

RSS_COLLECTION_TIMEOUT = 60.0


@dataclass(frozen=True)
class InferenceResult:
    content: str = field(repr=False)
    model: str


class CoreClientError(Exception):
    """Only sanitized, application-facing details belong in this exception."""

    def __init__(self, status_code: int, detail: str, retry_after: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


class CoreInferenceClient(Protocol):
    async def generate_structured(
        self, *, system_prompt: str, user_prompt: str,
        response_schema: dict[str, Any], timeout: float = 180.0,
        temperature: float = 0.2,
    ) -> InferenceResult: ...


class CoreDiscoveryClient(Protocol):
    async def discover_google_news(self, *, query: str, limit: int = 10) -> CoreDiscoveryResult: ...

    async def discover_rss(self, *, feed_id: str) -> list[dict]: ...


class RSSDiscoveryError(CoreClientError):
    """A failed/incomplete snapshot with evidence from validated pages only."""

    def __init__(self, error: CoreClientError, partial_sources: list[dict]):
        super().__init__(error.status_code, error.detail, error.retry_after)
        self.partial_sources = partial_sources


def validated_retry_after(value: str | None) -> str | None:
    if not value:
        return None
    if value.isascii() and value.isdigit() and len(value) <= 10:
        return str(int(value)) if int(value) <= 2147483647 else None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            return None
        return format_datetime(parsed.astimezone(timezone.utc), usegmt=True)
    except (ValueError, TypeError, OverflowError):
        return None


class ArgusCoreClient:
    def __init__(
        self, *, base_url: str | None, api_key: SecretStr | str | None,
        request_timeout: float = 190.0, http_client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key or "")
        self._request_timeout = request_timeout
        self._http_client = http_client

    def _endpoint_and_key(self, path: str) -> tuple[str, str]:
        try:
            base = httpx.URL(self._base_url or "")
            if base.scheme not in {"http", "https"} or not base.host or base.userinfo or base.query or base.fragment:
                raise ValueError
            key = self._api_key.get_secret_value()
            if not key or any(not 33 <= ord(char) <= 126 for char in key):
                raise ValueError
            if not 0 < self._request_timeout <= 600:
                raise ValueError
            return str(base).rstrip("/") + path, key
        except (ValueError, TypeError, httpx.InvalidURL):
            raise CoreClientError(503, "Argus-Core integration is not configured correctly.") from None

    async def _post(self, path: str, payload: dict, operation: str) -> httpx.Response:
        endpoint, key = self._endpoint_and_key(path)
        try:
            if self._http_client is not None:
                return await self._http_client.post(
                    endpoint, json=payload, headers={"X-API-Key": key},
                    timeout=self._request_timeout, follow_redirects=False,
                )
            async with httpx.AsyncClient(trust_env=False) as client:
                return await client.post(
                    endpoint, json=payload, headers={"X-API-Key": key},
                    timeout=self._request_timeout, follow_redirects=False,
                )
        except httpx.TimeoutException:
            raise CoreClientError(504, f"{operation} request timed out.") from None
        except httpx.HTTPError:
            detail = "Argus-Core inference request failed." if operation == "AI inference" else "Argus-Core discovery request failed."
            raise CoreClientError(502, detail) from None

    async def discover_google_news(self, *, query: str, limit: int = 10) -> CoreDiscoveryResult:
        try:
            payload = GoogleNewsDiscoveryRequest(query=query, limit=limit)
        except (ValueError, TypeError):
            raise CoreClientError(422, "Discovery requires a nonempty query of at most 1000 characters and a limit of 1-75.") from None
        response = await self._post("/api/v1/discovery", payload.model_dump(), "Discovery")
        retry_after = validated_retry_after(response.headers.get("Retry-After"))
        failures = {
            401: (502, "Argus-Core authentication failed."),
            403: (502, "Argus-Core discovery permission denied."),
            429: (429, "Discovery rate limit exceeded."),
            503: (503, "Discovery service unavailable."),
            504: (504, "Discovery request timed out."),
        }
        if response.status_code in failures:
            status, detail = failures[response.status_code]
            raise CoreClientError(status, detail, retry_after if status in {429, 503} else None)
        if response.status_code != 200:
            raise CoreClientError(502, "Argus-Core discovery request failed.", retry_after)
        try:
            result = CoreDiscoveryResult.model_validate_json(response.content)
            if len(result.candidates) > payload.limit:
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise CoreClientError(502, "Argus-Core returned an invalid discovery response.") from None

    async def discover_rss(self, *, feed_id: str) -> list[dict]:
        sources = []
        try:
            request = RSSRequest(feed_id=feed_id)
        except (ValueError, TypeError):
            raise CoreClientError(422, "Invalid RSS feed identifier.") from None
        first = None
        offset = 0
        cursors = set()
        rejected_count = 0
        response_bytes = 0
        try:
            # Seven 75-observation pages cover Core's 500-observation bound.
            # A total deadline also bounds slow multi-page Core operations.
            async with asyncio.timeout(RSS_COLLECTION_TIMEOUT):
                for _ in range(7):
                    response = await self._post("/api/v1/discovery", request.model_dump(), "RSS discovery")
                    retry_after = validated_retry_after(response.headers.get("Retry-After"))
                    failures = {
                        401: (502, "Argus-Core authentication failed."),
                        403: (502, "Argus-Core discovery permission denied."),
                        410: (410, "RSS snapshot expired; coverage is incomplete."),
                        429: (429, "RSS discovery rate limit exceeded."),
                        503: (503, "RSS discovery service unavailable."),
                        504: (504, "RSS discovery request timed out."),
                    }
                    if response.status_code != 200:
                        status, detail = failures.get(response.status_code, (502, "Argus-Core RSS discovery request failed."))
                        raise CoreClientError(status, detail, retry_after if status in {429, 503} else None)
                    response_bytes += len(response.content)
                    try:
                        if response_bytes > 4 * 1024 * 1024:
                            raise ValueError
                        page = RSSPage.model_validate_json(response.content)
                        if (page.feed_id != feed_id or page.offset != offset
                                or page.entries_in_page != min(75, page.total_entries - offset)
                                or (first is not None and page.identity() != first.identity())
                                or (page.next_cursor is not None and page.next_cursor in cursors)):
                            raise ValueError
                        if first is None:
                            first = page
                    except (ValueError, TypeError):
                        raise CoreClientError(502, "Argus-Core returned an invalid RSS discovery response.") from None
                    sources.extend(page.research_sources())
                    rejected_count += len(page.rejected_entries)
                    offset += page.entries_in_page
                    if not page.has_more:
                        if rejected_count != page.invalid_entries:
                            raise CoreClientError(502, "Argus-Core returned inconsistent RSS entry counts.")
                        if page.status == "incomplete":
                            raise CoreClientError(502, "RSS feed is incomplete; some entries or metadata could not be accepted.")
                        return sources
                    cursors.add(page.next_cursor)
                    request = request.model_copy(update={"cursor": page.next_cursor})
                raise CoreClientError(502, "RSS snapshot exceeds the pagination limit.")
        except TimeoutError:
            raise RSSDiscoveryError(CoreClientError(504, "RSS snapshot collection timed out."), sources) from None
        except CoreClientError as exc:
            raise RSSDiscoveryError(exc, sources) from None

    async def generate_structured(
        self, *, system_prompt: str, user_prompt: str,
        response_schema: dict[str, Any], timeout: float = 180.0,
        temperature: float = 0.2,
    ) -> InferenceResult:
        payload = {
            "system_prompt": system_prompt, "user_prompt": user_prompt,
            "response_schema": response_schema, "timeout": timeout, "temperature": temperature,
        }
        response = await self._post("/api/v1/ai/generate", payload, "AI inference")

        retry_after = validated_retry_after(response.headers.get("Retry-After"))
        failures = {
            401: (502, "Argus-Core authentication failed."),
            403: (502, "Argus-Core inference permission denied."),
            429: (429, "AI inference rate limit exceeded."),
            503: (503, "AI inference service unavailable."),
            504: (504, "AI inference request timed out."),
        }
        if response.status_code in failures:
            status, detail = failures[response.status_code]
            raise CoreClientError(status, detail, retry_after if status in {429, 503} else None)
        if response.status_code != 200:
            raise CoreClientError(502, "Argus-Core rejected the inference request.")
        try:
            data = response.json()
            if not isinstance(data, dict) or data.get("status") != "success":
                raise ValueError
            content, model = data["result"], data["model"]
            # TopicAnalysis.model is NOT NULL varchar(100). Never invent a model
            # name or defer a missing/oversized model to a database write failure.
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            if not isinstance(model, str) or not model.strip() or len(model) > 100:
                raise ValueError
            return InferenceResult(content=content, model=model)
        except (ValueError, KeyError, TypeError):
            raise CoreClientError(502, "Argus-Core returned an invalid inference response.") from None


def get_core_inference_client() -> CoreInferenceClient:
    return ArgusCoreClient(
        base_url=global_chess_settings.core_base_url,
        api_key=global_chess_settings.core_api_key,
        request_timeout=global_chess_settings.core_request_timeout,
    )


def get_core_discovery_client() -> CoreDiscoveryClient:
    return ArgusCoreClient(
        base_url=global_chess_settings.core_base_url,
        api_key=global_chess_settings.core_api_key,
        request_timeout=global_chess_settings.core_request_timeout,
    )

"""Global Chess's authenticated Core client; no Core service or ORM imports."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from app.global_chess_config import global_chess_settings
from app.clients.discovery_contracts import CoreDiscoveryResult, GoogleNewsDiscoveryRequest
from app.clients.rss_discovery_contracts import RSSPage, RSSRequest
from app.clients.source_contracts import (
    RegisteredSource, RegistrationReceipt, SourceRegistration, SourceResolution,
    article_hash, registry_url, source_id,
)

RSS_COLLECTION_TIMEOUT = 60.0
SOURCE_RESPONSE_BYTES = 64 * 1024
SOURCE_REQUEST_DEADLINE = 30.0


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


class CoreSourceClient(Protocol):
    async def register_source(self, registration: SourceRegistration) -> SourceResolution: ...

    async def lookup_source(self, identifier: str) -> RegisteredSource: ...


class CoreSourceError(CoreClientError):
    """Write uncertainty is explicit; only constant details enter logs/responses."""

    def __init__(self, status_code: int, detail: str, *, kind: str,
                 write_outcome: str = "not_attempted", retry_after: str | None = None,
                 receipt: RegistrationReceipt | None = None):
        super().__init__(status_code, detail, retry_after)
        self.kind = kind
        self.write_outcome = write_outcome
        self.receipt = receipt


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

    async def _source_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            endpoint, key = self._endpoint_and_key(path)
        except CoreClientError:
            raise CoreSourceError(503, "Argus-Core source integration is not configured correctly.",
                                  kind="configuration") from None
        outcome = "unknown" if method == "POST" else "not_attempted"

        async def send(http):
            # Explicit Request avoids inherited client cookies/default auth headers.
            outbound = httpx.Request(method, endpoint, json=payload if method == "POST" else None,
                headers={"X-API-Key": key, "Accept": "application/json", "Accept-Encoding": "identity"},
                extensions={"timeout": httpx.Timeout(self._request_timeout).as_dict()})
            response = await http.send(outbound, stream=True, auth=None, follow_redirects=False)
            try:
                if response.status_code != 200:
                    status = response.status_code
                    retry = validated_retry_after(response.headers.get("Retry-After"))
                    failures = {
                        401: (502, "authentication", "Argus-Core authentication failed."),
                        403: (502, "permission", "Argus-Core source permission denied."),
                        429: (429, "throttled", "Source operation rate limit exceeded."),
                        503: (503, "unavailable", "Source registry unavailable."),
                        504: (504, "timeout", "Source operation timed out."),
                        422: (422, "rejected", "Argus-Core rejected the source request."),
                    }
                    if status == 404 and method == "GET":
                        raise CoreSourceError(404, "Core source not found.", kind="not_found")
                    code, kind, detail = failures.get(status, (502, "upstream", "Argus-Core source request failed."))
                    raise CoreSourceError(code, detail, kind=kind,
                        write_outcome="rejected" if method == "POST" and status in {401,403,422,429} else outcome,
                        retry_after=retry if status in {429,503} else None)
                if (response.headers.get("content-encoding", "identity").strip().lower() != "identity"
                        or response.headers.get("content-type", "").split(";",1)[0].strip().lower() != "application/json"):
                    raise ValueError
                length = response.headers.get("content-length")
                if length is not None and (not length.isascii() or not length.isdigit()
                                          or len(length) > 10 or int(length) > SOURCE_RESPONSE_BYTES):
                    raise ValueError
                body = bytearray()
                if response.is_stream_consumed:
                    # Trusted injected transports can supply already-buffered responses.
                    if len(response.content) > SOURCE_RESPONSE_BYTES:
                        raise ValueError
                    body.extend(response.content)
                else:
                    async for chunk in response.aiter_raw(chunk_size=16384):
                        if len(body) + len(chunk) > SOURCE_RESPONSE_BYTES:
                            raise ValueError
                        body.extend(chunk)
                if length is not None and len(body) != int(length):
                    raise ValueError
                def unique_pairs(pairs):
                    result = {}
                    for name, value in pairs:
                        if name in result:
                            raise ValueError
                        result[name] = value
                    return result
                data = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique_pairs)
                if not isinstance(data, dict):
                    raise ValueError
                return data
            finally:
                await asyncio.wait_for(response.aclose(), timeout=1)

        try:
            async with asyncio.timeout(min(self._request_timeout, SOURCE_REQUEST_DEADLINE)):
                if self._http_client is not None:
                    return await send(self._http_client)
                async with httpx.AsyncClient(trust_env=False) as http:
                    return await send(http)
        except (TimeoutError, httpx.TimeoutException):
            raise CoreSourceError(504, "Source operation timed out.", kind="timeout", write_outcome=outcome) from None
        except httpx.HTTPError:
            raise CoreSourceError(502, "Argus-Core source connection failed.", kind="transport", write_outcome=outcome) from None
        except (ValueError, TypeError, RecursionError):
            raise CoreSourceError(502, "Argus-Core returned an invalid source response.", kind="protocol", write_outcome=outcome) from None

    async def lookup_source(self, identifier: str) -> RegisteredSource:
        try:
            source_id(identifier)
        except (ValueError, TypeError):
            raise CoreSourceError(422, "Invalid Core source ID.", kind="input") from None
        data = await self._source_request("GET", "/api/v1/sources/" + identifier)
        try:
            # Core metadata is unstructured and globally visible today. Do not
            # propagate it into application evidence, outbound writes, or logs.
            if not isinstance(data.get("metadata"), dict):
                raise ValueError
            facts = RegisteredSource.model_validate({key: value for key, value in data.items() if key != "metadata"})
            if facts.id != identifier:
                raise ValueError
            return facts
        except (ValueError, TypeError):
            raise CoreSourceError(502, "Argus-Core returned an invalid source response.", kind="protocol") from None

    async def register_source(self, registration: SourceRegistration) -> SourceResolution:
        try:
            if not isinstance(registration, SourceRegistration):
                raise ValueError
            registration = SourceRegistration.model_validate(registration.model_dump())
            expected_url = registry_url(registration.observed_url)
            if len(expected_url) > 2048:
                raise ValueError
            fingerprint = article_hash(registration.article.text) if registration.article else None
        except (ValueError, TypeError):
            raise CoreSourceError(422, "Invalid public source registration.", kind="input") from None
        data = await self._source_request("POST", "/api/v1/sources/register", registration.wire_payload())
        try:
            receipt = RegistrationReceipt.model_validate(data)
        except (ValueError, TypeError):
            raise CoreSourceError(502, "Argus-Core returned an invalid registration receipt.",
                                  kind="protocol", write_outcome="unknown") from None

        def conflict(reason):
            raise CoreSourceError(409, "Source identity requires review; no evidence was merged.",
                kind=reason, write_outcome="acknowledged", receipt=receipt)

        if receipt.status == "created":
            if receipt.canonical_url != expected_url or receipt.content_hash != fingerprint:
                conflict("created_identity_disagreement")
        elif receipt.reason == "canonical_url" and receipt.canonical_url != expected_url:
            conflict("url_disagreement")
        elif receipt.reason == "content_hash" and fingerprint is None:
            conflict("unsupported_content_match")
        try:
            facts = await self.lookup_source(receipt.source_id)
        except CoreSourceError as exc:
            # POST was acknowledged; retry only this GET, never blindly replay it.
            exc.write_outcome = "acknowledged"
            exc.receipt = receipt
            raise
        if facts.canonical_url != receipt.canonical_url:
            conflict("receipt_lookup_disagreement")
        if receipt.status == "created" and facts.content_hash != fingerprint:
            conflict("created_identity_disagreement")
        if fingerprint is not None and facts.content_hash is not None and facts.content_hash != fingerprint:
            conflict("url_hash_disagreement")
        if receipt.reason == "content_hash" and facts.content_hash != fingerprint:
            conflict("unverified_content_match")
        cross_url = facts.canonical_url != expected_url
        return SourceResolution(observed_url=registration.observed_url, observed_publisher=registration.publisher,
            receipt=receipt, source=facts,
            match="created" if receipt.status == "created" else "cross_url_content_matched" if cross_url else "url_matched",
            content_verification="not_supplied" if fingerprint is None else "matched" if facts.content_hash == fingerprint else "unavailable",
            requires_review=cross_url)

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


def get_core_source_client() -> CoreSourceClient:
    return ArgusCoreClient(
        base_url=global_chess_settings.core_base_url,
        api_key=global_chess_settings.core_api_key,
        request_timeout=global_chess_settings.core_request_timeout,
    )

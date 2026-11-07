"""Read-only DOC article discovery, with no Global Chess or database imports."""

import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import anyio

from app.services.discovery.gdelt_admission import GDELTAdmission
from app.services.discovery.gdelt_contracts import (
    GDELTCandidate, GDELTFault, GDELTProvenance, GDELTRejectedEntry, GDELTRequest, GDELTResult, RetryPolicy,
)
from app.services.discovery.gdelt_fetch import GDELTFetcher, MAX_BYTES, SecureGDELTFetcher
from app.services.discovery.google_news import candidate_url
from app.services.article_fetch import UnsafeDestination

OPERATION_DEADLINE = 25.0
PARSER_SECONDS = 2.0
_capacity = anyio.CapacityLimiter(2)


def parse_gdelt(data: bytes, request: GDELTRequest) -> GDELTResult:
    if len(data) > MAX_BYTES:
        raise GDELTFault("parser_limit")
    deadline = time.monotonic()+PARSER_SECONDS
    def check():
        if time.monotonic() >= deadline:
            raise GDELTFault("parser_limit")
    try:
        raw = data.decode("utf-8")
        # Bound structure/string work before allocating the decoded object graph.
        depth = tokens = start = 0; quoted = escaped = False
        for index, char in enumerate(raw):
            if index % 8192 == 0:check()
            if quoted:
                if index-start > 65536:raise GDELTFault("parser_limit")
                if escaped:escaped=False
                elif char == "\\":escaped=True
                elif char == '"':quoted=False
            elif char == '"':
                quoted=True;start=index;tokens+=1
            elif char in "[{":
                depth+=1;tokens+=1
                if depth > 32:raise GDELTFault("parser_limit")
            elif char in "]}":depth-=1
            elif char in ",:":tokens+=1
            if tokens > 10000:raise GDELTFault("parser_limit")
        def pairs(values):
            if len(values) > 64:raise GDELTFault("parser_limit")
            result={}
            for key,value in values:
                if key in result:raise ValueError
                result[key]=value
            return result
        def integer(value):
            if len(value)>128:raise GDELTFault("parser_limit")
            return int(value)
        def floating(value):
            if len(value)>128:raise GDELTFault("parser_limit")
            parsed=float(value)
            if not math.isfinite(parsed):raise ValueError
            return parsed
        def invalid_constant(value):raise ValueError
        payload=json.loads(raw,object_pairs_hook=pairs,parse_int=integer,parse_float=floating,parse_constant=invalid_constant)
        check()
        if not isinstance(payload,dict) or not isinstance(payload.get("articles"),list) or payload.get("error"):
            raise ValueError
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise GDELTFault("malformed_response") from None
    articles=payload["articles"]
    if len(articles)>request.limit:
        raise GDELTFault("parser_limit")
    result=GDELTResult(status="success",limit=request.limit,window_hours=request.window_hours,
        received_count=len(articles),limit_reached=len(articles)==request.limit,fetched_at=datetime.now(timezone.utc))
    seen=set()
    for index, article in enumerate(articles):
        check();reason="invalid_record"
        try:
            if not isinstance(article,dict):raise ValueError
            title=article.get("title")
            if not isinstance(title,str) or not title.strip() or "\x00" in title:raise ValueError
            title=title.strip();reason="field_limit"
            if len(title)>1000:raise ValueError
            def optional(key,limit):
                value=article.get(key)
                if value is not None and (not isinstance(value,str) or len(value)>limit):raise ValueError
                return value
            original=optional("url",2048);reported=optional("domain",255);item_id=optional("id",2048)
            raw_date=optional("seendate",128);language=optional("language",64);country=optional("sourcecountry",128)
            mobile=optional("url_mobile",2048);image=optional("socialimage",2048)
            reason="invalid_url";url=candidate_url(original)
            warnings=[];seen_at=None
            if raw_date:
                try:
                    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z",raw_date):raise ValueError
                    seen_at=datetime.strptime(raw_date,"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:warnings.append("invalid_seen_date")
            def metadata_url(value,warning):
                if not value:return None
                try:return candidate_url(value)
                except (ValueError,TypeError,UnsafeDestination):warnings.append(warning);return None
            normalized_mobile=metadata_url(mobile,"invalid_mobile_url")
            normalized_image=metadata_url(image,"invalid_image_url")
            domain=urlsplit(url).hostname or ""
            candidate=GDELTCandidate(title=title,url=url,canonical_url=url,domain=domain,publisher=domain,
                provenance=GDELTProvenance(entry_index=index,original_url=original,reported_domain=reported,
                    provider_item_id=item_id,seen_raw=raw_date,seen_at=seen_at,language=language,source_country=country,
                    original_mobile_url=mobile,mobile_url=normalized_mobile,original_image_url=image,image_url=normalized_image,
                    warnings=warnings))
        except (ValueError,TypeError,OverflowError,UnsafeDestination):
            result.rejected_entries.append(GDELTRejectedEntry(entry_index=index,reason=reason));continue
        if url in seen:result.duplicate_observations+=1
        seen.add(url);result.candidates.append(candidate)
        if warnings:result.status="incomplete"
    result.invalid_candidates=len(result.rejected_entries)
    if result.invalid_candidates:result.status="incomplete"
    check()
    return result


class GDELTProvider:
    def __init__(self, admission: GDELTAdmission, fetcher: GDELTFetcher | None = None, *, enabled: bool = False):
        self.admission=admission
        self.fetcher=fetcher if fetcher is not None else SecureGDELTFetcher()
        self.enabled=enabled

    async def discover(self, request: GDELTRequest) -> GDELTResult:
        if not self.enabled:return GDELTFault("disabled").result(request)
        token=None;fault=None;result=None
        try:
            with anyio.fail_after(OPERATION_DEADLINE):
                # Reject worker saturation immediately, without creating a queue.
                try:_capacity.acquire_nowait()
                except anyio.WouldBlock:raise GDELTFault("busy","1") from None
                try:
                    token=await self.admission.acquire()
                    data=await self.fetcher.fetch(request)
                    result=await anyio.to_thread.run_sync(parse_gdelt,data,request)
                    await anyio.lowlevel.checkpoint()
                finally:_capacity.release()
        except GDELTFault as exc:fault=exc
        except TimeoutError:fault=GDELTFault("timeout")
        except Exception:fault=GDELTFault("upstream_error")
        finally:
            if token is not None:
                policy=(fault.policy or RetryPolicy(delay_seconds=6)) if fault else RetryPolicy()
                # Bounded shielded cleanup also runs on client cancellation.
                with anyio.move_on_after(3,shield=True) as cleanup:
                    try:
                        retry=await self.admission.finish(token,policy)
                        if fault and fault.status in {"throttled","upstream_error"}:
                            fault.retry_after=retry if not policy.hold else None
                    except Exception:fault=GDELTFault("storage_unavailable")
                if cleanup.cancel_called:fault=GDELTFault("storage_unavailable")
        return fault.result(request) if fault else result

import json
from unittest.mock import AsyncMock, Mock, patch

import httpcore
import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.clients.argus_core_client import ArgusCoreClient, CoreClientError, validated_retry_after
from app.global_chess_config import GlobalChessSettings

KEY = "test-only-core-key"
REQUEST = {"system_prompt": "System", "user_prompt": "Topic", "response_schema": {"type": "object"}}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Offline test attempted a real network request")))


def test_independent_settings_and_secret_repr(monkeypatch):
    monkeypatch.setenv("GLOBAL_CHESS_CORE_BASE_URL", "https://core.example")
    monkeypatch.setenv("GLOBAL_CHESS_CORE_API_KEY", KEY)
    monkeypatch.setenv("GLOBAL_CHESS_CORE_REQUEST_TIMEOUT", "12.5")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://unrelated.example")
    settings = GlobalChessSettings(_env_file=None)
    assert settings.core_base_url == "https://core.example"
    assert settings.core_request_timeout == 12.5
    assert settings.core_api_key.get_secret_value() == KEY
    assert KEY not in repr(settings) and KEY not in settings.model_dump_json()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 601])
def test_settings_reject_invalid_timeouts(timeout):
    with pytest.raises(ValidationError):
        GlobalChessSettings(_env_file=None, core_request_timeout=timeout)


@pytest.mark.asyncio
async def test_request_contract_and_envelope_adapter():
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url == "https://core.example/prefix/api/v1/ai/generate"
        assert request.headers["X-API-Key"] == KEY
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {**REQUEST, "temperature": 0.2, "timeout": 180.0}
        assert all(value == 12.5 for value in request.extensions["timeout"].values())
        return httpx.Response(200, json={"status": "success", "model": "actual-core-model", "result": '{"ok":true}'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.example/prefix/", api_key=SecretStr(KEY),
                                 request_timeout=12.5, http_client=http)
        result = await client.generate_structured(**REQUEST)
        assert result.content == '{"ok":true}' and result.model == "actual-core-model"
        assert not http.is_closed
    assert len(requests) == 1


@pytest.mark.parametrize("status,expected", [(401, 502), (403, 502), (429, 429), (503, 503),
                                                    (504, 504), (422, 502), (500, 502), (302, 502)])
@pytest.mark.asyncio
async def test_failures_are_explicit_sanitized_and_never_retried(status, expected, caplog):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "17", "Location": "https://attacker.example"},
                              json={"detail": KEY, "request_body": "private prompt"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
        client = ArgusCoreClient(base_url="https://core.example", api_key=KEY, http_client=http)
        with pytest.raises(CoreClientError) as error:
            await client.generate_structured(**REQUEST)
    assert error.value.status_code == expected
    assert error.value.retry_after == ("17" if status in {429, 503} else None)
    assert len(calls) == 1
    assert KEY not in str(error.value) and KEY not in caplog.text
    assert "private prompt" not in str(error.value)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("17", "17"), ("0", "0"), ("0005", "5"),
    ("Tue, 01 Jan 2030 00:00:00 GMT", "Tue, 01 Jan 2030 00:00:00 GMT"),
    (KEY, None), ("-1", None), ("1.5", None), ("999999999999", None), ("\r\nX-API-Key: secret", None),
])
def test_retry_after_is_validated(value, expected):
    assert validated_retry_after(value) == expected


@pytest.mark.parametrize("error_type,status", [(httpx.ReadTimeout, 504), (httpx.ConnectTimeout, 504),
                                              (httpx.ConnectError, 502), (httpx.RemoteProtocolError, 502)])
@pytest.mark.asyncio
async def test_transport_errors_hide_details_without_retries(error_type, status, caplog):
    calls = []
    def respond(request):
        calls.append(request)
        raise error_type("private diagnostic " + KEY, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.example", api_key=KEY, http_client=http)
        with pytest.raises(CoreClientError) as error:
            await client.generate_structured(**REQUEST)
    assert error.value.status_code == status and len(calls) == 1
    assert KEY not in str(error.value) and KEY not in caplog.text
    assert error.value.__suppress_context__


@pytest.mark.parametrize("payload", [None, [], {}, {"status": "failed"},
    {"status": "success", "model": None, "result": "{}"},
    {"status": "success", "model": "", "result": "{}"},
    {"status": "success", "model": "m" * 101, "result": "{}"},
    {"status": "success", "model": 123, "result": "{}"},
    {"status": "success", "model": "m", "result": {}},
    {"status": "success", "model": "m", "result": ""},
])
@pytest.mark.asyncio
async def test_malformed_envelopes_fail_without_inventing_a_model(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as http:
        with pytest.raises(CoreClientError, match="invalid inference response") as error:
            await ArgusCoreClient(base_url="https://core.example", api_key=KEY, http_client=http).generate_structured(**REQUEST)
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_invalid_http_json_is_sanitized():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=KEY))) as http:
        with pytest.raises(CoreClientError, match="invalid inference response"):
            await ArgusCoreClient(base_url="https://core.example", api_key=KEY, http_client=http).generate_structured(**REQUEST)


@pytest.mark.parametrize("base,key", [(None, KEY), ("https://core.example", None),
    ("ftp://core.example", KEY), ("https://user:secret@core.example", KEY),
    ("https://core.example?api_key=secret", KEY), ("https://core.example/#secret", KEY),
    ("https://core.example", "secret\r\nheader"), ("https://core.example", " ")])
@pytest.mark.asyncio
async def test_invalid_configuration_fails_before_sending(base, key):
    http = Mock()
    with pytest.raises(CoreClientError) as error:
        await ArgusCoreClient(base_url=base, api_key=key, http_client=http).generate_structured(**REQUEST)
    assert error.value.status_code == 503
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_owned_http_client_disables_environment_proxies_and_closes():
    http = AsyncMock()
    http.post.return_value = httpx.Response(200, json={"status": "success", "model": "core-model", "result": "{}"})
    with patch("app.clients.argus_core_client.httpx.AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value = http
        await ArgusCoreClient(base_url="https://core.example", api_key=KEY).generate_structured(**REQUEST)
        constructor.assert_called_once_with(trust_env=False)
        constructor.return_value.__aexit__.assert_awaited_once()
    assert http.post.call_args.kwargs["follow_redirects"] is False
    http.post.assert_awaited_once()

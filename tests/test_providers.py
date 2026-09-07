from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from banco_agil.providers import (
    AwesomeAPIProvider,
    GeminiProvider,
    ProviderError,
    ProviderUnavailableError,
    StructuredOutputError,
)


def quote_payload(code="USD"):
    return {
        code + "BRL": {
            "code": code,
            "codein": "BRL",
            "bid": "5.1249",
            "ask": "5.1258",
            "timestamp": "1788557375",
        }
    }


def test_quote_header_precision_and_actual_timestamp():
    def handler(request):
        assert request.headers["x-api-key"] == "test-token"
        assert "token" not in str(request.url)
        return httpx.Response(200, json=quote_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        quote = AwesomeAPIProvider(http_client=http, api_key="test-token").get_quote("USD")
    assert quote.bid == Decimal("5.1249")
    assert quote.timestamp == "2026-09-04T21:29:35+00:00"
    assert quote.warning is None


@pytest.mark.parametrize(
    "payload",
    [
        {"EURBRL": {"bid": "5", "ask": "6"}},
        {
            "USDBRL": {
                "code": "EUR",
                "codein": "BRL",
                "bid": "5",
                "ask": "6",
                "timestamp": "1788557375",
            }
        },
        {
            "USDBRL": {
                "code": "USD",
                "codein": "BRL",
                "bid": "NaN",
                "ask": "6",
                "timestamp": "1788557375",
            }
        },
        {"USDBRL": {"code": "USD", "codein": "BRL", "bid": "5", "ask": "6"}},
        [],
    ],
)
def test_invalid_quote_is_not_presented(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(ProviderError):
            AwesomeAPIProvider(http_client=http).get_quote("USD")


@pytest.mark.parametrize(
    "status,expected_calls", [(500, 2), (429, 2), (408, 2), (504, 2), (404, 1), (401, 1)]
)
def test_retry_only_transient_errors(status, expected_calls):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderError):
            AwesomeAPIProvider(http_client=http).get_quote("USD")
    assert len(calls) == expected_calls


def test_catalog_and_no_arbitrary_url():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/xml/available":
            return httpx.Response(200, text="<xml><CAD-BRL>Dólar canadense</CAD-BRL></xml>")
        return httpx.Response(200, json=quote_payload("CAD"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = AwesomeAPIProvider(http_client=http)
        assert provider.get_quote("CAD").currency == "CAD"
        assert len(calls) == 2
        with pytest.raises(ProviderError):
            provider.get_quote("https://evil.example")
        with pytest.raises(ProviderError):
            provider.get_quote("ZZZ")
        assert len(calls) == 2


class Output(BaseModel):
    currency: str


def test_gemini_missing_key_fails_clearly():
    with pytest.raises(ProviderUnavailableError):
        GeminiProvider().generate_structured("message", Output)


def test_gemini_calls_real_sdk_interface_with_schema():
    received = {}

    class Models:
        def generate_content(self, **kwargs):
            received.update(kwargs)
            return SimpleNamespace(parsed=None, text='{"currency":"USD"}')

    result = GeminiProvider(client=SimpleNamespace(models=Models())).generate_structured(
        "hello", Output
    )
    assert result.currency == "USD"
    assert received["contents"] == "hello"
    assert received["config"].response_json_schema == Output.model_json_schema()
    assert received["config"].response_schema is None
    assert received["config"].response_mime_type == "application/json"


def test_gemini_invalid_parsed_mapping_has_safe_error():
    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(parsed={"wrong": "value"})

    with pytest.raises(StructuredOutputError):
        GeminiProvider(client=SimpleNamespace(models=Models())).generate_structured("hello", Output)


@pytest.mark.parametrize("extra,invalid", [({}, False), ({"cpf": "not-allowed"}, True)])
def test_full_agent_schema_through_real_sdk_transport(extra, invalid):
    import json

    from google import genai
    from google.genai import types

    from banco_agil.application import AgentDecision

    def handler(request):
        body = json.loads(request.content)
        config = body["generationConfig"]
        assert "responseSchema" not in config
        schema = config["responseJsonSchema"]
        assert schema["additionalProperties"] is False
        assert schema["$defs"]["InterviewSlots"]["additionalProperties"] is False
        assert "additional_properties" not in json.dumps(schema)
        answer = {"intent": "credito", "action": "consultar_limite", **extra}
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": json.dumps(answer)}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        sdk = genai.Client(api_key="test-key", http_options=types.HttpOptions(httpx_client=http))
        provider = GeminiProvider(client=sdk)
        if invalid:
            with pytest.raises(StructuredOutputError):
                provider.generate_structured("Qual meu limite?", AgentDecision)
        else:
            result = provider.generate_structured("Qual meu limite?", AgentDecision)
            assert result.action == "consultar_limite"


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "formato"),
        (401, "chave"),
        (403, "permissão"),
        (404, "modelo"),
        (429, "quota"),
        (503, "sobrecarregado"),
        (504, "demorou"),
    ],
)
def test_gemini_error_messages_distinguish_cause_without_leaking_details(status, expected):
    from google.genai import errors

    class Models:
        def generate_content(self, **kwargs):
            error = errors.ClientError if status < 500 else errors.ServerError
            raise error(status, {"error": {"message": "private-value", "code": status}})

    with pytest.raises(ProviderError) as caught:
        GeminiProvider(client=SimpleNamespace(models=Models())).generate_structured("hello", Output)
    assert expected in str(caught.value)
    assert "private-value" not in str(caught.value)

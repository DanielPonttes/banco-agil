"""Typed external integrations with bounded requests and safe errors."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TypeVar
from xml.etree import ElementTree

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Safe error message, excluding HTTP URLs, secrets and personal data."""


class ProviderUnavailableError(ProviderError):
    pass


class StructuredOutputError(ProviderError):
    pass


class GeminiProvider:
    def __init__(self, api_key=None, model="gemini-3.5-flash-lite", client=None):
        self.api_key = api_key
        self.model = model
        self._client = client

    @property
    def available(self):
        return bool(self.api_key or self._client)

    def generate_structured(self, prompt: str, response_model: type[T], *, agent=None) -> T:
        if not self.available:
            raise ProviderUnavailableError(
                "Gemini não configurado. Preencha GEMINI_API_KEY no arquivo .env local."
            )
        try:
            if self._client is None:
                self._client = genai.Client(
                    api_key=self.api_key,
                    http_options=types.HttpOptions(
                        timeout=20000, retry_options=types.HttpRetryOptions(attempts=1)
                    ),
                )
            result = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=response_model.model_json_schema(),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    temperature=0,
                ),
            )
        except errors.APIError as exc:
            messages = {
                400: "O Gemini rejeitou o formato da solicitação. A integração precisa ser verificada.",
                401: "O Gemini não autorizou a chave configurada. Verifique GEMINI_API_KEY.",
                403: "A chave configurada não tem permissão para acessar o Gemini.",
                404: "O modelo configurado não está disponível. Verifique GEMINI_MODEL.",
                429: "O Gemini atingiu o limite de requisições ou quota. Tente novamente mais tarde.",
                503: "O Gemini está temporariamente sobrecarregado. Tente novamente mais tarde.",
                504: "O Gemini demorou demais para responder. Tente novamente.",
            }
            log.warning("gemini_request_failed http_status=%s", exc.code)
            raise ProviderError(
                messages.get(exc.code, "O serviço Gemini não concluiu a solicitação.")
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("O Gemini demorou demais para responder. Tente novamente.") from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "Não foi possível conectar ao Gemini. Verifique a conexão."
            ) from exc
        except Exception as exc:
            log.warning("gemini_request_failed category=%s", type(exc).__name__)
            raise ProviderError("A integração com o Gemini não concluiu a solicitação.") from exc
        try:
            parsed = getattr(result, "parsed", None)
            if isinstance(parsed, response_model):
                return parsed
            if parsed is not None:
                return response_model.model_validate(parsed)
            return response_model.model_validate_json(result.text)
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            raise StructuredOutputError(
                "O Gemini retornou dados inválidos. Tente novamente."
            ) from exc


@dataclass(frozen=True)
class CurrencyQuote:
    currency: str
    bid: Decimal
    ask: Decimal
    timestamp: str
    source: str = "AwesomeAPI"
    warning: str | None = None


class AwesomeAPIProvider:
    BASE = "https://economia.awesomeapi.com.br"
    DEFAULT_CATALOG = {"USD", "EUR", "GBP"}

    def __init__(self, *, api_key=None, http_client=None, catalog=None):
        self.api_key = api_key
        self.http = http_client or httpx
        self.catalog = set(catalog) if catalog is not None else None

    @classmethod
    def from_settings(cls, settings):
        return cls(api_key=settings.awesomeapi_key)

    def _get(self, path):
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        for attempt in range(2):
            try:
                response = self.http.get(self.BASE + path, headers=headers, timeout=5.0)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                retryable = (
                    exc.response.status_code in (408, 429) or exc.response.status_code >= 500
                )
                if attempt == 0 and retryable:
                    continue
                raise ProviderError(
                    "A fonte de câmbio está indisponível para esta consulta."
                ) from exc
            except httpx.RequestError as exc:
                if attempt == 0:
                    continue
                raise ProviderError(
                    "A consulta de câmbio falhou. Tente novamente mais tarde."
                ) from exc

    def _code(self, currency):
        aliases = {"DÓLAR": "USD", "DOLAR": "USD", "EURO": "EUR", "LIBRA": "GBP"}
        raw = str(currency).strip().upper()
        code = aliases.get(raw, raw)
        if not re.fullmatch("[A-Z]{3}", code) or code == "BRL":
            raise ProviderError("Informe uma moeda estrangeira válida, por exemplo USD ou EUR.")
        if code not in self.DEFAULT_CATALOG:
            if self.catalog is None:
                try:
                    root = ElementTree.fromstring(self._get("/xml/available").text)
                    self.catalog = {
                        node.tag[:-4] for node in root if re.fullmatch("[A-Z]{3}-BRL", node.tag)
                    }
                except (ElementTree.ParseError, ValueError) as exc:
                    raise ProviderError("Não foi possível validar o catálogo de moedas.") from exc
            if code not in self.catalog:
                raise ProviderError("Esse par de moedas não está disponível na fonte.")
        return code

    def get_quote(self, currency):
        code = self._code(currency)
        try:
            payload = self._get(f"/json/last/{code}-BRL").json()
            entry = payload[f"{code}BRL"]
            if entry.get("code") != code or entry.get("codein") != "BRL":
                raise ValueError("wrong pair")
            bid, ask = Decimal(str(entry["bid"])), Decimal(str(entry["ask"]))
            if not bid.is_finite() or not ask.is_finite() or min(bid, ask) <= 0:
                raise ValueError("invalid price")
            timestamp = datetime.fromtimestamp(int(entry["timestamp"]), timezone.utc)
            if timestamp.year < 2000:
                raise ValueError("invalid timestamp")
        except ProviderError:
            raise
        except (
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
            OverflowError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise ProviderError("A fonte retornou uma cotação inválida.") from exc
        return CurrencyQuote(
            code,
            bid,
            ask,
            timestamp.isoformat(),
            warning=None
            if self.api_key
            else "Consulta sem chave: a fonte pode usar cache de até 1 minuto.",
        )

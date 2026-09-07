"""Opt-in checks against real services; excluded from the default test suite."""

import os

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from banco_agil.providers import AwesomeAPIProvider, GeminiProvider

pytestmark = pytest.mark.live


class CurrencyIntent(BaseModel):
    currency: str


def test_gemini_real_structured_output():
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("Configure GEMINI_API_KEY locally to run the Gemini smoke test")
    provider = GeminiProvider(api_key=api_key, model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
    result = provider.generate_structured(
        "Extraia o código ISO da moeda na pergunta: Qual a cotação do dólar americano?",
        CurrencyIntent,
        agent="cambio",
    )
    assert result.currency == "USD"


def test_awesomeapi_real_quote():
    quote = AwesomeAPIProvider().get_quote("USD")
    assert quote.currency == "USD"
    assert quote.bid > 0
    assert quote.ask > 0
    assert quote.timestamp
    assert quote.source == "AwesomeAPI"

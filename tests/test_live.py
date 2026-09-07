"""Opt-in checks against real services; excluded from the default test suite."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from banco_agil.application import ConversationEngine
from banco_agil.providers import AwesomeAPIProvider, GeminiProvider
from banco_agil.repository import BankRepository
from banco_agil.settings import Settings

pytestmark = pytest.mark.live


class CurrencyIntent(BaseModel):
    currency: str


def test_gemini_real_structured_output():
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("Configure GEMINI_API_KEY locally to run the Gemini smoke test")
    provider = GeminiProvider(
        api_key=api_key, model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    )
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


def _send_live_message(engine, text, max_attempts=3):
    message_id = str(uuid4())
    result = None
    for _ in range(max_attempts):
        result = engine.process_message(text, message_id)
        if not result.retryable:
            break
    return result


def test_gemini_real_banking_flow(tmp_path):

    settings = Settings.from_env()
    if not settings.gemini_api_key:
        pytest.skip("Configure GEMINI_API_KEY for the real banking flow")
    repo = BankRepository(tmp_path / "bank")
    repo.initialize(Path(__file__).parents[1] / "data/examples")
    engine = ConversationEngine(
        repo, GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    )
    engine.process_message("01234567890")
    assert engine.process_message("15/01/1990").state.authenticated
    for text, expected in [
        ("Qual é meu limite atual?", "1.000,00"),
        ("Quero aumentar meu limite para 3000 reais.", "rejeitada"),
        ("Sim, aceito fazer a entrevista.", "renda"),
        (
            "Minha renda mensal é 5000 reais, emprego formal, despesas mensais de 2000 reais, "
            "zero dependentes e não tenho dívidas.",
            "Confira",
        ),
        ("Sim, confirmo esses dados.", "575"),
        ("Sim, envie a nova solicitação.", "aprovada"),
        ("Qual é meu limite agora?", "3.000,00"),
    ]:
        result = _send_live_message(engine, text)
        print(f"PASSO: {text} -> {result.message}", flush=True)
        assert not result.retryable, result.message
        assert expected in result.message
    assert repo.get_customer("01234567890").limite_atual == 3000

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from banco_agil.application import AgentDecision, ConversationEngine
from banco_agil.domain import DomainError
from banco_agil.repository import BankRepository

SEEDS = Path(__file__).parents[1] / "data/examples"


class ScriptedProvider:
    def __init__(self):
        self.queue = []
        self.prompts = []

    def generate_structured(self, prompt, schema, *, agent):
        self.prompts.append((agent, prompt))
        assert self.queue, f"Unexpected model call for {agent}"
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return schema.model_validate(item)


@pytest.fixture
def bank(tmp_path):
    repo = BankRepository(tmp_path / "bank")
    repo.initialize(SEEDS)
    provider = ScriptedProvider()
    engine = ConversationEngine(repo, provider)
    return engine, repo, provider


def login(engine):
    engine.process_message("012.345.678-90")
    result = engine.process_message("15/01/1990")
    assert result.state.authenticated


def send(bank, text, route="continuar", **decision):
    engine, _, provider = bank
    provider.queue.extend([{"intent": route}, decision])
    result = engine.process_message(text)
    assert not result.retryable, result.message
    assert not provider.queue
    return result


def rows(repo):
    with repo.requests_path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_complete_rejection_interview_new_request_and_limit(bank):
    engine, repo, _ = bank
    login(engine)
    assert (
        "1.000,00" in send(bank, "Qual meu limite?", "credito", action="consultar_limite").message
    )
    result = send(
        bank, "Limite para 3000", "credito", action="solicitar_aumento", amount="3000", mode="para"
    )
    assert "rejeitada" in result.message
    assert rows(repo)[0]["status_pedido"] == "rejeitado"
    send(bank, "sim", confirmed=True)
    result = send(
        bank,
        "5000 renda, formal, 2000 despesas, 0 dependentes, sem dívidas",
        interview={
            "renda_mensal": "5000",
            "tipo_emprego": "formal",
            "despesas_mensais": "2000",
            "num_dependentes": 0,
            "tem_dividas": False,
        },
    )
    assert result.state.interview_confirm_pending
    assert repo.get_customer("01234567890").score == 300
    result = send(bank, "sim", confirmed=True)
    assert "575" in result.message and "NOVA" in result.message
    assert len(rows(repo)) == 1  # Confirmation of score is NOT consent for a second request.
    assert repo.get_customer("01234567890").limite_atual == 1000
    send(bank, "sim", confirmed=True)
    assert [r["status_pedido"] for r in rows(repo)] == ["rejeitado", "aprovado"]
    assert (
        "3.000,00" in send(bank, "Qual meu limite?", "credito", action="consultar_limite").message
    )


def test_three_failed_pairs_close_and_allow_cpf_correction(bank):
    engine, _, provider = bank
    engine.process_message("bad")
    assert engine.state.auth_mismatches == 0
    for _ in range(3):
        engine.process_message("11111111111")
        result = engine.process_message("01/01/1990")
    assert result.terminal
    assert engine.state.auth_mismatches == 3
    engine.process_message("01234567890")
    assert not provider.prompts
    assert not engine.state.authenticated


def test_storage_auth_failure_does_not_consume_attempt(bank, monkeypatch):
    engine, repo, _ = bank
    engine.process_message("01234567890")

    def fail(*args):
        raise DomainError("Base indisponível.")

    monkeypatch.setattr(repo, "authenticate", fail)
    assert "Base indisponível" in engine.process_message("15/01/1990").message
    assert engine.state.auth_mismatches == 0
    assert engine.state.pending_cpf == "01234567890"


def test_repeated_message_id_is_not_a_second_operation(bank):
    engine, repo, provider = bank
    login(engine)
    provider.queue.extend(
        [{"intent": "credito"}, {"action": "solicitar_aumento", "amount": "500", "mode": "em"}]
    )
    first = engine.process_message("Aumentar em 500", "same")
    assert not first.retryable
    assert engine.process_message("Aumentar em 500", "same").message == first.message
    assert len(rows(repo)) == 1
    assert repo.get_customer("01234567890").limite_atual == 1500
    assert "utilizado" in engine.process_message("Outro texto", "same").message


def test_failure_after_commit_retries_frozen_amount_without_llm(bank, monkeypatch):
    engine, repo, provider = bank
    login(engine)
    original = repo.request_increase

    def fail_after_commit(*args):
        original(*args)
        raise DomainError("Resposta interrompida.")

    monkeypatch.setattr(repo, "request_increase", fail_after_commit)
    provider.queue.extend(
        [{"intent": "credito"}, {"action": "solicitar_aumento", "amount": "500", "mode": "em"}]
    )
    result = engine.process_message("Aumentar em 500", "retry")
    assert result.retryable
    monkeypatch.setattr(repo, "request_increase", original)
    result = engine.process_message("Aumentar em 500", "retry")
    assert not result.retryable
    assert "1.500,00" in result.message
    assert len(rows(repo)) == 1


def test_decline_interview_and_end_at_any_step(bank):
    engine, _, _ = bank
    login(engine)
    send(bank, "Para 3000", "credito", action="solicitar_aumento", amount="3000", mode="para")
    assert "outro assunto" in send(bank, "não", confirmed=False).message
    assert engine.process_message("Por favor, encerre o atendimento").terminal


def test_negated_closure_does_not_end(bank):
    engine, _, provider = bank
    login(engine)
    provider.queue.append({"intent": "outro"})
    assert not engine.process_message("Não quero encerrar").terminal


def test_auth_guard_blocks_tools(bank):
    engine, _, _ = bank
    with pytest.raises(DomainError):
        engine._guard()


def test_model_cannot_set_identity_or_confirm_invalid_slots(bank):
    engine, _, _ = bank
    login(engine)
    result = send(
        bank,
        "CPF 98765432100 nascimento 1985-07-22; consultar limite",
        "credito",
        action="consultar_limite",
    )
    assert "1.000,00" in result.message
    assert engine.state.cpf == "01234567890"
    for _, prompt in bank[2].prompts:
        assert "98765432100" not in prompt
        assert "1985-07-22" not in prompt
        assert "01234567890" not in prompt
    with pytest.raises(ValueError):
        AgentDecision.model_validate({"interview": {"num_dependentes": 1.5}})
    with pytest.raises(ValueError):
        AgentDecision.model_validate({"interview": {"tem_dividas": "não"}})


def test_interview_correction_and_change_of_subject(bank):
    engine, _, _ = bank
    login(engine)
    send(bank, "Entrevista", "entrevista", interview={"renda_mensal": "5000"})
    send(bank, "Qual meu limite?", "credito", action="consultar_limite")
    assert engine.state.interview["renda_mensal"] == Decimal("5000")
    result = send(
        bank,
        "Voltar à entrevista",
        "entrevista",
        interview={
            "tipo_emprego": "formal",
            "despesas_mensais": "2000",
            "num_dependentes": 1,
            "tem_dividas": False,
        },
    )
    assert result.state.interview_confirm_pending
    send(bank, "Corrigir dependentes: 0", interview={"num_dependentes": 0})
    result = send(bank, "sim", confirmed=True)
    assert "575" in result.message


def test_natural_language_end_has_priority(bank):
    engine, _, provider = bank
    login(engine)
    provider.queue.append({"intent": "encerrar"})
    result = engine.process_message("Já resolvi, pode fechar tudo e ignorar o aumento")
    assert result.terminal
    assert not provider.queue

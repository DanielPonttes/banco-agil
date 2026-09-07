import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

import banco_agil.repository as repository_module
from banco_agil.domain import DomainError, Interview
from banco_agil.repository import BankRepository

SEEDS = Path(__file__).parents[1] / "data" / "examples"
CPF = "01234567890"
INTERVIEW = Interview(Decimal("5000"), "formal", Decimal("2000"), 0, False)


def make_repo(tmp_path):
    repo = BankRepository(tmp_path / "runtime-data")
    repo.initialize(SEEDS)
    return repo


def test_initialize_and_demo_flow_preserve_cpf_and_snapshot(tmp_path):
    repo = make_repo(tmp_path)
    customer = repo.authenticate("012.345.678-90", "15/01/1990")
    assert customer is not None
    assert customer.cpf_cliente == CPF
    assert repo.update_score(CPF, INTERVIEW, str(uuid4())) == 575
    result = repo.request_increase(CPF, Decimal("3000"), str(uuid4()))
    assert result.status_pedido == "aprovado"
    assert result.limite_atual == Decimal("1000.00")
    assert repo.get_customer(CPF).limite_atual == Decimal("3000.00")


def test_uuid_replay_is_durable_and_collision_is_rejected(tmp_path):
    repo = make_repo(tmp_path)
    operation_id = str(uuid4())
    first = repo.request_increase(CPF, Decimal("1500"), operation_id)
    assert repo.get_customer(CPF).limite_atual == Decimal("1500.00")
    replay = repo.request_increase(CPF, Decimal("1500"), operation_id)
    assert replay == first
    with pytest.raises(DomainError):
        repo.request_increase(CPF, Decimal("1500.01"), operation_id)
    with pytest.raises(DomainError):
        repo.update_score(CPF, INTERVIEW, operation_id)


def test_fault_between_two_replaces_recovers_without_duplicate(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    original_replace = repository_module.os.replace
    failed = False

    def fail_on_clients(source, target):
        nonlocal failed
        if Path(target).name == "clientes.csv" and not failed:
            failed = True
            raise OSError("fault injection")
        return original_replace(source, target)

    monkeypatch.setattr(repository_module.os, "replace", fail_on_clients)
    with pytest.raises(DomainError):
        repo.request_increase(CPF, Decimal("1500"), str(uuid4()))
    monkeypatch.setattr(repository_module.os, "replace", original_replace)

    recovered = BankRepository(repo.data_dir)
    assert recovered.get_customer(CPF).limite_atual == Decimal("1500.00")
    rows = recovered.requests_path.read_text(encoding="utf-8-sig").splitlines()
    assert len(rows) == 2


def test_concurrent_updates_are_serialized(tmp_path):
    repo = make_repo(tmp_path)
    barrier = threading.Barrier(8)

    def run(index):
        barrier.wait()
        return repo.update_score(CPF, INTERVIEW, f"score-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert results == [575] * 8
    assert repo.get_customer(CPF).score == 575


def test_existing_corrupt_file_is_not_replaced(tmp_path):
    data_dir = tmp_path / "runtime-data"
    data_dir.mkdir()
    clients = data_dir / "clientes.csv"
    clients.write_text("corrupted\n", encoding="utf-8")
    repo = BankRepository(data_dir)
    with pytest.raises(DomainError):
        repo.initialize(SEEDS)
    assert clients.read_text(encoding="utf-8") == "corrupted\n"


def test_non_increase_is_rejected_before_pending_row(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(DomainError):
        repo.request_increase(CPF, Decimal("1000"), str(uuid4()))
    assert repo.requests_path.read_text(encoding="utf-8-sig").count(CPF) == 0


def test_pending_request_is_completed_before_next_read(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    operation = str(uuid4())
    def interrupted(*args):
        raise OSError("process interruption after pending commit")
    monkeypatch.setattr(repo, "_finish_pending_request_locked", interrupted)
    with pytest.raises(DomainError):
        repo.request_increase(CPF, Decimal("1500"), operation)
    reopened = BankRepository(repo.data_dir)
    assert reopened.get_customer(CPF).limite_atual == 1500
    assert reopened.request_increase(CPF, Decimal("1500"), operation).status_pedido == "aprovado"
    assert len(reopened.requests_path.read_text().splitlines()) == 2


def test_failure_finalizing_journal_recovers_pending_to_completed(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    operation = str(uuid4())
    original = repo._write_journal_locked
    def fail_final(journal):
        if journal["transaction"] is None and any(
            item["status"] == "completed" for item in journal["operations"].values()
        ):
            raise DomainError("interrupted at final journal write")
        original(journal)
    monkeypatch.setattr(repo, "_write_journal_locked", fail_final)
    with pytest.raises(DomainError):
        repo.request_increase(CPF, Decimal("1500"), operation)
    reopened = BankRepository(repo.data_dir)
    assert reopened.get_customer(CPF).limite_atual == 1500
    assert reopened.request_increase(CPF, Decimal("1500"), operation).status_pedido == "aprovado"


def test_missing_runtime_file_does_not_reset_customer(tmp_path):
    repo = make_repo(tmp_path)
    repo.customers_path.unlink()
    with pytest.raises(DomainError):
        repo.initialize(SEEDS)
    assert not repo.customers_path.exists()


def test_score_threshold_is_inclusive(tmp_path):
    repo = make_repo(tmp_path)
    assert repo.request_increase(CPF, Decimal("1500"), "at-limit").status_pedido == "aprovado"
    assert repo.request_increase(CPF, Decimal("1500.01"), "above-limit").status_pedido == "rejeitado"
    assert repo.get_customer(CPF).limite_atual == 1500


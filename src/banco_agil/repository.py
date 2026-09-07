"""CSV-backed persistence for the Banco Ágil domain.

The repository uses a small write-ahead journal for multi-file updates. CSV
files remain human-readable, while the journal makes a process interruption
between the two ``os.replace`` calls recoverable on the next repository access.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

try:
    from filelock import FileLock
    from filelock import Timeout as FileLockTimeout
except ImportError:  # pragma: no cover - the project declares filelock
    FileLock = None  # type: ignore[assignment,misc]
    FileLockTimeout = TimeoutError  # type: ignore[assignment,misc]

from .domain import (
    CreditResult,
    Customer,
    DomainError,
    Interview,
    calculate_score,
    normalize_birth_date,
    normalize_cpf,
    parse_money,
)

_CUSTOMERS = "clientes.csv"
_SCORE_LIMITS = "score_limite.csv"
_REQUESTS = "solicitacoes_aumento_limite.csv"
_FILES = (_CUSTOMERS, _SCORE_LIMITS, _REQUESTS)
_CUSTOMER_FIELDS = ("cpf_cliente", "nome", "data_nascimento", "limite_atual", "score")
_SCORE_FIELDS = ("score_minimo", "limite_maximo")
_REQUEST_FIELDS = (
    "cpf_cliente",
    "data_hora_solicitacao",
    "limite_atual",
    "novo_limite_solicitado",
    "status_pedido",
)
_ALLOWED_STATUSES = {"pendente", "aprovado", "rejeitado"}
_JOURNAL_VERSION = 1


class BankRepository:
    """Persist and retrieve banking data under ``data_dir``."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._thread_lock = threading.RLock()
        self._lock_path = self.data_dir / ".banco_agil.lock"
        self._journal_path = self.data_dir / "runtime" / "journal.json"

    @property
    def customers_path(self) -> Path:
        return self.data_dir / _CUSTOMERS

    @property
    def score_limits_path(self) -> Path:
        return self.data_dir / _SCORE_LIMITS

    @property
    def requests_path(self) -> Path:
        return self.data_dir / _REQUESTS

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # The in-process lock protects instances in the same interpreter; the
        # file lock protects a second Python process using the same data_dir.
        with self._thread_lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                lock = FileLock(str(self._lock_path)) if FileLock is not None else None
                if lock is None:
                    yield
                else:
                    try:
                        with lock.acquire(timeout=30):
                            yield
                    except FileLockTimeout as exc:
                        raise DomainError("Não foi possível acessar a base bancária.") from exc
            except DomainError:
                raise
            except OSError as exc:
                raise DomainError("Não foi possível acessar a base bancária.") from exc

    def initialize(self, seed_dir: Path) -> None:
        """Copy only missing seed files, then validate and recover the runtime."""

        seed_dir = Path(seed_dir)
        with self._locked():
            self._recover_journal_locked()
            if not seed_dir.is_dir():
                raise DomainError("Diretório de dados de exemplo inválido.")

            # Validate every existing runtime file before touching missing
            # files.  A malformed existing file must never be hidden by a seed.
            existing = {name: self.data_dir / name for name in _FILES}
            for name, path in existing.items():
                if path.exists() and not path.is_file():
                    raise DomainError("Base bancária inválida.")
            if self._all_present(existing):
                self._load_all_locked()
                return

            if any(path.exists() for path in existing.values()):
                raise DomainError("Base bancária incompleta. Restaure os arquivos ausentes.")

            seed_contents: dict[str, bytes] = {}
            for name in _FILES:
                source = seed_dir / name
                if not source.is_file():
                    raise DomainError("Dados de exemplo incompletos.")
                try:
                    contents = source.read_bytes()
                except OSError as exc:
                    raise DomainError("Não foi possível ler os dados de exemplo.") from exc
                seed_contents[name] = contents

            # Parse runtime files and seed candidates separately first.  This
            # prevents a bad seed from creating a corrupt partial runtime.
            for name, path in existing.items():
                if path.exists():
                    self._parse_file_bytes(name, self._read_file_bytes(path))
            seed_parsed = {
                name: self._parse_file_bytes(name, contents)
                for name, contents in seed_contents.items()
            }
            self._validate_cross_references(
                seed_parsed[_CUSTOMERS], seed_parsed[_SCORE_LIMITS], seed_parsed[_REQUESTS]
            )

            for name, path in existing.items():
                if path.exists():
                    continue
                try:
                    # xb gives a second line of defence against another
                    # process racing this process while initializing.
                    with path.open("xb") as destination:
                        destination.write(seed_contents[name])
                        destination.flush()
                        os.fsync(destination.fileno())
                except FileExistsError:
                    # The other initializer won; its file is validated below.
                    pass
                except OSError as exc:
                    raise DomainError("Não foi possível inicializar a base bancária.") from exc
            self._load_all_locked()

    def authenticate(self, cpf: str, birth_date: str) -> Customer | None:
        normalized_cpf = normalize_cpf(cpf)
        normalized_birth = normalize_birth_date(birth_date)
        with self._locked():
            self._recover_journal_locked()
            customers, _, _ = self._load_all_locked()
            customer = customers.get(normalized_cpf)
            if customer is None or customer.data_nascimento != normalized_birth:
                return None
            return customer

    def get_customer(self, cpf: str) -> Customer:
        normalized_cpf = normalize_cpf(cpf)
        with self._locked():
            self._recover_journal_locked()
            customers, _, _ = self._load_all_locked()
            customer = customers.get(normalized_cpf)
            if customer is None:
                raise DomainError("Cliente não encontrado.")
            return customer

    def request_increase(self, cpf: str, requested: Any, operation_id: str) -> CreditResult:
        normalized_cpf = normalize_cpf(cpf)
        requested_money = parse_money(requested)
        normalized_operation = self._normalize_operation_id(operation_id)
        fingerprint = self._fingerprint(
            "request_increase",
            {"cpf": normalized_cpf, "requested": self._money_text(requested_money)},
        )

        with self._locked():
            self._recover_journal_locked()
            journal = self._read_journal_locked()
            existing_operation = self._check_operation(journal, normalized_operation, fingerprint)
            if existing_operation is not None:
                if existing_operation["status"] == "completed":
                    return self._credit_from_record(existing_operation)
                if existing_operation["status"] != "pending":
                    raise DomainError("Operação inválida.")
                return self._finish_pending_request_locked(
                    journal, existing_operation, requested_money
                )

            customers, score_limits, requests = self._load_all_locked()
            customer = customers.get(normalized_cpf)
            if customer is None:
                raise DomainError("Cliente não encontrado.")
            if requested_money <= Decimal("0") or requested_money <= customer.limite_atual:
                raise DomainError("Novo limite deve ser maior que o limite atual.")
            timestamp = self._utc_now()
            requests_with_pending = [dict(row) for row in requests]
            requests_with_pending.append(
                {
                    "cpf_cliente": normalized_cpf,
                    "data_hora_solicitacao": timestamp,
                    "limite_atual": self._money_text(customer.limite_atual),
                    "novo_limite_solicitado": self._money_text(requested_money),
                    "status_pedido": "pendente",
                }
            )
            pending_record = {
                "operation_id": normalized_operation,
                "kind": "request_increase",
                "fingerprint": fingerprint,
                "status": "pending",
                "cpf": normalized_cpf,
                "requested": self._money_text(requested_money),
                "current_limit": self._money_text(customer.limite_atual),
                "timestamp": timestamp,
            }
            # Persist the pending request before making the business decision.
            self._atomic_commit_locked(
                {_REQUESTS: self._serialize_requests(requests_with_pending)}, pending_record
            )
            return self._finish_pending_request_locked(
                self._read_journal_locked(), pending_record, requested_money
            )

    def update_score(self, cpf: str, interview: Interview, operation_id: str) -> int:
        normalized_cpf = normalize_cpf(cpf)
        normalized_operation = self._normalize_operation_id(operation_id)
        score = calculate_score(interview)
        income = parse_money(interview.renda_mensal)
        expenses = parse_money(interview.despesas_mensais)
        fingerprint = self._fingerprint(
            "update_score",
            {
                "cpf": normalized_cpf,
                "renda_mensal": self._money_text(income),
                "tipo_emprego": interview.tipo_emprego,
                "despesas_mensais": self._money_text(expenses),
                "num_dependentes": interview.num_dependentes,
                "tem_dividas": interview.tem_dividas,
            },
        )

        with self._locked():
            self._recover_journal_locked()
            journal = self._read_journal_locked()
            existing_operation = self._check_operation(journal, normalized_operation, fingerprint)
            if existing_operation is not None:
                if existing_operation["status"] != "completed":
                    raise DomainError("Operação pendente requer nova tentativa.")
                try:
                    return int(existing_operation["result"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainError("Diário de operações inválido.") from exc

            customers, _, _ = self._load_all_locked()
            customer = customers.get(normalized_cpf)
            if customer is None:
                raise DomainError("Cliente não encontrado.")
            updated_rows = []
            for row in self._read_csv(_CUSTOMERS):
                if row["cpf_cliente"] == normalized_cpf:
                    changed = dict(row)
                    changed["score"] = str(score)
                    updated_rows.append(changed)
                else:
                    updated_rows.append(dict(row))
            record = {
                "operation_id": normalized_operation,
                "kind": "update_score",
                "fingerprint": fingerprint,
                "status": "completed",
                "result": score,
            }
            self._atomic_commit_locked(
                {_CUSTOMERS: self._serialize_customers(updated_rows)}, record
            )
            return score

    # ---- validation and CSV -------------------------------------------------

    def _all_present(self, paths: dict[str, Path]) -> bool:
        return all(path.exists() and path.is_file() for path in paths.values())

    def _load_all_locked(
        self,
    ) -> tuple[dict[str, Customer], list[tuple[int, Any]], list[dict[str, str]]]:
        parsed_customers = self._parse_file_bytes(
            _CUSTOMERS, self._read_file_bytes(self.customers_path)
        )
        parsed_limits = self._parse_file_bytes(
            _SCORE_LIMITS, self._read_file_bytes(self.score_limits_path)
        )
        parsed_requests = self._parse_file_bytes(
            _REQUESTS, self._read_file_bytes(self.requests_path)
        )
        self._validate_cross_references(parsed_customers, parsed_limits, parsed_requests)

        customers: dict[str, Customer] = {}
        for row in parsed_customers:
            customer = Customer(
                cpf_cliente=normalize_cpf(row["cpf_cliente"]),
                nome=row["nome"].strip(),
                data_nascimento=normalize_birth_date(row["data_nascimento"]),
                limite_atual=parse_money(row["limite_atual"]),
                score=int(row["score"]),
            )
            customers[customer.cpf_cliente] = customer
        limits = [
            (int(row["score_minimo"]), parse_money(row["limite_maximo"])) for row in parsed_limits
        ]
        return customers, limits, parsed_requests

    def _read_file_bytes(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise DomainError("Base bancária não inicializada.") from exc
        except OSError as exc:
            raise DomainError("Não foi possível ler a base bancária.") from exc

    def _parse_file_bytes(self, name: str, contents: bytes) -> list[dict[str, str]]:
        try:
            text = contents.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DomainError("Base bancária inválida.") from exc
        expected = {
            _CUSTOMERS: _CUSTOMER_FIELDS,
            _SCORE_LIMITS: _SCORE_FIELDS,
            _REQUESTS: _REQUEST_FIELDS,
        }[name]
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            if tuple(reader.fieldnames or ()) != expected:
                raise DomainError("Schema CSV inválido.")
            rows: list[dict[str, str]] = []
            for row in reader:
                if row is None:
                    continue
                values = list(row.values())
                if row.get(None) is not None or any(value is None for value in values):
                    raise DomainError("Base bancária inválida.")
                if all(value == "" for value in values):
                    continue
                rows.append({field: row[field] for field in expected})
        except csv.Error as exc:
            raise DomainError("Base bancária inválida.") from exc

        if name == _CUSTOMERS:
            self._validate_customer_rows(rows)
        elif name == _SCORE_LIMITS:
            self._validate_score_rows(rows)
        else:
            self._validate_request_rows(rows)
        return rows

    def _validate_customer_rows(self, rows: list[dict[str, str]]) -> None:
        seen: set[str] = set()
        for row in rows:
            cpf = normalize_cpf(row["cpf_cliente"])
            if cpf in seen or not row["nome"].strip() or "\x00" in row["nome"]:
                raise DomainError("Base bancária inválida.")
            seen.add(cpf)
            normalize_birth_date(row["data_nascimento"])
            parse_money(row["limite_atual"])
            score = self._parse_score(row["score"])
            if not 0 <= score <= 1000:
                raise DomainError("Base bancária inválida.")

    def _validate_score_rows(self, rows: list[dict[str, str]]) -> None:
        if not rows:
            raise DomainError("Tabela de limites inválida.")
        seen: set[int] = set()
        previous = -1
        previous_limit = parse_money(0)
        for index, row in enumerate(rows):
            minimum = self._parse_score(row["score_minimo"])
            limit = parse_money(row["limite_maximo"])
            if (
                not 0 <= minimum <= 1000
                or minimum in seen
                or minimum < previous
                or (index == 0 and minimum != 0)
                or limit < previous_limit
            ):
                raise DomainError("Tabela de limites inválida.")
            seen.add(minimum)
            previous = minimum
            previous_limit = limit

    def _validate_request_rows(self, rows: list[dict[str, str]]) -> None:
        for row in rows:
            normalize_cpf(row["cpf_cliente"])
            self._parse_utc_timestamp(row["data_hora_solicitacao"])
            parse_money(row["limite_atual"])
            parse_money(row["novo_limite_solicitado"])
            if row["status_pedido"] not in _ALLOWED_STATUSES:
                raise DomainError("Histórico de solicitações inválido.")

    def _validate_cross_references(
        self,
        customers: list[dict[str, str]],
        limits: list[dict[str, str]],
        requests: list[dict[str, str]],
    ) -> None:
        self._validate_customer_rows(customers)
        self._validate_score_rows(limits)
        self._validate_request_rows(requests)
        cpfs = {row["cpf_cliente"] for row in customers}
        if any(row["cpf_cliente"] not in cpfs for row in requests):
            raise DomainError("Histórico de solicitações inválido.")

    @staticmethod
    def _parse_score(value: str) -> int:
        if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
            raise DomainError("Score inválido.")
        return int(value)

    @staticmethod
    def _parse_utc_timestamp(value: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise DomainError("Data de solicitação inválida.")
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise DomainError("Data de solicitação inválida.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise DomainError("Data de solicitação inválida.")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _serialize_csv(rows: list[dict[str, str]], fields: tuple[str, ...]) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8")

    def _serialize_customers(self, rows: list[dict[str, str]]) -> bytes:
        return self._serialize_csv(rows, _CUSTOMER_FIELDS)

    def _serialize_requests(self, rows: list[dict[str, str]]) -> bytes:
        return self._serialize_csv(rows, _REQUEST_FIELDS)

    # ---- journal and transaction --------------------------------------------

    def _read_journal_locked(self) -> dict[str, Any]:
        if not self._journal_path.exists():
            return {"version": _JOURNAL_VERSION, "operations": {}, "transaction": None}
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError("Diário de recuperação inválido.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _JOURNAL_VERSION
            or not isinstance(payload.get("operations"), dict)
        ):
            raise DomainError("Diário de recuperação inválido.")
        if payload.get("transaction") is not None and not isinstance(payload["transaction"], dict):
            raise DomainError("Diário de recuperação inválido.")
        return payload

    def _write_journal_locked(self, journal: dict[str, Any]) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(prefix=".journal-", dir=str(self._journal_path.parent))
            temporary_path = Path(raw_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as journal_file:
                json.dump(journal, journal_file, ensure_ascii=False, sort_keys=True)
                journal_file.write("\n")
                journal_file.flush()
                os.fsync(journal_file.fileno())
            os.replace(str(temporary_path), str(self._journal_path))
            temporary_path = None
        except Exception as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise DomainError("Não foi possível gravar o diário de recuperação.") from exc

    def _recover_journal_locked(self) -> None:
        journal = self._read_journal_locked()
        transaction = journal.get("transaction")
        if transaction is None:
            self._resume_pending_locked(journal)
            return
        files = transaction.get("files")
        if not isinstance(files, list) or not files:
            raise DomainError("Diário de recuperação inválido.")

        for entry in files:
            self._validate_transaction_entry(entry)
            target = self.data_dir / entry["name"]
            current_hash = self._hash_file(target)
            if current_hash == entry["new_hash"]:
                self._unlink_temp(entry["temp"])
                continue
            if current_hash not in {entry.get("old_hash"), None}:
                raise DomainError("Base bancária alterada durante uma operação.")
            temp_path = self._safe_temp_path(entry["temp"])
            if not temp_path.exists():
                self._write_temp_bytes(temp_path, self._decode_content(entry["content"]))
            try:
                os.replace(str(temp_path), str(target))
            except Exception as exc:
                raise DomainError("Não foi possível recuperar a base bancária.") from exc
            if self._hash_file(target) != entry["new_hash"]:
                raise DomainError("Falha na recuperação da base bancária.")

        operation = transaction.get("operation")
        if operation is not None:
            if not isinstance(operation, dict) or not isinstance(
                operation.get("operation_id"), str
            ):
                raise DomainError("Diário de recuperação inválido.")
            operation_id = operation["operation_id"]
            previous = journal["operations"].get(operation_id)
            if previous is not None and previous != operation:
                valid_transition = (
                    isinstance(previous, dict)
                    and previous.get("fingerprint") == operation.get("fingerprint")
                    and previous.get("status") == "pending"
                    and operation.get("status") == "completed"
                )
                if not valid_transition:
                    raise DomainError("Conflito no diário de operações.")
            journal["operations"][operation_id] = operation
        journal["transaction"] = None
        self._write_journal_locked(journal)
        self._resume_pending_locked(journal)

    def _resume_pending_locked(self, journal):
        for record in list(journal["operations"].values()):
            if not isinstance(record, dict):
                raise DomainError("Diário de operações inválido.")
            if record.get("status") == "pending":
                self._finish_pending_request_locked(journal, record, record.get("requested"))

    def _atomic_commit_locked(self, changes: dict[str, bytes], operation: dict[str, Any]) -> None:
        if not changes:
            raise DomainError("Operação sem alterações.")
        journal = self._read_journal_locked()
        if journal.get("transaction") is not None:
            raise DomainError("Há uma operação de recuperação pendente.")
        entries: list[dict[str, Any]] = []
        try:
            for name, content in changes.items():
                if name not in _FILES:
                    raise DomainError("Arquivo de base inválido.")
                target = self.data_dir / name
                old_hash = self._hash_file(target)
                temp_path = self._create_temp(content)
                entries.append(
                    {
                        "name": name,
                        "old_hash": old_hash,
                        "new_hash": self._hash_bytes(content),
                        "temp": temp_path.name,
                        "content": base64.b64encode(content).decode("ascii"),
                    }
                )
        except DomainError:
            for entry in entries:
                self._unlink_temp(entry["temp"])
            raise
        except OSError as exc:
            for entry in entries:
                self._unlink_temp(entry["temp"])
            raise DomainError("Não foi possível preparar a operação bancária.") from exc

        transaction = {"files": entries, "operation": operation}
        journal["transaction"] = transaction
        self._write_journal_locked(journal)
        try:
            for entry in entries:
                os.replace(
                    str(self._safe_temp_path(entry["temp"])), str(self.data_dir / entry["name"])
                )
        except Exception as exc:
            # The journal intentionally remains.  A later repository access
            # completes whichever replacements were still outstanding.
            raise DomainError("Não foi possível concluir a operação bancária.") from exc

        for entry in entries:
            if self._hash_file(self.data_dir / entry["name"]) != entry["new_hash"]:
                raise DomainError("Falha ao verificar a operação bancária.")
        journal["operations"][operation["operation_id"]] = operation
        journal["transaction"] = None
        self._write_journal_locked(journal)

    def _validate_transaction_entry(self, entry: Any) -> None:
        if not isinstance(entry, dict) or entry.get("name") not in _FILES:
            raise DomainError("Diário de recuperação inválido.")
        for key in ("new_hash", "temp", "content"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise DomainError("Diário de recuperação inválido.")
        if entry.get("old_hash") is not None and not isinstance(entry["old_hash"], str):
            raise DomainError("Diário de recuperação inválido.")
        self._decode_content(entry["content"])

    def _decode_content(self, content: str) -> bytes:
        try:
            return base64.b64decode(content.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise DomainError("Diário de recuperação inválido.") from exc

    def _safe_temp_path(self, name: str) -> Path:
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith(".banco-tx-")
        ):
            raise DomainError("Diário de recuperação inválido.")
        return self.data_dir / name

    def _create_temp(self, content: bytes) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix=".banco-tx-", dir=str(self.data_dir))
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
        except OSError:
            self._unlink_temp(path.name)
            raise
        return path

    def _write_temp_bytes(self, path: Path, content: bytes) -> None:
        try:
            with path.open("xb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
        except FileExistsError:
            pass
        except OSError as exc:
            raise DomainError("Não foi possível recuperar a base bancária.") from exc

    def _unlink_temp(self, name: str) -> None:
        try:
            self._safe_temp_path(name).unlink(missing_ok=True)
        except (OSError, DomainError):
            pass

    @staticmethod
    def _hash_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _hash_file(self, path: Path) -> str | None:
        try:
            if not path.exists():
                return None
            return self._hash_bytes(path.read_bytes())
        except OSError as exc:
            raise DomainError("Não foi possível ler a base bancária.") from exc

    # ---- operation records ---------------------------------------------------

    @staticmethod
    def _normalize_operation_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise DomainError("Identificador de operação inválido.")
        if any(ord(character) < 32 for character in value):
            raise DomainError("Identificador de operação inválido.")
        return value.strip()

    @staticmethod
    def _fingerprint(kind: str, payload: dict[str, Any]) -> str:
        return json.dumps(
            {"kind": kind, **payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _check_operation(
        journal: dict[str, Any], operation_id: str, fingerprint: str
    ) -> dict[str, Any] | None:
        record = journal["operations"].get(operation_id)
        if record is None:
            return None
        if (
            not isinstance(record, dict)
            or record.get("fingerprint") != fingerprint
            or record.get("operation_id") != operation_id
        ):
            raise DomainError("Identificador de operação já usado com outros dados.")
        if record.get("status") not in {"pending", "completed"}:
            raise DomainError("Diário de operações inválido.")
        return record

    @staticmethod
    def _money_text(value: Any) -> str:
        return f"{parse_money(value):.2f}"

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _credit_from_record(record: dict[str, Any]) -> CreditResult:
        try:
            result = record["result"]
            if not isinstance(result, dict):
                raise TypeError
            return CreditResult(
                status_pedido=str(result["status_pedido"]),
                limite_atual=parse_money(result["limite_atual"]),
                novo_limite_solicitado=parse_money(result["novo_limite_solicitado"]),
                limite_maximo=parse_money(result["limite_maximo"]),
                score=int(result["score"]),
            )
        except (KeyError, TypeError, ValueError, DomainError) as exc:
            raise DomainError("Diário de operações inválido.") from exc

    def _finish_pending_request_locked(
        self, journal: dict[str, Any], pending: dict[str, Any], requested: Any
    ) -> CreditResult:
        try:
            cpf = normalize_cpf(pending["cpf"])
            requested_money = parse_money(pending["requested"])
            current_limit = parse_money(pending["current_limit"])
            timestamp = pending["timestamp"]
            operation_id = self._normalize_operation_id(pending["operation_id"])
            fingerprint = pending["fingerprint"]
        except (KeyError, TypeError, DomainError) as exc:
            raise DomainError("Diário de operações inválido.") from exc
        if requested_money != parse_money(requested) or not isinstance(fingerprint, str):
            raise DomainError("Identificador de operação já usado com outros dados.")

        customers, score_limits, requests = self._load_all_locked()
        customer = customers.get(cpf)
        if customer is None or customer.limite_atual != current_limit:
            raise DomainError("Cliente alterado durante uma operação pendente.")
        pending_rows = [
            row
            for row in requests
            if row["cpf_cliente"] == cpf
            and row["data_hora_solicitacao"] == timestamp
            and row["status_pedido"] == "pendente"
            and parse_money(row["novo_limite_solicitado"]) == requested_money
        ]
        if len(pending_rows) != 1:
            raise DomainError("Solicitação pendente não encontrada.")

        maximum = self._limit_for_score(customer.score, score_limits)
        approved = requested_money > customer.limite_atual and requested_money <= maximum
        status = "aprovado" if approved else "rejeitado"
        result = CreditResult(
            status, customer.limite_atual, requested_money, maximum, customer.score
        )
        updated_requests: list[dict[str, str]] = []
        changed = False
        for row in requests:
            row_copy = dict(row)
            if (
                not changed
                and row_copy["cpf_cliente"] == cpf
                and row_copy["data_hora_solicitacao"] == timestamp
                and row_copy["status_pedido"] == "pendente"
                and parse_money(row_copy["novo_limite_solicitado"]) == requested_money
            ):
                row_copy["status_pedido"] = status
                changed = True
            updated_requests.append(row_copy)
        changes: dict[str, bytes] = {_REQUESTS: self._serialize_requests(updated_requests)}
        if approved:
            updated_customers: list[dict[str, str]] = []
            for row in self._read_csv(_CUSTOMERS):
                row_copy = dict(row)
                if row_copy["cpf_cliente"] == cpf:
                    row_copy["limite_atual"] = self._money_text(requested_money)
                updated_customers.append(row_copy)
            changes[_CUSTOMERS] = self._serialize_customers(updated_customers)

        completed_record = {
            "operation_id": operation_id,
            "kind": "request_increase",
            "fingerprint": fingerprint,
            "status": "completed",
            "result": {
                "status_pedido": result.status_pedido,
                "limite_atual": self._money_text(result.limite_atual),
                "novo_limite_solicitado": self._money_text(result.novo_limite_solicitado),
                "limite_maximo": self._money_text(result.limite_maximo),
                "score": result.score,
            },
        }
        self._atomic_commit_locked(changes, completed_record)
        return result

    @staticmethod
    def _limit_for_score(score: int, score_limits: list[tuple[int, Any]]) -> Any:
        eligible = [limit for minimum, limit in score_limits if minimum <= score]
        if not eligible:
            return parse_money(0)
        return max(eligible)

    def _read_csv(self, name: str) -> list[dict[str, str]]:
        path = self.data_dir / name
        return self._parse_file_bytes(name, self._read_file_bytes(path))


__all__ = ["BankRepository"]

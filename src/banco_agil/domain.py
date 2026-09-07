"""Primitives and business rules for Banco Ágil.

The application layer deliberately knows very little about these objects.  The
functions in this module therefore validate at the boundary and return stable,
plain dataclasses that are convenient for both the Streamlit app and tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TypeAlias


class DomainError(Exception):
    """An expected, safe-to-show domain validation or persistence error."""


MoneyInput: TypeAlias = str | int | float | Decimal
_CENT = Decimal("0.01")


@dataclass
class Customer:
    cpf_cliente: str
    nome: str
    data_nascimento: str
    limite_atual: Decimal
    score: int


@dataclass
class Interview:
    renda_mensal: Decimal
    tipo_emprego: str
    despesas_mensais: Decimal
    num_dependentes: int
    tem_dividas: bool


@dataclass
class CreditResult:
    status_pedido: str
    limite_atual: Decimal
    novo_limite_solicitado: Decimal
    limite_maximo: Decimal
    score: int


def normalize_cpf(value: str) -> str:
    """Return an 11-digit CPF, retaining leading zeroes."""

    if not isinstance(value, str):
        raise DomainError("CPF inválido.")
    cpf = value.strip()
    if re.fullmatch(r"[0-9]{11}", cpf):
        return cpf
    if re.fullmatch(r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}", cpf):
        return cpf.replace(".", "").replace("-", "")
    raise DomainError("CPF inválido.")


def normalize_birth_date(value: str) -> str:
    """Normalize DD/MM/YYYY and ISO dates to ISO (YYYY-MM-DD).

    A date in the future is never a valid customer birth date.  Exact regular
    expressions are used so values such as ``1/2/1990`` or an ISO timestamp do
    not silently become accepted customer identities.
    """

    if not isinstance(value, str):
        raise DomainError("Data de nascimento inválida.")
    raw = value.strip()
    try:
        if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", raw):
            parsed = date.fromisoformat(f"{raw[6:10]}-{raw[3:5]}-{raw[0:2]}")
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            parsed = date.fromisoformat(raw)
        else:
            raise ValueError
    except ValueError as exc:
        raise DomainError("Data de nascimento inválida.") from exc
    if parsed > date.today():
        raise DomainError("Data de nascimento inválida.")
    return parsed.isoformat()


def _parse_decimal_number(value: MoneyInput) -> Decimal:
    if isinstance(value, bool):
        raise DomainError("Valor monetário inválido.")
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, float):
        # str() avoids importing the binary approximation into a financial
        # amount while still rejecting values such as nan and infinity below.
        decimal_value = Decimal(str(value))
    elif isinstance(value, str):
        raw = value.strip()
        if raw.startswith("R$"):
            raw = raw[2:].strip()
        if not raw or raw.startswith(("+", "-")):
            raise DomainError("Valor monetário inválido.")
        if any(character.isspace() for character in raw):
            raise DomainError("Valor monetário inválido.")

        if "," in raw:
            # Brazilian notation: dots are thousands separators and comma is
            # the decimal separator.  Grouping is strict to catch typos.
            if raw.count(",") != 1:
                raise DomainError("Valor monetário inválido.")
            integer_part, fractional_part = raw.split(",")
            if "." in integer_part:
                valid_integer = re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})*", integer_part)
            else:
                valid_integer = re.fullmatch(r"[0-9]+", integer_part)
            if valid_integer is None or not re.fullmatch(r"[0-9]{1,2}", fractional_part):
                raise DomainError("Valor monetário inválido.")
            raw = integer_part.replace(".", "") + "." + fractional_part
        elif "." in raw:
            # CSV data uses canonical Decimal notation (1000.00).  A single
            # dot with three digits is ambiguous between 1.000 and 1000, so
            # reject it; multiple correctly-sized dots unambiguously denote
            # Brazilian thousands grouping.
            if raw.count(".") == 1:
                integer_part, fractional_part = raw.split(".")
                if not re.fullmatch(r"[0-9]+", integer_part) or not re.fullmatch(
                    r"[0-9]{1,2}", fractional_part
                ):
                    raise DomainError("Valor monetário inválido.")
            elif not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+", raw):
                raise DomainError("Valor monetário inválido.")
            else:
                raw = raw.replace(".", "")
        elif not re.fullmatch(r"[0-9]+", raw):
            raise DomainError("Valor monetário inválido.")
        try:
            decimal_value = Decimal(raw)
        except InvalidOperation as exc:
            raise DomainError("Valor monetário inválido.") from exc
    else:
        raise DomainError("Valor monetário inválido.")

    if not decimal_value.is_finite() or decimal_value < 0:
        raise DomainError("Valor monetário inválido.")
    try:
        quantized = decimal_value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("Valor monetário inválido.") from exc
    # Quantize can round a value with more than two fractional digits.  Money
    # inputs are required to already have cent precision, so reject instead of
    # silently changing the requested amount.
    if quantized != decimal_value:
        raise DomainError("Valor monetário inválido.")
    return quantized


def parse_money(value: MoneyInput) -> Decimal:
    """Parse a non-negative BRL amount and return exactly two decimal places."""

    return _parse_decimal_number(value)


def _validate_interview(interview: Interview) -> tuple[Decimal, Decimal]:
    if not isinstance(interview, Interview):
        raise DomainError("Entrevista inválida.")
    income = parse_money(interview.renda_mensal)
    expenses = parse_money(interview.despesas_mensais)
    if interview.tipo_emprego not in {"formal", "autonomo", "desempregado"}:
        raise DomainError("Tipo de emprego inválido.")
    if (
        isinstance(interview.num_dependentes, bool)
        or not isinstance(interview.num_dependentes, int)
        or interview.num_dependentes < 0
    ):
        raise DomainError("Número de dependentes inválido.")
    if not isinstance(interview.tem_dividas, bool):
        raise DomainError("Informação de dívidas inválida.")
    return income, expenses


def calculate_score(interview: Interview) -> int:
    """Calculate the rubric score, rounded HALF_UP and clamped to 0..1000.

    The explicit rubric is kept in Decimal arithmetic until the final rounding:
    income divided by expenses plus one, times 30; employment, dependants and
    debt adjustments are then added.  The demonstration interview scores 575.
    """

    income, expenses = _validate_interview(interview)
    employment_bonus = {
        "formal": Decimal("300"),
        "autonomo": Decimal("200"),
        "desempregado": Decimal("0"),
    }[interview.tipo_emprego]
    dependant_bonus = {
        0: Decimal("100"),
        1: Decimal("80"),
        2: Decimal("60"),
    }.get(interview.num_dependentes, Decimal("30"))
    debt_adjustment = Decimal("-100") if interview.tem_dividas else Decimal("100")
    raw_score = (
        income / (expenses + Decimal("1")) * Decimal("30")
        + employment_bonus
        + dependant_bonus
        + debt_adjustment
    )
    rounded = int(raw_score.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0, min(1000, rounded))


__all__ = [
    "CreditResult",
    "Customer",
    "DomainError",
    "Interview",
    "calculate_score",
    "normalize_birth_date",
    "normalize_cpf",
    "parse_money",
]

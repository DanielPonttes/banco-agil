from datetime import date, timedelta
from decimal import Decimal

import pytest

from banco_agil.domain import (
    DomainError,
    Interview,
    calculate_score,
    normalize_birth_date,
    normalize_cpf,
    parse_money,
)


def test_normalizers_keep_zero_and_canonicalize_birth_date():
    assert normalize_cpf("012.345.678-90") == "01234567890"
    assert normalize_cpf("01234567890") == "01234567890"
    assert normalize_birth_date("15/01/1990") == "1990-01-15"
    assert normalize_birth_date("1990-01-15") == "1990-01-15"

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(DomainError):
        normalize_birth_date(tomorrow)


def test_parse_money_brazilian_and_ambiguous_forms():
    assert parse_money("R$ 1.000,50") == Decimal("1000.50")
    assert parse_money("1.000.000") == Decimal("1000000.00")
    assert parse_money("10.5") == Decimal("10.50")
    assert parse_money(12) == Decimal("12.00")
    for value in ("1.000", "1,000", "1.2345", "-1,00", "nan"):
        with pytest.raises(DomainError):
            parse_money(value)


def test_score_demo_half_up_and_clamp():
    interview = Interview(Decimal("5000"), "formal", Decimal("2000"), 0, False)
    assert calculate_score(interview) == 575
    assert calculate_score(Interview(0, "formal", 0, 0, False)) == 500
    assert calculate_score(Interview(10**9, "formal", 0, 0, False)) == 1000
    assert calculate_score(Interview(0, "desempregado", 10**9, 20, True)) == 0


def test_interview_rejects_invalid_enums_and_values():
    with pytest.raises(DomainError):
        calculate_score(Interview(100, "informal", 10, 0, False))
    with pytest.raises(DomainError):
        calculate_score(Interview(100, "formal", 10, -1, False))
    with pytest.raises(DomainError):
        calculate_score(Interview(100, "formal", 10, 0, 1))


@pytest.mark.parametrize("employment,deps,debts,expected", [
    ("formal", 0, False, 500),
    ("autonomo", 1, False, 380),
    ("desempregado", 2, False, 160),
    ("formal", 3, True, 230),
    ("formal", 99, True, 230),
    ("desempregado", 0, True, 0),
])
def test_all_score_weights(employment, deps, debts, expected):
    assert calculate_score(Interview(Decimal("0"), employment, Decimal("0"), deps, debts)) == expected


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-1", True, "10.001", "1.00.000,00"])
def test_invalid_money_is_rejected(raw):
    with pytest.raises(DomainError):
        parse_money(raw)


def test_score_clamp_and_half_up():
    assert calculate_score(Interview(Decimal("100000"), "formal", Decimal("0"), 0, False)) == 1000
    assert calculate_score(Interview(Decimal("0"), "desempregado", Decimal("0"), 3, True)) == 0
    assert calculate_score(Interview(Decimal("1"), "formal", Decimal("3"), 0, False)) == 508


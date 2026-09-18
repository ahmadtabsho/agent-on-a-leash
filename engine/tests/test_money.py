"""CHF conversion must reproduce the pack's own `billing_amount_chf`.

The fixtures are the oracle here: if our arithmetic and the platform's disagree
on a single attempt, every spending limit we enforce is off.
"""

from decimal import Decimal

import pytest

from leash.datapack import load_table
from leash.models.enums import Currency
from leash.models.money import fx_rates, money, to_chf


def test_rates_match_the_documented_fixed_values():
    assert fx_rates() == {
        Currency.CHF: Decimal("1.000000"),
        Currency.EUR: Decimal("0.950000"),
        Currency.GBP: Decimal("1.120000"),
        Currency.USD: Decimal("0.870000"),
    }


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("199.00", "EUR", "189.05"),
        ("219.00", "GBP", "245.28"),
        ("450.00", "USD", "391.50"),
        ("260.00", "EUR", "247.00"),
        ("20.00", "CHF", "20.00"),
    ],
)
def test_known_conversions(amount, currency, expected):
    assert to_chf(amount, currency) == Decimal(expected)


def test_conversion_reproduces_billing_amount_for_every_attempt():
    mismatches = []
    for row in load_table("purchase_attempts"):
        ours = to_chf(row["amount"], row["currency"])
        theirs = Decimal(row["billing_amount_chf"])
        if ours != theirs:
            mismatches.append((row["authorization_id"], row["currency"], ours, theirs))
    assert not mismatches, mismatches


def test_amount_already_includes_delivery_for_every_attempt():
    """`amount` is the total. Adding `delivery_fee` to it would double-charge."""
    mismatches = []
    for row in load_table("purchase_attempts"):
        stated = money(row["amount"])
        parts = money(Decimal(row["items_subtotal"]) + Decimal(row["delivery_fee"]))
        if stated != parts:
            mismatches.append((row["authorization_id"], stated, parts))
    assert not mismatches, mismatches


def test_money_does_not_inherit_binary_float_error():
    assert money(0.1) + money(0.2) == Decimal("0.30")


def test_rounding_is_half_even():
    assert money("2.345") == Decimal("2.34")
    assert money("2.355") == Decimal("2.36")

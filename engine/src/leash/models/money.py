"""CHF arithmetic.

Spending limits are the spine of the challenge, so money never touches binary
floating point here. Amounts are `Decimal`, conversions use the pack's fixed
rates, and every result is quantised to two places with half-even rounding —
the rule the data dictionary states.

Two traps this module exists to prevent:

* Converting with the merchant's country instead of the row's `currency`.
  A CHF amount does not imply a Swiss merchant, and a foreign merchant may
  bill in CHF because the cardholder accepted conversion at the till.
* Adding the delivery fee a second time. `amount` already includes it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path

from ..config import DATA_DIR
from .enums import Currency

CENTS = Decimal("0.01")


def money(value: Decimal | float | str) -> Decimal:
    """Quantise to two places, half-even, as the data dictionary specifies."""
    if not isinstance(value, Decimal):
        # str() first: Decimal(0.1) would carry the float's binary error in.
        value = Decimal(str(value))
    return value.quantize(CENTS, rounding=ROUND_HALF_EVEN)


@lru_cache(maxsize=1)
def fx_rates(data_dir: Path = DATA_DIR) -> dict[Currency, Decimal]:
    """Fixed synthetic rates to CHF, read from the pack rather than hard-coded."""
    import csv

    rates: dict[Currency, Decimal] = {}
    with (data_dir / "fx_rates.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["to_currency"] != "CHF":
                continue
            rates[Currency(row["from_currency"])] = Decimal(row["rate"])
    missing = set(Currency) - set(rates)
    if missing:
        raise ValueError(f"fx_rates.csv is missing rates to CHF for {sorted(missing)}")
    return rates


def to_chf(amount: Decimal | float | str, currency: Currency | str) -> Decimal:
    """Convert an amount in its own currency to CHF.

    Pass the row's `currency`, never something derived from the merchant's
    country.
    """
    rate = fx_rates()[Currency(currency)]
    return money(Decimal(str(amount)) * rate)

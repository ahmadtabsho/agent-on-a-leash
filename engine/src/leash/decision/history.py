"""What this card has done before.

Familiarity is a claim about the past, so it comes from the authorization
history and nothing else. Two rules govern how it is read:

* Match on identifiers, never on names. A shop's *name* is the one thing an
  impostor controls; `merchant_id` is not. `PixelHarbour` and `PixelHarbor`
  read almost identically and are different merchants.
* Count only approved records. A declined attempt is not evidence that the
  cardholder uses a shop.

Names are still indexed, for one purpose only: telling the customer that an
unfamiliar shop is wearing a familiar shop's name.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from ..datapack import load_table

APPROVED = "approved"


def _key(name: str) -> str:
    """Fold a display name for comparison: case, spacing and punctuation."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass(frozen=True)
class Lookalike:
    familiar_merchant_id: str
    familiar_name: str
    similarity: float


class CardHistory:
    """Indexed prior activity for every card in the supplied history."""

    def __init__(self, rows: Iterable[dict]):
        self.merchant_counts: Counter[tuple[str, str]] = Counter()
        self.device_counts: Counter[tuple[str, str]] = Counter()
        self._names: dict[str, str] = {}
        self._card_merchants: dict[str, set[str]] = {}

        for row in rows:
            if row.get("status") != APPROVED:
                continue
            card = row["card_id"]
            merchant = row["merchant_id"]
            self.merchant_counts[(card, merchant)] += 1
            self._names.setdefault(merchant, row.get("merchant_name", ""))
            self._card_merchants.setdefault(card, set()).add(merchant)
            device = row.get("customer_device_id") or ""
            if device:
                self.device_counts[(card, device)] += 1

    def merchant_purchases(self, card_id: str, merchant_id: str) -> int:
        return self.merchant_counts[(card_id, merchant_id)]

    def device_purchases(self, card_id: str, device_id: str) -> int:
        if not device_id:
            return 0
        return self.device_counts[(card_id, device_id)]

    def known_merchants(self, card_id: str) -> set[str]:
        return self._card_merchants.get(card_id, set())

    def find_lookalike(
        self, card_id: str, merchant_id: str, merchant_name: str, *, threshold: float = 0.85
    ) -> Lookalike | None:
        """Is an unfamiliar shop wearing a familiar shop's name?

        Only asked about shops this card has *not* used. A familiar shop cannot
        impersonate itself.
        """
        if self.merchant_purchases(card_id, merchant_id):
            return None
        candidate = _key(merchant_name)
        if not candidate:
            return None
        best: Lookalike | None = None
        for known_id in self.known_merchants(card_id):
            known_name = self._names.get(known_id, "")
            ratio = SequenceMatcher(None, candidate, _key(known_name)).ratio()
            if ratio >= threshold and (best is None or ratio > best.similarity):
                best = Lookalike(known_id, known_name, round(ratio, 4))
        return best


@lru_cache(maxsize=1)
def default_history() -> CardHistory:
    """The vendored history pack, loaded once."""
    return CardHistory(load_table("authorization_history"))

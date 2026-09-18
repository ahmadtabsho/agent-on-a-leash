"""The vendored pack must match its manifest, and the joins must hold.

If either breaks, every downstream decision is built on a different fixture set
than the live API serves.
"""

from leash.datapack import check_pack, load_table


def test_pack_matches_manifest():
    failures = [r for r in check_pack() if not r.ok]
    assert not failures, [(r.name, r.problem) for r in failures]


def test_scenarios_cover_all_forty_five_attempts():
    attempts = load_table("purchase_attempts")
    catalogue = {r["scenario_id"]: int(r["event_count"]) for r in load_table("scenario_catalogue")}

    assert len(attempts) == 45
    assert sum(catalogue.values()) == 45

    per_scenario: dict[str, int] = {}
    for row in attempts:
        per_scenario[row["scenario_id"]] = per_scenario.get(row["scenario_id"], 0) + 1
    assert per_scenario == catalogue


def test_every_attempt_has_contiguous_replay_order_and_cart_lines():
    attempts = load_table("purchase_attempts")
    carts = {row["authorization_id"] for row in load_table("purchase_attempt_items")}

    by_scenario: dict[str, list[int]] = {}
    for row in attempts:
        by_scenario.setdefault(row["scenario_id"], []).append(int(row["replay_order"]))
        assert row["authorization_id"] in carts, row["authorization_id"]

    for scenario, orders in by_scenario.items():
        assert sorted(orders) == list(range(1, len(orders) + 1)), scenario


def test_attempt_foreign_keys_resolve():
    merchants = {r["merchant_id"] for r in load_table("merchants")}
    cards = {r["card_id"] for r in load_table("cards")}
    authorities = {r["authority_id"] for r in load_table("scenario_authorities")}

    for row in load_table("purchase_attempts"):
        assert row["merchant_id"] in merchants
        assert row["card_id"] in cards
        assert row["authority_id"] in authorities

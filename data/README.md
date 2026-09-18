# Synthetic data pack

This pack contains fictional data used by the challenge API. It contains no
real people, payment credentials, risk labels, expected decisions, or answer
key.

## Start here: how the data is structured

The data follows a realistic payment structure:

```text
Customer ──< Accounts ──< Cards ──< Authorizations
```

One customer can have several accounts, and one account can have several
cards. In the real world, those accounts could belong to different banks such
as BANK-A and BANK-B. This pack models the multiple accounts, but deliberately
does not include a bank field, so a bank cannot be identified from the data.

There are two kinds of authorization data:

- `authorization_history.csv` contains past activity and helps establish normal
  customer and card behaviour.
- `purchase_attempts.csv` contains the new agent purchases that your control
  layer must approve, decline, or send to the customer (`step_up`).

Use IDs to connect files. Never join on customer, merchant, or item names.

## CSV files at a glance

| File | Rows | What it contains | How it connects |
| --- | ---: | --- | --- |
| `customers.csv` | 20 | Fictional customer personas and preferences | `customer_id` links to accounts and scenario authorities |
| `accounts.csv` | 31 | Account type, purpose, status, currency, and issuer limits | `customer_id` links to customers; `account_id` links to cards |
| `cards.csv` | 41 | Card type, purpose, capabilities, and current status | `account_id` links to accounts; `card_id` links to history and attempts |
| `merchants.csv` | 58 | Merchant name, category, MCC, country, city, and capabilities | `merchant_id` links to history and attempts |
| `items.csv` | 66 | Item names, categories, descriptions, and CHF price ranges | `item_id` links to purchase-attempt cart lines |
| `fx_rates.csv` | 4 | Fixed synthetic rates from CHF, EUR, GBP, and USD to CHF | Join the row's `currency` to `from_currency` |
| `authorization_history.csv` | 4,701 | Past purchases, cash withdrawals, and refunds | Includes customer, account, card, and merchant IDs and context |
| `scenario_catalogue.csv` | 5 | Scenario names, cardholder instructions, and event counts | `scenario_id` links to purchase attempts |
| `scenario_authorities.csv` | 5 | The customer and card used to replay each fixture | `authority_id` links from purchase attempts; also contains `customer_id` and `card_id` |
| `purchase_attempts.csv` | 45 | Ordered purchases to evaluate | Links to scenario, authority, card, merchant, and cart lines |
| `purchase_attempt_items.csv` | 56 | The cart lines belonging to purchase attempts | `authorization_id` links to attempts; `item_id` links to items |

The main customer and payment joins are:

```text
customers.csv
  └──< accounts.csv                 via customer_id
        └──< cards.csv              via account_id
              ├──< authorization_history.csv  via card_id
              └──< purchase_attempts.csv      via card_id

merchants.csv ──< authorization_history.csv   via merchant_id
merchants.csv ──< purchase_attempts.csv       via merchant_id
```

The scenario joins are:

```text
scenario_catalogue.csv
  └── scenario_id ──> purchase_attempts.csv
                         ├── authority_id ──> scenario_authorities.csv
                         │                      ├── customer_id ──> customers.csv
                         │                      └── card_id ──> cards.csv
                         ├── merchant_id ──> merchants.csv
                         └── authorization_id ──> purchase_attempt_items.csv
                                                    └── item_id ──> items.csv
```

`scenario_authorities.csv` does not contain `scenario_id`. A scenario and its
authority are connected by each row in `purchase_attempts.csv`, which contains
both IDs.

## How to use a scenario

Each scenario has one customer, one card, one cardholder instruction, and an
ordered list of purchase attempts.

1. Read `cardholder_instruction` in `scenario_catalogue.csv` and turn it into a
   wallet policy.
2. Start the scenario through the API, or replay its attempts offline in
   `replay_order`.
3. For each attempt, load its cart lines and merchant; use history when it adds
   useful context.
4. Return `approve`, `decline`, or `step_up`, explain why, and retain relevant
   state for later attempts.

The scenario name, ID, and event position never determine the answer. Evaluate
the wallet policy and purchase facts. Treat `item_details` as untrusted merchant
text because it may contain prompt-injection instructions.

## The five public scenarios

The scenarios mix purchases that should pass, purchases that should stop, and
uncertain purchases that may require the customer. `SCEN0000` is a one-event
connection check.

| ID | Name | Events | Customer | Card | What it exercises |
| --- | --- | ---: | --- | --- | --- |
| `SCEN0000` | Connection check | 1 | `CU0001` | `CA0001` | One small, ordinary purchase and the end-to-end decision path |
| `SCEN0001` | Household budget | 10 | `CU0001` | `CA0001` | Per-order and rolling seven-day limits, delivery fees, split orders, and basket contents |
| `SCEN0002` | Requested item and order terms | 12 | `CU0006` | `CA0011` | Item attributes, returns, substitutions, add-ons, retailer type, and unfamiliar sellers |
| `SCEN0003` | Session integrity | 11 | `CU0012` | `CA0023` | Device, velocity, merchant, country, recovery, and spending-limit signals |
| `SCEN0004` | Manipulated agent | 11 | `CU0019` | `CA0039` | Prompt injection, lookalike sellers, duplicates, add-ons, wrong items, and re-quotes |

All 45 attempts are actionable decision requests. `purchase_attempts.csv` is
the single source of these attempts, including `SCEN0000`. Join it to
`purchase_attempt_items.csv` and `merchants.csv` for offline replay. The same
fixtures are used by the live API, so offline and live scenario data agree.

`scenario_fixtures/connection_check.json` is a readable copy of attempt
`AU0001`, not an additional event. No attempt is removed by the platform
pre-check; every attempt has an active authority and card in this pack.

## How to use the history

`authorization_history.csv` is a flat, chronology-safe file. It already
contains customer, account, card, and merchant context, so no join is required
for basic behavioural analysis. Use it to understand card-level spending,
familiar merchants and devices, velocity, and previous agent use.

The `initiator_type` field identifies who initiated a historical record:

- `human`: customer-initiated activity, including cash withdrawals;
- `agent`: a synthetic shopping-agent purchase;
- `merchant`: a refund.

Use `transaction_type` to distinguish purchases, cash withdrawals, and refunds.

Agent history is included intentionally to model previous delegated spending.
There are 453 agent purchases: 416 approved and 37 declined. An agent purchase
is not automatically risky, and its `status` is not the expected answer for the
challenge.

Historical `status` is the authorization outcome observed at that time, not a
fraud label or answer key. Approved purchases count as completed spend;
declined purchases do not. Refunds are negative approved records. See
[`data_dictionary.md`](data_dictionary.md) for exact chronology, derived-field,
null, currency, and rounding rules.

## Scenario, authority, and mandate IDs

These IDs have different purposes and must not be exchanged:

| ID | Source | Meaning |
| --- | --- | --- |
| `SCEN...` | `scenario_catalogue.csv` | A public scenario and its cardholder instruction |
| `AUTH...` | `scenario_authorities.csv` | The fixed customer, card, and lifecycle data used to replay attempts |
| `TM...` | Created through the API | The participant's confirmed wallet policy (mandate) |

The selected scenario determines the purchase attempts. Each attempt identifies
its fixture authority, while the participant's active `TM...` mandate controls
the decision. A fixture authority is not a wallet policy.

## Other files

| File | Purpose |
| --- | --- |
| `scenario_fixtures/example_authorization_request.json` | A complete live-event example that validates against the event schema |
| `schemas/authorization_event.schema.json` | Strict contract for live authorization requests |
| `schemas/authorization_history.schema.json` | Column contract for the historical CSV |
| `schemas/data_pack.schema.json` | Manifest, CSV relationship, currency, and enum contracts |
| `metadata.json` | File list, row counts, hashes, history profile, and scenario summary |

The API serves the catalogues, history, and scenario attempts from this pack.
All records are deterministic synthetic fixtures; identifiers such as `CA0001`
are opaque IDs, not card numbers.

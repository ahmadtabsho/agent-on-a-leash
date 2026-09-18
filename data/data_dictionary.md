# Agent on a Leash — data dictionary

**SYNTHETIC TEST DATA.** All identifiers, people, merchants, amounts, and
timestamps are fictional.

## Units and nulls

- Values ending in `_chf` are CHF amounts.
- `amount`, `unit_price`, and `delivery_fee` use the row's `currency`.
- `billing_amount_chf` is the fixed-rate CHF equivalent.
- The item catalogue ranges (`unit_price_min_chf`, `unit_price_typical_chf`,
  and `unit_price_max_chf`) are always in CHF.
- `fx_rates.csv` contains CHF→CHF `1.000000`, EUR→CHF `0.950000`, GBP→CHF
  `1.120000`, and USD→CHF `0.870000`, all dated `2026-08-01` and marked
  `synthetic_fixed`.
- Convert a purchase line to CHF as `unit_price × fx_rates[currency]` before
  comparing it with an item catalogue range. The same table defines
  `billing_amount_chf = amount × fx_rates[currency]`.
- Monetary values have two decimal places; booleans are lowercase `true` or
  `false`.
- CHF conversions and monetary totals are rounded to two decimal places using
  decimal half-even rounding.
- Empty optional fields mean that no period value, delivery date, linked
  authorization, or related status was supplied.
- `unknown` means that a purchase term was not supplied; `not_applicable` means
  that the term does not apply to that fulfilment type.

### Currency does not follow the merchant's country

Most foreign purchases are billed in the merchant's local currency, but 61
historical rows at foreign merchants are billed in CHF because the cardholder
accepted conversion at the point of sale. Derive the currency from the
`currency` column, never from `merchant_country`. Conversely, a CHF amount does
not imply a Swiss merchant.

### `card_present` is not a restatement of `channel`

`in_store` and `atm` rows are always card-present, and `ecommerce` and
`recurring` rows never are. `mobile_wallet` is genuinely mixed: 415 rows are an
in-person wallet tap (`card_present=true`) and 197 are a remote wallet payment
(`card_present=false`). A control layer that treats the wallet channel as
uniformly remote will mis-read a third of it.

`customer_device_id` is empty on the 1,109 `in_store` and `atm` rows, because
no cardholder device is involved. It is always populated on `ecommerce`,
`recurring`, and `mobile_wallet` rows.

`channel=recurring` describes the payment rail. The separate
`recurring=true` flag describes a recurring agreement and may also occur on an
`ecommerce` row; do not derive either field from the other.

`merchants.csv.availability` is catalogue metadata about the merchant's
available footprint, not a hard validation rule for every historical channel.
The historical `channel` column is authoritative; a merchant with a physical
store can still appear in an ecommerce or remote-order record.

### Card lifecycle

`cards.csv.status`, `first_used_on`, and `expires_on` describe a card **as it
stands today**. `authorization_history.csv.card_status` describes the card **at
the moment of that transaction**. These agree for 39 of the 41 cards.

Two cards left service inside the history window: `CA0009` was blocked and
`CA0026` expired. Their earlier rows carry `card_status=active`; the handful of
later attempts against them carry `blocked` or `expired` and were declined for
that reason. No approved transaction precedes a card's `first_used_on` or falls
on or after its `expires_on`.

## Category vocabulary

Categories are a shared, consistent vocabulary across the catalogue, history,
and scenario fixtures:

- `merchants.csv.merchant_category` and
  `authorization_history.csv.merchant_category` use the same term for the same
  merchant, e.g. `groceries`, `subscriptions`, `sporting_goods`.
- `items.csv.item_category` and `purchase_attempt_items.csv.item_category` use
  the same vocabulary. The recurring content family is `subscriptions` in every
  file; `membership` (a premium tier) and `subscriptions` (a recurring
  subscription) are deliberately separate categories.
- `purchase_attempt_items.csv.item_category` is always one of the
  `items.csv.item_category` values it references, and `item_name` matches the
  referenced catalogue row.
- Three item categories have no merchant-category counterpart, because they
  describe what is in the basket rather than what kind of shop sold it:
  `gift_card`, `membership`, and `cosmetics`. A cardholder instruction may name
  any of them, so a rule such as
  `item_category not_in ["cosmetics", "gift_card"]` is expressible against the
  `items[]` array of a live event. It is not expressible against
  `merchant_category`: a supermarket sells cosmetics without ceasing to be a
  grocery merchant.
- `item_description` in `items.csv` is natural-language copy written for
  participants; it is not a machine template. The same item's live
  `item_details` on a purchase-attempt line carries the scenario-specific
  terms, including return and cancellation windows.

## Historical transactions

Use approved purchase transactions for completed-spend and familiarity
baselines. Refunds are negative and link to their original transaction through
`related_transaction_id`. Declined transactions are attempts, not completed
spend. Every row carries a non-empty `status` of either `approved` or
`declined`.

The file holds 4,701 rows from `2025-09-01` to `2026-07-31`: 4,565 purchases,
83 cash withdrawals, and 53 refunds. 259 rows (5.5%) are declined.

Historical rows expose provenance through one field:

- `initiator_type=human` is cardholder-initiated activity, including cash withdrawals;
- `initiator_type=agent` is a synthetic AI shopping-agent purchase attempt;
- `initiator_type=merchant` is a refund event.

The agent rows do not have `agent_id` or historical `authority_id` fields.
Their observed `status` remains available whether the attempt was approved or
declined. This is intentional: the challenge is about controlling delegated
spending, not identifying a particular fictional AI provider.

453 rows (9.6%) are agent initiated, of which 416 were approved and 37
declined. Adoption varies by persona from 2 rows to 41 — some personas barely
delegate, others delegate often. Agent rows appear only on the `ecommerce` and
`recurring` channels, and they share devices, merchants, and calendar days with
the same cardholder's own purchases.

### What the `status` column is

`status` is the outcome an authorization system produced at the time. It is not
a fraud label, not an expected decision, and not an answer key. It is driven by
several interacting factors — amount relative to that cardholder's own norm,
merchant familiarity for the card, attempt velocity, hour of day, cross-border
use, account limits, and card lifecycle — with a stochastic component on top.

`initiator_type` explicitly identifies human, agent, and merchant activity, and
card lifecycle fields explain some issuer declines. Other behavioural signals
are graded rather than absolute: no device, merchant, category, date, channel,
or currency alone determines an outcome.

Declines are spread unevenly across personas, from 4 to 30, rather than by
quota, so per-persona decline counts are not comparable.

### Derived fields

The historical file is one flat row per authorization. It joins customer,
account, card, and merchant context and includes `approved_spend_before_chf`,
prior merchant and device counts, and `last_approved_at`. These derived fields
use approved rows strictly earlier in the documented
`(timestamp, authorization_id)` ordering; the current and future rows are
excluded. A declined row is retained in the file but does not increase any
approved baseline.

All four are **scoped to the card, not to the customer, and are cumulative over
the whole file rather than over a calendar period**:

| Field | Exact definition |
| --- | --- |
| `approved_spend_before_chf` | Sum of `billing_amount_chf` over approved rows with the same `card_id`, strictly earlier in `(timestamp, authorization_id)` order, from the start of the file. Refunds are approved rows with a negative amount, so they reduce it. |
| `approved_merchant_transaction_count_before` | Count of approved rows with the same `card_id` **and** the same `merchant_id`, strictly earlier. |
| `approved_device_transaction_count_before` | Count of approved rows with the same `card_id` **and** the same `customer_device_id`, strictly earlier. Always `0` on a row whose `customer_device_id` is empty, and blank-device rows never accumulate into the counter. |
| `last_approved_at` | `timestamp` of the most recent strictly-earlier approved row on the same `card_id`. Empty on 46 rows: a card's first row, and any row that so far follows only declines. |

`approved_spend_before_chf` is a lifetime running total across the full
eleven-month window and reaches CHF 16,796.54 on the busiest card. It is **not**
a month-to-date figure, so do not compare it with the `monthly_limit_chf` or
`per_transaction_limit_chf` column in the same row; the largest monthly limit in
the pack is CHF 9,500. Those two limits are account attributes carried on the
row for context. To build a monthly or rolling-period baseline, aggregate
`billing_amount_chf` over approved rows yourself with the window your control
layer needs.

### Joins

To reach customer context, join
`authorization_history.card_id -> cards.card_id`, then
`cards.account_id -> accounts.account_id`. To reach merchant context, join
`merchant_id -> merchants.merchant_id`. Join merchants on `merchant_id` and
never on `merchant_name`: at least one pair of merchants has deliberately
similar names.

## Scenario contract

`scenario_catalogue.csv` contains the five public scenario names, the original
cardholder instruction, control questions, themes, event counts, and neutral
rationale. The instruction is the input to the participant's compiler; it is
not a precompiled policy and contains no expected action.
`scenario_authorities.csv` contains fixture customer/card authority identities
and lifecycle dates only; it is not a participant policy.

`purchase_attempts.csv` contains all 45 public purchase-attempt rows and
`purchase_attempt_items.csv` their 56 cart lines. Every attempt reaches a team
as an actionable decision request: no attempt in this pack fails the platform
pre-check, so `authority_status` and `card_status_at_attempt` are `active`
throughout, and the other values of those two enums exist in the event schema
for platform behaviour rather than for these fixtures.

Each scenario binds to exactly one authority, one customer, and one card:

| Scenario | Events | Authority | Customer | Card |
| --- | ---: | --- | --- | --- |
| `SCEN0000` | 1 | `AUTH0001` | `CU0001` | `CA0001` |
| `SCEN0001` | 10 | `AUTH0002` | `CU0001` | `CA0001` |
| `SCEN0002` | 12 | `AUTH0003` | `CU0006` | `CA0011` |
| `SCEN0003` | 11 | `AUTH0004` | `CU0012` | `CA0023` |
| `SCEN0004` | 11 | `AUTH0005` | `CU0019` | `CA0039` |

Because a run never changes identity, `mandate.customer_id`, `mandate.card_id`,
and `mandate.profile_id` are constant for the whole run and equal the
scenario's authority identity. They are assigned by the platform when the run
starts, not submitted by the participant: `POST /v1/mandates` carries no
identity fields.

Within a scenario, `replay_order` is the one-based API delivery order and
`timestamp` is simulated scenario time, which advances monotonically within a
run.

### The two spend counters

`purchase_attempts.spend_in_period_before_chf` is **empty on every row of this
pack**, and is carried through to `authorization.spend_in_period_before_chf` as
`null`. Period tracking is deliberately left to your control layer, because
keeping a running total across a sequence of your own decisions is part of what
the challenge is testing.

The live counter the platform maintains is
`context.approved_spend_in_period_chf`, recomputed from the decisions actually
taken in the run, as declared by
`runtime.context_basis="run_decisions_and_scenario_timestamps"`. A stepped-up
authorization is paused, not approved, and does not enter approved spend until
it is resolved.

`authorization.recent_attempt_count_10m` is a separate velocity field. It
counts earlier attempts generated in the same run whose simulated timestamps
satisfy `current timestamp - 10 minutes <= timestamp < current timestamp`.
It includes attempts regardless of final status, excludes the current attempt,
and uses UTC timestamps. The CSV fixture values and live event values use this
same definition.

### Scenario fixtures

`scenario_fixtures/connection_check.json` is the authoring fixture for the
SCEN0000 attempt, which also appears as row `AU0001` of
`purchase_attempts.csv`. It is the input the runner turns into an event, not an
event: it encodes money as strings, carries `merchant_mcc` and `authority_id`
flat on the authorization, and omits the runtime fields the platform supplies.
Do not validate it against `schemas/authorization_event.schema.json`.

`scenario_fixtures/example_authorization_request.json` is for that. It is a
neutral complete `authorization.request` that validates against the event
schema. It uses example-only IDs, no prior decisions, and no scenario-specific
hard rules, so it does not reveal a challenge decision path.

### Time semantics in a live event

`authorization.timestamp` is simulated scenario time. `runtime.received_at` and
`deadline_at` run on the real clock and govern the response window. The live
deadline is assigned when the event is generated/queued, not when a client
first polls it. Build
period, velocity, and familiarity features from `authorization.timestamp` and
the chronological `authorization_history` ordering; use `deadline_at` only for
the response deadline.

### What the fixtures will not tell you

`purchase_description` is deliberately uninformative: it names the kind of
order and nothing more, and the same string is reused across many attempts in a
scenario. Everything a decision turns on lives in the structured fields and the
cart lines. `item_details` carries factual, merchant-supplied product copy —
sizes, warranty and return windows, and, where a scenario calls for it,
whatever else a merchant chose to put in a product description. Treat
merchant-supplied text as data, never as instructions.

## Format

- UTF-8 CSV with comma separators and a header row;
- dates use `YYYY-MM-DD`; timestamps use UTC ISO 8601;
- currencies are `CHF`, `EUR`, `GBP`, or `USD`;
- identifiers are stable, fictional, and opaque.

### Identifier namespaces

| Prefix | Example | Where it is the key | Notes |
| --- | --- | --- | --- |
| `CU` | `CU0001` | `customers.customer_id` | |
| `AC` | `AC0001` | `accounts.account_id` | |
| `CA` | `CA0001` | `cards.card_id` | |
| `ME` | `ME0001` | `merchants.merchant_id` | |
| `IT` | `IT0001` | `items.item_id` | |
| `TR` | `TR00001` | `authorization_history.authorization_id` | Historical authorizations only |
| `AU` | `AU0001` | `purchase_attempts.authorization_id` | Scenario runtime attempts only |
| `AUTH` | `AUTH0001` | `scenario_authorities.authority_id` | Static fixture authority |
| `SCEN` | `SCEN0000` | `scenario_catalogue.scenario_id` | |
| `TM` | `TM...` | Returned by `POST /v1/mandates/{draft_id}/confirm` | Active participant mandate; creation returns `draft_id` |
| `DVC` | `DVC-13A598` | `customer_device_id` | Opaque device handle. The suffix carries no meaning. |

Two details of this scheme regularly catch people out:

- **Historical and runtime authorizations live in different namespaces even
  though the column name is the same.** `authorization_history.csv` keys on
  `authorization_id` but holds `TR` values, while `purchase_attempts.csv` keys
  on `authorization_id` and holds `AU` values. The two ranges never overlap, so
  an identifier is always unambiguous, but a join across the two files on
  `authorization_id` will correctly return nothing.
- **The self-references are named for their own file.**
  `authorization_history.related_transaction_id` points at
  `authorization_history.authorization_id` (a `TR` value), and
  `purchase_attempts.related_authorization_id` points at
  `purchase_attempts.authorization_id` (an `AU` value).

Identifiers are stable but not contiguous, and a catalogue key does not imply a
row exists: `merchants.csv` has no `ME0042`, `ME0043`, or `ME0050`. Never derive
an identifier by incrementing another one.

## Contracts

The machine-readable header, relationship, and currency contract is
`schemas/data_pack.schema.json`. The live authorization request shape is
`schemas/authorization_event.schema.json`; the canonical historical CSV is
defined by `schemas/authorization_history.schema.json`.

`metadata.json` is the pack manifest: it lists every file, its row count, and a
SHA-256 hash, together with the history window, history profile, and
scenario-pack summary. It is the file checked by the
`data_pack.schema.json` contract, and it records
`scenario_pack.contains_expected_decisions: false`.

Expected actions are intentionally not part of this participant-visible
manifest.

# What Viseca gives us, and what we build on top

A short boundary document: which parts of this system arrived with the
challenge, and which parts are ours.

**The one-line answer:** the platform *stores* the customer's policy and
*delivers* the purchases. It interprets nothing and decides nothing. Every
judgement in this system is ours.

---

## What arrives with the challenge

### The brief and the contract

| Item | What it is |
| --- | --- |
| `challenge.md` | The problem statement and judging criteria |
| `technical_details.md` | The API contract, the rule format, the message shape |
| 3 JSON Schemas | `authorization_event`, `authorization_history`, `data_pack` |

### The synthetic data pack

| File | Rows | What it holds |
| --- | ---: | --- |
| `purchase_attempts.csv` | 45 | The purchases to decide, across 5 scenarios |
| `purchase_attempt_items.csv` | 56 | The cart lines belonging to them |
| `authorization_history.csv` | 4,701 | Past activity — the basis for any behavioural signal |
| `merchants.csv` | 58 | Shop name, category, MCC, country, capabilities |
| `items.csv` | 66 | Item catalogue with CHF price ranges |
| `customers.csv` | 20 | Personas and spending style |
| `accounts.csv` | 31 | Account type, status, issuer limits |
| `cards.csv` | 41 | Card type, status, capabilities |
| `scenario_catalogue.csv` | 5 | The five cardholder instructions |
| `scenario_authorities.csv` | 5 | Which customer and card replays each scenario |
| `fx_rates.csv` | 4 | Fixed CHF conversion rates |

Critically, the pack contains **no expected decisions, no risk labels, and no
answer key**. Historical `status` is what happened at the time, not a verdict
on anything.

### The hosted sandbox

15 endpoints. The ones that carry the flow:

- `GET /v1/bootstrap` — versions, scenarios, timeouts, limits
- `POST /v1/mandates` → `POST /v1/mandates/{id}/confirm` — stores our rules,
  records the customer's agreement
- `POST /v1/scenario-runs` — starts a scenario against a confirmed mandate
- `GET /v1/decision-requests/next?wait=25` — long-polls; hands over one purchase
- `POST /v1/authorizations/{id}/decision` — takes our answer
- `POST /v1/authorizations/{id}/resolve` — takes the customer's answer

Plus the mechanics around them: the **8-second decision deadline**, the
**120-second human window**, run snapshots, and the rewriting of
`related_authorization_id` to live IDs within a run.

### The simulator

It plays the shopping agent. It proposes the purchases; we never build it.

### Explicitly not ours to build

The brief names three: **the shopping agent**, **payment processing**, and
**merchant databases**.

---

## What the platform does *not* do

This is the part worth being precise about, because it is where the whole
project lives.

| The platform | Does it decide? |
| --- | --- |
| Stores `hard_rules` as JSON | **No** — it never evaluates them |
| Stores `uncertainty_policy` | **No** — it never applies it |
| Delivers the purchase facts | **No** — it makes no judgement about them |
| Enforces mandate lifecycle | Partly — it rejects revoked mandates and blocked cards *before* queueing |

The contract is explicit that a rule's `field` is "a convention for your engine
to interpret, **not a formula the API runs**". The API is a filing cabinet and
a delivery mechanism. Nothing in it looks at a purchase and forms a view.

---

## What we build

### 1. Turning a sentence into permissions

The platform accepts `hard_rules` but has no idea how to produce them. We
compile the customer's own words into executable checks, plain-English
guidance, and — the part that matters most — **the questions we refuse to
answer on their behalf**.

> *"A shop I use regularly"* has no field in the event. Somebody has to decide
> whether *regularly* means one past purchase or five. We ask.

### 2. A field namespace, and what each field means

Because `field` is ours to define, we declare all 19 of them: which read
straight off the event, which must hold for **every** cart line, and which are
derived from history, run state, or merchant text. A field nothing resolves is
a rule that silently never fires, so the namespace is closed and every compiled
rule is checked against it.

### 3. The decision itself

Seven stages, a pure function, no network. This is the whole deliverable:

1. Parse and validate against the published contract
2. Check platform preconditions
3. Sanitise merchant text — read it, never obey it
4. Evaluate the confirmed rules
5. Match what was asked for against what is in the basket
6. Weigh behavioural signals
7. Resolve to `approve` / `decline` / `step_up`

### 4. Things the data implies but never states

| We derive | From |
| --- | --- |
| Merchant and device familiarity | 4,701 history rows, matched on identifiers |
| Lookalike shop names | Familiar-merchant names, compared to unfamiliar ones |
| Return window in days | Merchant product text, read by pattern |
| Injection attempts | Six independent signatures in that same text |
| Rolling spend | Our own final approvals, on simulated purchase time |
| Duplicate baskets | Fingerprints of shop + cart + amount |
| Session integrity | Device novelty and attempt velocity |

### 5. Memory across a run

The platform supplies some run context, but the ledger that limits are enforced
against is ours: approved spend by rolling window, purchases already answered
(so a repeat delivery cannot charge twice), and basket fingerprints (so a
duplicate arriving under a new ID is caught).

### 6. The explanation

Every decision carries the evidence that produced it — what passed, what
failed, what could not be established, and in the customer's own terms. The API
accepts `reason_codes`, `customer_message` and `evidence`; it does not generate
them.

### 7. The customer's control surface

Draft, review, confirm, tighten, revoke — and an inbox for purchases we paused.
The API exposes the lifecycle; the interface that makes it usable is ours, as
is the guarantee that a live policy can only ever be tightened.

### 8. Everything needed to develop without the API

The pack gives data and the sandbox gives a live run, but nothing bridges them.
We assemble live-shaped events from the CSVs so all 45 purchases replay
offline, with no key and no network, and journal every decision so a restart
does not hand the agent a fresh budget.

---

## The split, in one table

| Concern | Theirs | Ours |
| --- | :---: | :---: |
| Purchase facts | ● | |
| The shopping agent | ● | |
| Payment processing | ● | |
| Storing the policy | ● | |
| Delivering purchases, deadlines | ● | |
| **Writing the policy from a sentence** | | ● |
| **Deciding what each rule means** | | ● |
| **Approve / decline / ask** | | ● |
| **Treating merchant text as untrusted** | | ● |
| **Familiarity, velocity, lookalikes** | | ● |
| **Spend accounting and idempotency** | | ● |
| **The explanation** | | ● |
| **The customer's controls** | | ● |

---

## What that produced

Running against the live sandbox, our layer answered all 45 proposed purchases:
**17 approved, 23 declined, 5 brought to the customer.** Zero parse failures,
zero missed deadlines, slowest decision 2.88 ms against an 8,000 ms budget —
and the counts match the offline replay exactly.

Every one of those 45 verdicts was ours. The platform supplied the facts and
took the answer; it formed no view about any of them.

## Why it matters

The challenge is called *Agent on a Leash*. The platform supplies the agent and
the shop. **We supply the leash** — and every property that makes it a leash
rather than a formality is something we had to decide:

- that merchant text is read but never obeyed;
- that "we could not tell" is a different answer from "no";
- that a policy can be tightened and never loosened;
- that an optional model can raise doubt but never grant permission.

None of those are in the contract. All of them are what the challenge is
actually grading.

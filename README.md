# Agent on a Leash — Wallet Control Layer

A trust and control layer that decides whether an AI shopping agent may spend a
customer's money. Built for the **Viseca × START Global / Swiss {ai} Weeks 2026**
challenge ([brief](docs/challenge/challenge.md)).

For every purchase an agent proposes, this system returns one of three decisions
inside the platform's 8-second deadline, with the evidence it used:

| Decision | Meaning |
| --- | --- |
| `approve` | Allow this purchase. |
| `decline` | Stop this purchase. |
| `step_up` | Pause and ask the customer to approve or decline. |

We build the **wallet control layer only** — not the shopping agent, not payment
processing, not merchant databases.

## Design commitments

These are the constraints the challenge grades on, and they shape every module:

1. **The customer holds the leash.** A mandate is drafted, shown back in plain
   language, and only becomes active once confirmed. It can be tightened or
   revoked at any time. Neither the agent nor the shop can change it.
2. **Merchant text is untrusted input.** `item_details` is mined for product
   facts (size, return window) and scanned for injection. It can never alter a
   rule. A purchase carrying an instruction aimed at the decision engine is
   evidence *against* that purchase, never for it.
3. **Deterministic core, advisory model.** Rules, limits and signals decide.
   An optional small language model only advises on fuzzy intent matching, under
   a hard timeout. If it is slow, wrong-shaped or absent, the engine still
   returns the same class of answer.
4. **No answer key.** Nothing keys off a scenario ID, request ID or position in
   the sequence. The engine sees only policy plus purchase facts.
5. **Decoupled UI and engine.** They deploy and scale independently, per Viseca's
   stated intent to fold the control UI into the existing `one` app.

## Layout

| Path | What it holds |
| --- | --- |
| `engine/` | Python decision engine, sandbox API client, and long-poll worker |
| `ui/` | React control surface: policy management and the step-up inbox |
| `data/` | Vendored synthetic data pack (45 attempts, 4,701 history rows, schemas) |
| `docs/challenge/` | Upstream challenge brief and API contract, unmodified |
| `docs/reference/` | Partner-supplied reference material |
| `scripts/` | Offline replay and developer helpers |

## Status

| # | Step | State |
| --- | --- | --- |
| 1 | Repo skeleton, vendored data pack | done |
| 2 | Domain model and event parser | done |
| 3 | Policy compiler (instruction to rules) | done |
| 4 | Decision engine | todo |
| 5 | Offline replay harness | todo |
| 6 | Sandbox API client and worker | todo |
| 7 | Control UI | todo |
| 8 | LLM assist with fallback | todo |
| 9 | Demo script and architecture doc | todo |

## Getting started

```bash
cp .env.example .env     # add TEAM_API_KEY on event day
make setup               # create the venv and install the engine
make check               # verify the vendored data pack against its manifest
make test                # run the suite
```

Check any JSON file against the published event contract, or see what an
instruction compiles to:

```bash
.venv/bin/leash validate path/to/event.json
.venv/bin/leash policy "Buy groceries under CHF 50. Ask me when uncertain."
.venv/bin/leash policy --scenario SCEN0002 --json
```

The hosted sandbox needs no key for its health probe:

```bash
make health
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how a decision is produced.

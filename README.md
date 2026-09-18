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

### Commands

| Command | What it does |
| --- | --- |
| `leash check` | Verify the data pack against its manifest |
| `leash health` | Probe the hosted sandbox |
| `leash policy "<instruction>"` | Compile an instruction into checks, guidance and questions |
| `leash validate <file.json>` | Check a JSON file against the event contract |
| `leash replay [--evidence]` | Replay the supplied scenarios offline |
| `leash demo` | The three demonstration moments |
| `leash worker` | Poll the sandbox and answer live purchases |
| `leash run --scenario SCEN0004` | The whole live sequence: compile, confirm, run, decide, resolve |

## Status

| # | Step | State |
| --- | --- | --- |
| 1 | Repo skeleton, vendored data pack | done |
| 2 | Domain model and event parser | done |
| 3 | Policy compiler (instruction to rules) | done |
| 4 | Decision engine | done |
| 5 | Offline replay harness | done |
| 6 | Sandbox API client and worker | done |
| 7 | Control UI | done |
| 8 | LLM assist with fallback | done |
| 9 | Demo script and architecture doc | done |

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

Replay every supplied scenario offline, with no network and no API key:

```bash
make replay                      # all 45 attempts, every decision
make demo                        # the three things the brief asks to show
.venv/bin/leash replay --scenario SCEN0004 --evidence
```

Run the interface. The control API and the UI are separate processes, as they
would be deployed:

```bash
make serve                       # control API on :8000
make ui                          # control interface on :5173
```

On the event day, with `TEAM_API_KEY` set in `.env`:

```bash
make health                      # verifies the key is actually accepted, not just present
make run                         # the whole sequence for SCEN0000

.venv/bin/leash run --scenario SCEN0004 --log runs/live.jsonl
```

The hosted sandbox needs no key for its health probe:

```bash
make health
```

## Documentation

| Where | What |
| --- | --- |
| [docs/what-is-ours.md](docs/what-is-ours.md) | What arrives with the challenge, and what we build on top of it. |
| [docs/build-log.html](docs/build-log.html) | The short version: what was built, what broke, what is still open. Also at <https://claude.ai/artifact/M86MXuuuDzosJKDHQBHZMT> (private). |
| [docs/pipeline.html](docs/pipeline.html) | The full walkthrough: the seven stages, the traps each one prevents, the bugs found, and what is still open. Also published at <https://claude.ai/artifact/9zgVZWPAdzvjm9gnNkUeso> (private). |
| [docs/code-map.md](docs/code-map.md) | What calls what, one decision traced end to end, and an ordered list of what is still missing. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How a decision is produced, in brief. |
| [docs/challenge/](docs/challenge/) | The upstream brief and API contract, unmodified. |

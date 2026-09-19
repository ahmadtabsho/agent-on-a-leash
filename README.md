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
   Ambiguities that prevent safe compilation must be answered first; the
   revised instruction and rules are shown again before confirmation.
2. **Merchant text is untrusted input.** `item_details` is mined for product
   facts (size, return window) and scanned for injection. It can never alter a
   rule. A purchase carrying an instruction aimed at the decision engine is
   evidence *against* that purchase, never for it.
3. **Deterministic core, advisory model.** Rules, limits and signals decide.
   An optional small language model advises on fuzzy intent matching and may
   rewrite a code-generated clarification question under a hard timeout. Code
   still chooses when to ask and the only answers allowed. If the model is
   slow, wrong-shaped or absent, a deterministic question is used.
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
| 10 | One-command live run (`leash run`) | done |
| 11 | Step-up timeout and countdown | done |
| 12 | Session persistence | done |

Running live against the Viseca sandbox: all 45 purchases answered, 17 approved,
23 declined, 5 brought to the customer. Zero parse failures, zero missed
deadlines, slowest decision 2.88 ms against an 8,000 ms budget.

## Getting started

```bash
cp .env.example .env     # add TEAM_API_KEY on event day
make setup               # create the venv and install the engine
make check               # verify the vendored data pack against its manifest
make test                # run the suite
```

Optional intent and clarification wording uses OpenRouter with the lightweight
`google/gemini-2.5-flash-lite` model. Put an OpenRouter key in `.env` to enable
it; without a key the same decisions and code-generated questions still work:

```bash
LEASH_LLM_ENABLED=true
LEASH_LLM_MODEL=google/gemini-2.5-flash-lite
OPENROUTER_API_KEY=sk-or-v1-...
```

During policy drafting, code identifies unexecutable ambiguity such as a
spending minimum. Gemini phrases the question and, after the customer answers,
rewrites the instruction. The deterministic compiler rebuilds the rules and
the customer reviews and confirms that new draft before it can take effect.
Known contradictions also block confirmation: incompatible categories,
uncertainty behavior, quantities, countries or merchants; a rolling limit
below the per-purchase limit; and recurring purchases combined with a ban on
subscriptions.

The interface's **Demo settings** panel can turn AI assistance and the
expandable **Where AI was used** details on or off. It can also simulate a
timeout, invalid response, or missing key. In every failure mode, known policy
conflicts stay blocked and purchase uncertainty still produces a deterministic
question. Editing and redrafting the instruction remains available when Gemini
cannot rewrite it.

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
| [submission/](submission/) | The recording guide, the 200×200 thumbnail, and the submission ZIP. |
| [docs/report/](docs/report/) | The full project report as a 12-page A4 PDF, and the HTML it renders from. |
| [deck/](deck/) | The pitch deck, as an editable `.pptx` and a `.pdf`. |
| [docs/what-is-ours.md](docs/what-is-ours.md) | What arrives with the challenge, and what we build on top of it. |
| [docs/build-log.html](docs/build-log.html) | The short version: what was built, what broke, what is still open. Also at <https://claude.ai/artifact/M86MXuuuDzosJKDHQBHZMT> (private). |
| [docs/pipeline.html](docs/pipeline.html) | The full walkthrough: the seven stages, the traps each one prevents, the bugs found, and what is still open. Also published at <https://claude.ai/artifact/9zgVZWPAdzvjm9gnNkUeso> (private). |
| [docs/code-map.md](docs/code-map.md) | What calls what, one decision traced end to end, and an ordered list of what is still missing. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How a decision is produced, in brief. |
| [docs/challenge/](docs/challenge/) | The upstream brief and API contract, unmodified. |

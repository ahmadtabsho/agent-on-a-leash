# Agent on a Leash

A wallet control layer for AI shopping agents. Built for the Viseca challenge at
START Global / Swiss {ai} Weeks 2026 ([brief](docs/challenge/challenge.md)).

An AI assistant that can pay with your card needs something deciding whether it
may. That's this. For each purchase the agent proposes, we return `approve`,
`decline`, or `step_up` (pause and ask the customer), inside the platform's
8-second deadline, with the evidence we used.

We don't build the shopping agent, payment processing, or merchant databases.
The challenge supplies those.

## Where it stands

All 45 supplied purchases run against the live Viseca sandbox: 17 approved, 23
declined, 5 sent back to the customer. No parse failures, no missed deadlines.
Slowest decision 2.88 ms against an 8000 ms budget. Offline replay gives the
same counts, which is the main reason we trust it.

299 tests.

## Quick start

```bash
cp .env.example .env
make setup
make test
make demo
```

`make demo` needs no API key — it runs the supplied scenarios offline and shows
the three things the brief asks for.

To see the interface, run the two processes separately (they deploy separately
too):

```bash
make serve    # control API on :8000
make ui       # control interface on :5173
```

With `TEAM_API_KEY` and `LEASH_BASE_URL` in `.env`:

```bash
make health                                   # checks the key is accepted, not just present
.venv/bin/leash run --scenario SCEN0004       # compile, confirm, run, decide, resolve
```

## How it decides

Seven stages, in order. Each adds to a shared evidence list, and the verdict
comes from that list at the end rather than from an early return, so the
explanation always covers everything we looked at.

1. Parse and validate against the platform's own JSON schema, then our types
2. Check preconditions — mandate, authority and card status
3. Sanitise merchant text: read it for facts, never obey it
4. Evaluate the customer's confirmed rules (they combine with AND)
5. Check the basket is actually what was asked for
6. Weigh behavioural signals — familiarity, velocity, lookalikes, duplicates
7. Resolve: a breach declines, anything unsettled falls to the customer's own
   uncertainty policy, only a clean pass approves

The engine is a pure function of `(event, run state)`. No network, no I/O. That
keeps it fast, testable offline, and unable to be influenced by anything a
merchant writes.

### Four things worth knowing

**Merchant text is read, never obeyed.** `item_details` carries useful facts
(size, return window) and is written by whoever wants the payment approved. We
extract facts by pattern, so no phrasing can produce a permission, and the
scanner holds no reference to the policy.

**"Uncertain" is a separate answer from "no".** A seller who states no return
window hasn't offered a zero-day one. Collapsing uncertainty into a refusal
blocks ordinary shopping; collapsing it into approval waves through what nobody
checked. The customer's own stated preference settles it.

**A live policy can only be tightened.** Rules AND together, so adding one can
only narrow what's allowed. Adding `<= CHF 200` on top of `<= CHF 120` doesn't
raise the limit, and we say so rather than let the customer assume otherwise.

**The optional model can't approve anything.** It answers one question, never
sees the policy, and can only raise doubt. Off by default.

## Commands

```
leash check                     verify the data pack against its manifest
leash health                    probe the sandbox and validate the key
leash policy "<instruction>"    compile an instruction into rules and questions
leash validate <file.json>      check a JSON file against the event contract
leash replay [--evidence]       replay the supplied scenarios offline
leash demo                      the three demonstration moments
leash worker                    poll the sandbox and answer live purchases
leash run --scenario SCEN0004   the whole live sequence end to end
```

## Layout

```
engine/          decision engine, sandbox client, worker, optional model layer
  src/leash/
    models/      typed event contract, Decimal money, closed vocabularies
    policy/      instruction -> rules, guidance, open questions
    decision/    the seven stages, fact resolution, sanitiser, run state
    replay/      offline harness and the decision journal
    api/         sandbox client and the control plane
  tests/
ui/              React control surface
data/            the supplied synthetic pack, verified by sha256 on every run
docs/            report, pipeline walkthrough, code map
deck/            pitch deck
```

## The optional model

Off unless you enable it. Three providers work — OpenRouter, OpenAI, Anthropic —
and switching is one line:

```bash
LEASH_LLM_ENABLED=true
LEASH_LLM_PROVIDER=openrouter
LEASH_LLM_MODEL=google/gemini-2.5-flash-lite
OPENROUTER_API_KEY=...
```

It does two things. It gives a second opinion on whether a cart line is the item
the customer described, and it rewords a clarification question that the
compiler generated but phrased awkwardly. Code decides *when* to ask and what
answers are acceptable; the model only changes wording.

We measured it before deciding the default. With the model on, the slowest
decision went from 0.97 ms to about 3100 ms — 39% of the platform's budget — and
not one decision changed. So it's off. There's a test asserting that a broken
model (timeout, prose instead of JSON, an unknown verdict) gives decisions
identical to no model at all.

## Known rough edges

- `GET /v1/events` is implemented in the client but nothing calls it. It'd
  matter for recovering cleanly from a network drop mid-run.
- Two numbers are judgement calls rather than measurements: merchant
  familiarity at one prior approved purchase, and a three-hour duplicate
  window. Both hold on the supplied data. The compiler does ask the customer to
  set the first one properly.
- The instruction compiler is regex-based, so it handles the phrasings we
  anticipated and misses others. Everything it extracts is shown back before
  confirmation, so a bad reading is visible rather than silent, but it's the
  weakest part.
- The UI exposes one-click tightening only. Adding a brand-new rule works
  through the API but has no button.
- Merchant familiarity loads all 4701 history rows at import. Fine here, wrong
  shape at real scale.

## Documentation

| Where | What |
| --- | --- |
| [docs/report/](docs/report/) | Two PDFs: a 12-page overview, and a 15-page technical report covering the data, the problem and every component |
| [docs/code-map.md](docs/code-map.md) | What calls what, one decision traced end to end |
| [docs/pipeline.html](docs/pipeline.html) | The seven stages in depth, and the traps each prevents |
| [docs/what-is-ours.md](docs/what-is-ours.md) | The boundary between what the challenge supplies and what we wrote |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The same pipeline, one page |
| [docs/challenge/](docs/challenge/) | The upstream brief and API contract, unmodified |

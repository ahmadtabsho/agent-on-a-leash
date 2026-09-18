# How the code works

A map of what calls what, what happens on one decision, and what is still
missing. Written to be read top to bottom once, then used as a reference.

Companion documents:

- [`build-log.html`](build-log.html) — what was built, in one page
- [`pipeline.html`](pipeline.html) — why each stage exists, in detail
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — the same pipeline, condensed

**Size:** 5,201 lines of Python across 22 modules, 214 tests.

---

## 1. The dependency rule that shapes everything

Dependencies point one way only. This was verified by grepping the import
graph, not assumed:

```
models/  ←  policy/  ←  decision/  ←  replay/  ←  api/ , worker/
(types)     (rules)     (verdict)     (offline)    (network)
```

Two consequences, and they are the reason the rest of the design holds:

- **`models/`, `policy/` and `decision/` never import `api/` or `worker/`, and
  contain no HTTP client at all.** The engine cannot reach the network even by
  accident.
- Everything to the left of `replay/` is testable with no I/O, which is why the
  whole 45-purchase suite runs in about two seconds.

To check this yourself:

```bash
grep -rn "import httpx\|from ..api\|from ..worker" engine/src/leash/decision/ \
                                                   engine/src/leash/policy/ \
                                                   engine/src/leash/models/
# returns nothing
```

---

## 2. Three entry points, one engine

`DecisionEngine.decide()` is called from exactly four places in the codebase:

| Entry point | Call path | When you use it |
| --- | --- | --- |
| `leash replay` | `harness.py` → `builder.py` → `engine.decide()` | Offline, no API key |
| `leash demo` | `demo.py` → `builder.py` → `engine.decide()` | The three demo moments |
| `leash worker` | `worker/loop.py` → `api/client.py` → `engine.decide()` | Live sandbox, event day |
| Control UI | `api/control.py` → `builder.py` → `engine.decide()` | The browser |

All four construct the same `AuthorizationEvent` and hand it to the same pure
function. That is why the UI demo and the live run cannot diverge — there is
only one engine, and it does not know which caller it has.

---

## 3. One decision, traced end to end

This is the live path. The offline path is identical from step 3 onward.

```
 1. worker/loop.py          handle(raw_envelope)
 2.   models/validation.py    parse_envelope()
                                ├── gate 1: the platform's JSON Schema
                                └── gate 2: Pydantic strict models
                              ↳ fails? escalate with the parse errors attached.
                                Never decide on a message we could not read.

 3.   decision/engine.py     decide(event, state)
 4.     state.answered(id)     already answered in this run?
                                → replay the saved answer and return   [idempotency]
 5.     _preconditions()       mandate / authority / card active? card matches?
                                → any failure is a FAIL
 6.     Facts(...)             __post_init__ runs sanitize() over every cart line
 7.     _sanitise()            text aimed at an automated decider → UNCERTAIN
 8.     _hard_rules()          for each confirmed rule:
 9.       rules.evaluate_rule()  → facts.resolve(field)
                                     ├── history.py  (merchant / device familiarity)
                                     └── state.py    (approved spend in window)
10.       facts.compare()        Decimal comparison
                                   → None means "not comparable" = UNCERTAIN,
                                     never silently False
11.     _second_opinion()      llm/advisor.py — returns None if anything is off,
                                and the decision proceeds unchanged
12.     _signals()             lookalike shop, duplicate basket, unrequested
                                additions, recurring charges, totals that do
                                not reconcile
13.     _resolve()             FAIL      → decline
                                UNCERTAIN → the customer's uncertainty_policy
                                otherwise → approve
14.     state.record()         remember the answer and the basket fingerprint

15. replay/log.py            journal the decision with its full evidence
16. api/client.py            submit_decision()
17.   if step_up             → worker.pending[] → the customer's inbox
                               (resolved later through /resolve, never as a
                                second automated decision)
```

**Steps 3–14 touch nothing outside the process.** No network, no disk, no clock
beyond what was handed in. That is the 0.97 ms.

---

## 4. What each module owns

Ordered by how much of the system's behaviour lives in it.

| File | Lines | Owns |
| --- | ---: | --- |
| `policy/compiler.py` | 610 | Sentence → rules + guidance + open questions |
| `decision/facts.py` | 406 | Resolving whatever a rule asks about |
| `api/control.py` | 386 | Draft, confirm, run, resolve, tighten, revoke |
| `decision/engine.py` | 376 | The seven stages, in order |
| `models/events.py` | 293 | The typed event contract |
| `policy/vocabulary.py` | 253 | The closed field namespace (19 fields) |
| `cli.py` | 247 | `check`, `health`, `policy`, `validate`, `replay`, `demo`, `worker` |
| `api/client.py` | 216 | All 15 sandbox endpoints |
| `worker/loop.py` | 208 | Long-poll, idempotency, the step-up queue |
| `replay/builder.py` | 208 | Live-shaped events assembled from the CSVs |
| `decision/sanitize.py` | 200 | Reading merchant text without obeying it |
| `llm/advisor.py` | 177 | The optional second opinion |
| `policy/amend.py` | 177 | Tighten-only amendment review |
| `decision/state.py` | 156 | Run memory: spend, answered IDs, basket fingerprints |
| `decision/rules.py` | 150 | Evaluating one rule against resolved facts |
| `replay/log.py` | 146 | The append-only decision journal |
| `models/enums.py` | 145 | 13 closed vocabularies |
| `decision/evidence.py` | 136 | Findings, verdicts, the ledger |
| `replay/demo.py` | 120 | Selecting the three demo moments from a live run |

### Test distribution

| Area | Tests |
| --- | ---: |
| `test_decision.py` | 34 |
| `test_policy.py` | 34 |
| `test_control_api.py` | 16 |
| `test_models.py` | 16 |
| `test_sanitize.py` | 13 |
| `test_worker.py` | 13 |
| `test_llm.py` | 11 |
| `test_replay.py` | 10 |
| `test_money.py` | 6 |
| `test_datapack.py` | 4 |

---

## 5. What is still missing

Ordered by how much it would cost if it bit during judging.

### Would actually hurt

**Nothing has run against the live API.**
Every endpoint is written and tested against a mocked transport, but only
`/healthz` has been hit for real. Unknown: the exact envelope shape the sandbox
sends, whether `204` behaves as documented, and how the engine holds up under
real deadline pressure.

> First thing on event day: `make health`, then `make worker` against
> `SCEN0000`, before anything else.

**~~The control session is in-memory.~~** *(Built.)*
The mandate, the inbox, the runs and the journal are written to
`runs/control-session.json` on every change and restored on startup. The write
is atomic, so an interrupted save cannot leave half a session behind, and a
file that cannot be read back is discarded rather than half-restored —
returning a policy we only partly understand is worse than asking for it
again. Verified across a real process kill, not only in tests.

**~~The worker never resolves a step-up on its own.~~** *(Built.)*
The window is read from `/v1/bootstrap` rather than assumed, every paused
purchase carries its own deadline, and a sweep runs on each poll. Nothing is
sent when a window lapses, and that is deliberate: the guide forbids inventing
a human answer, and measuring the live service showed it already moves an
unanswered purchase from `awaiting_customer` to `timed_out` by itself at
exactly 120 seconds. Anything we submitted would have overwritten that with a
decision nobody made.

### Would be noticed by a judge

**The regex compiler misses phrasings nobody anticipated.**
This is the weakest link in the chain. It is mitigated by showing every
extracted rule back to the customer *before* confirmation, so a bad reading is
visible rather than silent — but it is still a gap.

> Worth testing with phrasings you would actually use:
> `.venv/bin/leash policy "your sentence here"`

**Two numbers are judgements, not measurements.**
Merchant familiarity is `>= 1` prior approved purchase, and the duplicate
window is 3 hours. Both work on the supplied data. Neither is derived from
anything, and the compiler explicitly asks the customer to set the first one.

**No UI for tightening with a brand-new rule.**
The API supports it — `POST /api/policy/tighten` with `add_rules` — but the
interface only exposes the one-click "refuse when unsure". A judge asking "can
I lower my limit mid-run?" gets a curl command rather than a button.

### Known and deliberate

**The advisor has never called a real model.**
Every failure path is tested; the happy path against a live endpoint is not.
It is off by default, so this only matters if you want to demo it.

**`GET /v1/events` is unused.**
The client method exists but nothing calls it. It would matter for reconciling
a run after a network drop mid-scenario.

**Familiarity loads all 4,701 history rows at import.**
Correct at this size, wrong shape at real scale — it should be an index or a
query, not a dict built at startup.

---

## 6. Suggested next work

If picking two, these are the two most likely to cause a problem live:

1. **`GET /v1/events` reconciliation** — recover cleanly from a network drop
   mid-run rather than relying on redelivery alone.
2. **The regex compiler's coverage** — the weakest remaining link, and the one
   only a human writing real sentences can stress.

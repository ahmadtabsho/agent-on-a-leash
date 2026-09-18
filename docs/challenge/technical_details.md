# Agent on a Leash — Technical details

You are building a solution that keeps a customer in control when an AI shopping
agent wants to spend their money.

**Your solution can use an AI model, a decision layer, rules, or a combination.**
For example, a model could understand customer instructions, assess unusual
activity, or detect misleading shop text. You choose how the solution works.
The challenge brief explicitly welcomes machine learning, language models,
behavioral signals, rules, and interface design.

Whatever approach you choose, your prototype has two jobs:

1. **Let the customer control what is allowed.** Help them explain their wishes,
   review the permissions your system understands, and confirm, tighten, or
   revoke them (withdraw permission).
2. **Decide whether each purchase should go ahead.** Consider those permissions,
   the purchase facts, and relevant past activity. Explain the result and
   remember earlier decisions when they affect the next purchase.

The API calls the customer's spending permissions a **wallet policy**. This is
one input to your solution; it does not prescribe the technology you must build.
An AI-based solution still needs to respect the customer's confirmed limits
and support the decisions below.

| Your decision | What it means |
| --- | --- |
| `approve` | Allow this purchase. |
| `decline` | Stop this purchase. |
| `step_up` | Pause this purchase and ask the customer to approve or decline it. |

The customer controls those permissions. The shopping agent and the shop cannot
change them. The supplied simulator provides the shopping agent's proposed
purchases; your solution provides the independent trust and control layer.

```text
Customer describes what is allowed → Your solution explains → Customer confirms
                                                                  ↓
Simulator proposes a purchase → Your solution considers permissions and evidence
                                      ├── approve or decline → Record the result
                                      └── ask the customer
                                              ↓
                                      Human approves or declines → Record the result

```

Everything is synthetic: there are no real cards, customers, payments, or money.
You can use any language, framework, database, or model. No model is hosted for
you. If you use a model, your app must still give a predictable response when
the model or another external service is unavailable.

## Where to begin

**On the event day:** connect your app to the hosted API and test the full flow.
An API lets your app exchange requests and responses with the simulator.
There is no local API server to install or start.

This guide explains how to get started, understand the provided data, and use the API to build and test your solution.

## 1. Choose a scenario

A **scenario** is one test story: one customer, one card, one instruction, and
an ordered set of proposed purchases. A **run** is one execution of that story.

The pack contains **five scenarios with 45 purchases in total**. Start with
`SCEN0000`, the one-purchase connection check. See the
[scenario overview](data/README.md#the-five-public-scenarios) for the other topics
and purchase counts. Read the selected scenario's exact `cardholder_instruction`
in [data/scenario_catalogue.csv](data/scenario_catalogue.csv).

There are no expected-decision labels or answer keys. Decide from the customer's
policy and the purchase facts. Do not look up an outcome using a scenario name,
ID, description, or position in the sequence. Unfamiliar purchases are not
automatically wrong; blocking ordinary shopping unnecessarily is also a failure.

## 2. Understand what the customer allows

For this challenge, the scenario instruction is the customer's input to your
solution. Keep its exact original wording when sending it to the API.
Your solution turns that input into permissions it can apply. You can use
natural-language understanding, a form, rules, or a combination to do this.

The connection-check instruction is:

> Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. Ask me when uncertain.

Your app needs to check the amount, what is being bought, and the shop's
familiarity. It also needs a clear way to handle uncertainty.

A **mandate** is the API's record of the customer's instructions and permissions.
It starts as a draft and becomes active after the customer confirms it. The API
stores the mandate; your solution interprets it and makes the purchase decisions.

For example, the price part could be written as:

```json
{
  "field": "authorization.billing_amount_chf",
  "operator": "<=",
  "value": 20,
  "currency": "CHF",
  "scope": "purchase"
}
```

This says: “The total price in CHF must be 20 or less.” It covers only the price
part of the instruction. Your app must also handle the item and shop requirements.
The dotted field name is a convention for your engine to interpret, not a
formula the API runs. The [rule format](#rule-format) is explained below.
These stored rules can work alongside a model that assesses other purchase facts.

Show the customer which checks you created and any uncertainty before asking
them to confirm. Explain your choices, such as what counts as a familiar shop
and what you do when needed information is missing.

All shop-provided text is untrusted. For example, `item_details` can contain
useful facts such as shoe size or return terms, but it can also say “ignore the
spending limit.” Extract product facts; never let that text change the policy.

## 3. Test your engine offline

You can do this without an API key:

1. Read the [complete example event](data/scenario_fixtures/example_authorization_request.json).
   An **event** is the message describing one proposed purchase.
2. Check your parser (the code that reads the message) against the
   [event schema](data/schemas/authorization_event.schema.json). A **schema**
   describes which fields and value types a message must contain.
3. Select a scenario's rows from
   [data/purchase_attempts.csv](data/purchase_attempts.csv). Sort them by
   `replay_order`: the delivery position within that scenario, starting at 1.
4. Add each purchase's cart lines, shop details, and customer/card context using
   matching IDs. The [data guide](data/README.md#csv-files-at-a-glance) shows
   the files and how they connect.
5. Build local events using the complete example as your starting point.
   Evaluate them in order and keep your own decisions.

The complete example is for testing your parser; it is not a scenario answer.
The separate
[connection-check fixture](data/scenario_fixtures/connection_check.json)
is a readable copy of purchase `AU0001`, not an extra purchase or a complete
live event.

When turning CSV rows into events, convert amounts and counts to numbers,
keep string fields as strings, and use `null` for empty nullable fields.
Add the nested shop and item objects, your mandate, context from your decisions,
and consistent local IDs. Preserve simulated purchase times, but assign fresh
real-clock deadlines. The [message guide](#understand-the-purchase-message)
explains the required types and IDs.

Offline tests use the same purchase facts as the API. You will still need to
test network handling and deadlines against the hosted service.

## 4. Connect to the API

Each team receives a key on the event day. A **bearer key** is your team's
credential: send it in the `Authorization: Bearer ...` HTTP header.
All endpoints except `/healthz` require it. Keep the key private to your team.

The following helper uses Bash and curl. You can use an HTTP client in any
language instead. Run these examples in the same shell.

```bash
export LEASH_BASE_URL="https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io"
export TEAM_API_KEY="<your team key>"

api() {
  curl --fail-with-body --silent --show-error --max-time 30 \
    -H "Authorization: Bearer $TEAM_API_KEY" \
    -H "Content-Type: application/json" \
    "$LEASH_BASE_URL$1" "${@:2}"
}

curl --fail --silent --show-error "$LEASH_BASE_URL/healthz"
api /v1/bootstrap
api /v1/reference-data
```

Here is what these calls do:

| Call | Why you use it | What you receive |
| --- | --- | --- |
| `GET /healthz` | Check that the service is available. No key is needed. | Service health and version information. |
| `GET /v1/bootstrap` | Read the settings for your team before starting. | API/data versions, scenarios, timeout values, limits, and enabled features. |
| `GET /v1/reference-data` | Load the supplied background information. | Small catalogues, including scenarios and fixed currency rates, plus history-file metadata. |
| `GET /v1/reference-data/authorization-history.csv` | Load past activity if your approach needs it. | The historical CSV file. |

For example, download the history with:

```bash
api /v1/reference-data/authorization-history.csv -o authorization-history.csv
```

API errors are returned as JSON under `error`. Check the HTTP status before
treating a response as a successful result.

## 5. Create and confirm the mandate

This request demonstrates how to submit the price rule from step 2. Complete
your app's handling of the full instruction before using it as a finished policy.
`hard_rules` holds your checks, and `uncertainty_policy` says how to handle
uncertainty. `guidance` and `open_questions` hold explanatory text and questions
for the customer; they can be empty lists (`[]`).
`uncertainty_policy` accepts `ask`, `decline`, or `approve`; your solution must
explain and apply the customer's choice.

```bash
api /v1/mandates -X POST -d '{
  "instruction": "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. Ask me when uncertain.",
  "hard_rules": [
    {
      "field": "authorization.billing_amount_chf",
      "operator": "<=",
      "value": 20,
      "currency": "CHF",
      "scope": "purchase"
    }
  ],
  "uncertainty_policy": "ask",
  "guidance": [],
  "open_questions": []
}'
```

The response contains `draft_id` and the submitted content. Show the policy to
the customer. **Only after they agree**, confirm the draft:

```bash
DRAFT_ID="<draft_id from the response>"
api "/v1/mandates/$DRAFT_ID/confirm" -X POST -d '{"confirmed":true}'
```

The confirmation response contains `mandate_id`. Save that returned value:

```bash
MANDATE_ID="<mandate_id from the confirmation response>"
```

Do not submit customer, card, or profile IDs when creating a mandate. The
platform assigns them when the run starts. A profile ID is a platform
identifier for the context used in the run.

## 6. Prepare your worker, then start a run

A **worker** is the part of your app that keeps receiving and answering requests
automatically. Have it ready before starting a run: the default decision deadline
is **8 seconds from when a request is queued**, including time before delivery.
Manually reading a request and typing a decision can take too long.

Your worker asks for the next request with:

```bash
api '/v1/decision-requests/next?wait=25'
```

This is **long-polling**: the server holds the request for up to 25 seconds and
returns early when a purchase is available.

| Response | What your worker does |
| --- | --- |
| HTTP `200` | Read the purchase event inside the response's `data` field. |
| HTTP `204` | There is no body to parse. Check run progress and poll again while work remains. |

`204` does not mean the run has finished. The 25-second polling wait is also
separate from the purchase's decision deadline.

The outer response is called an **envelope**. Keep its `run_id` to identify the
run. The API calls a proposed purchase an **authorization**. Validate the
envelope's `data` against the event schema. Use
`data.authorization.authorization_id` as the live purchase ID.
See [what the message contains](#understand-the-purchase-message).

Once the worker is ready, start the connection check:

```bash
api /v1/scenario-runs -X POST --data-binary @- <<EOF
{
  "scenario_id": "SCEN0000",
  "mandate_id": "$MANDATE_ID"
}
EOF
```

The response includes `run_id`, the selected scenario, the bound mandate,
fixture profiles, and event counters. Save `run_id` to check progress later.
A run uses a **snapshot**: the copy of the mandate taken when that run starts.

This is the worker's outline, not runnable code:

```text
While the run has work remaining:
    Poll for a request.
    If the response is 204, check progress and continue.
    If the response is an error, handle it before reading purchase data.
    Read the envelope's run ID and validate its data event.
    If this live purchase ID was already handled, reconcile its saved result.
    Otherwise, evaluate the policy and submit a decision before deadline_at.
    Record the result accepted by the API.
    If it needs a human answer, show it in the customer interface.
    Keep receiving requests while the interface waits for the customer.

Separately, when the customer answers:
    Submit their answer through /resolve and record the accepted result.
```

## 7. Send a decision and handle the human answer

Send your engine's result to:

```text
POST /v1/authorizations/{authorization_id}/decision
```

Use the live ID in both the URL and body. Here is the request format for a
purchase that your engine has decided needs customer confirmation:

```json
{
  "authorization_id": "<live authorization ID>",
  "decision": "step_up",
  "reason_codes": ["customer_confirmation"],
  "customer_message": "Please review this purchase."
}
```

Only `authorization_id` and `decision` are required. Your engine chooses
`approve`, `decline`, or `step_up` from the policy and evidence. The example
does not prescribe a decision for the connection check.
You can also send `reason_codes`, `customer_message`, `evidence` (facts supporting
the result), and `engine_version` (your solution's version) to explain the decision.

`step_up` pauses the purchase; it does not approve it. Show the reason and
purchase details to the real customer. If they choose to approve, send:

```text
POST /v1/authorizations/{authorization_id}/resolve
```

```json
{
  "decision": "approve",
  "customer_message": "The customer confirmed this purchase.",
  "evidence": []
}
```

If they reject it, send `decline` and explain their choice. Do not invent a human
answer or send a second automated decision after `step_up`; use `/resolve`.

The default human window is **120 seconds**. Read current timeout settings from
`/v1/bootstrap`; `data.deadline_at` is the automated deadline.

## 8. Remember results and keep the customer in control

Earlier purchases can affect the next decision. In particular:

- Count final approvals when enforcing spending limits. A purchase waiting for
  a human answer is not yet approved.
- Use simulated purchase time for spending windows, and the real clock for
  response deadlines.
- Recognize repeated delivery by its live purchase ID. Record it once, so a
  retry does not add the amount twice.
- Check distinct but similar purchases against earlier outcomes; different IDs
  can still describe an unwanted duplicate order.

### Read, change, or revoke permissions

`GET /v1/mandates/{mandate_id}` returns the stored mandate. Use it to display
the customer's current permissions. It also returns `guidance` and
`open_questions`, which are explanatory text and are absent from live events.

`PATCH /v1/mandates/{mandate_id}` updates an active mandate:

- Omitted fields stay unchanged.
- If you send `hard_rules`, keep every existing rule unchanged. You may add
  rules, but cannot remove or replace them.
- You may change `uncertainty_policy` from `approve` or `ask` to `decline`.
  The documented PATCH rules do not allow changing `approve` to `ask`.
- Supplied `guidance` and `open_questions` replace those lists.
- Changes affect later runs. An existing run keeps its original snapshot.

For example, this updates an active mandate that currently uses `ask` or `approve`:

```bash
api "/v1/mandates/$MANDATE_ID" -X PATCH -d '{"uncertainty_policy":"decline"}'
```

`DELETE /v1/mandates/{mandate_id}` revokes the mandate, withdrawing permission:

```bash
api "/v1/mandates/$MANDATE_ID" -X DELETE
```

The platform rejects revoked or expired mandates, inactive fixture authorities,
and blocked cards before queueing an actionable request. The effect of revoking
a mandate while one of its purchases is already queued or waiting for a human
is not yet specified. Show cancellation only when the platform confirms it.

### Start a clean development session

`POST /v1/team/reset` clears your team's mandates, runs, decisions, and event
cursor so you can test from a clean state. It is disabled during judging.
After a reset, create a new mandate and run instead of reusing the old IDs.

## What to show in your demo

Show these three things:

1. An ordinary purchase completes with little friction.
2. An ambiguous, unsafe, or manipulated purchase gets a useful intervention.
3. The customer can use the human approval, rejection, or revocation path.

For each result, explain what the app allowed, which facts it used, and why.
Make uncertainty visible. Let the audience see that the customer stays in control.

## Where to find the data details

The [data README](data/README.md) explains the pack. Use these sections when
you need more detail:

| What you need | Where to look |
| --- | --- |
| File contents, row counts, and how records connect | [Files and joins](data/README.md#csv-files-at-a-glance) |
| Past purchases, refunds, and agent activity | [Using the history](data/README.md#how-to-use-the-history) |
| Exact field meanings, currencies, missing values, and time calculations | [Data dictionary](data/data_dictionary.md) |
| Example files, schemas, and the file manifest | [Supporting files](data/README.md#other-files) |

Use IDs to connect records, not names. Historical outcomes are context for your
analysis, not expected answers for new purchases.

## Rule format

The API stores a list of checks in `hard_rules`. Your solution decides how to
interpret those checks and how they work with any models or other analysis.
The live-event schema defines these fields for each rule:

| Field | Required? | Allowed value |
| --- | --- | --- |
| `field` | Yes | A nonempty string naming the fact to check. |
| `operator` | Yes | `<`, `<=`, `=`, `!=`, `>`, `>=`, `in`, or `not_in`. |
| `value` | Yes | A number, a string, or a list containing only strings. |
| `currency` | No | `"CHF"`, `"EUR"`, `"GBP"`, `"USD"`, or `null`. |
| `scope` | No | `"purchase"`, `"period"`, or `null`. |
| `period_days` | No | A whole number of days, at least 1, or `null`. |

No extra rule fields are allowed. A rule's `value` cannot be a boolean, `null`,
an object, or a list of numbers. Omit unused optional fields in new mandate
requests; the nulls above describe what the live-event schema accepts.

Explain how your solution reads field names, handles every item in a basket,
combines checks, and deals with missing facts. These choices belong to your
solution. Adding a rule must not weaken a customer's existing restriction.

## Understand the purchase message

The poll response's outer envelope includes `run_id`, `event_id`, `type`,
`authorization_id`, `status`, and `occurred_at`. The complete purchase event is
inside `data`. Keep the envelope for run tracking; validate `data` as the event.

| Event field | What it means |
| --- | --- |
| `type` | Always `"authorization.request"`. |
| `request_id` | The event's request identifier. |
| `deadline_at` | The real-clock deadline for the automated answer. |
| `authorization` | Purchase, shop, cart, and session facts. |
| `mandate` | The confirmed instructions and permissions saved when this run started. |
| `context` | Spend and recent authorization information from this run. |
| `runtime` | Real-clock receipt time and information about how the context was built. |

### Use the correct value types

| Field or value | Correct live JSON type |
| --- | --- |
| Amounts and prices | Numbers, such as `20.0`, not `"20.00"`. |
| `quantity`, `line_no`, `replay_order` | Whole numbers, at least 1. |
| `recent_attempt_count_10m` | A whole number, at least 0. |
| `merchant_mcc` | A four-digit string, such as `"5411"`. |
| `order_returnable`, `order_cancellable` | Strings: `"true"`, `"false"`, `"unknown"`, or `"not_applicable"`. |
| Merchant `recurring_capable` | Only the strings `"true"` or `"false"`. |
| `delivery_by` | A date string such as `"2026-08-10"`, or `null`. The field is required. |
| `related_authorization_id` | A string or `null`. The field is required. |
| `related_authorization_status` | `"pending"`, `"approved"`, `"declined"`, `"cancelled"`, or `null`. The field is required. |

`unknown` means information was not supplied. `not_applicable` means the term
does not apply to that kind of order. A `null` field is present but has no value;
it must not be silently dropped, changed to zero, or treated as permission.
`items` must contain at least one cart line. Every live scenario purchase has
`initiator_type: "agent"`.

Useful facts such as shoe size and return-window length can be in `item_details`;
there are no dedicated live fields named `shoe_size` or `return_period_days`.
A shop's category also does not establish every basket item's category.

### Keep the IDs separate

Keep the `draft_id`, `mandate_id`, and `run_id` returned by their API calls.
For each purchase, distinguish these two IDs:

- Live `authorization_id`: submit or resolve its decision and recognize repeated
  delivery. It stays the same within a run but changes between runs.
- `source_authorization_id`: find the original `AU...` row in the purchase CSV.

The API also rewrites a non-null `related_authorization_id` to the related
purchase's live ID in that run. Keep the same mapping in your offline events.
For historical IDs and CSV relationships, see the
[identifier guide](data/data_dictionary.md#identifier-namespaces).

### Calculate money consistently

`amount` already includes delivery; `billing_amount_chf` is that total in CHF.
Do not add delivery again. Use the row's currency, not the shop's country,
when converting prices. The [units and currency guide](data/data_dictionary.md#units-and-nulls)
lists the fixed rates, item-price units, and rounding rules.

## All API calls in one place

An **endpoint** is an API address for an operation. `GET` reads information,
`POST` submits or starts an operation, `PATCH` updates selected fields, and
`DELETE` revokes a mandate here. All calls except `/healthz` need the bearer key.
Replace names in braces, such as `{run_id}`, with values returned by the API.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/healthz` | Reports whether the service is available, plus version information. |
| `GET` | `/v1/bootstrap` | Gives your team's versions, scenarios, limits, timeout settings, and features. |
| `GET` | `/v1/reference-data` | Returns small catalogues, currency rates, and history-file metadata. |
| `GET` | `/v1/reference-data/authorization-history.csv` | Downloads the historical CSV for your analysis. |
| `POST` | `/v1/mandates` | Stores the original instruction and your structured permissions as a draft; returns `draft_id`. |
| `POST` | `/v1/mandates/{draft_id}/confirm` | Records the customer's agreement and activates the mandate; returns `mandate_id`. |
| `GET` | `/v1/mandates/{mandate_id}` | Reads the full stored mandate, including guidance and open questions. |
| `PATCH` | `/v1/mandates/{mandate_id}` | Preserves or tightens an active mandate for later runs, following step 8's update rules. |
| `DELETE` | `/v1/mandates/{mandate_id}` | Revokes that mandate. See step 8 for the limitation affecting existing pending work. |
| `POST` | `/v1/scenario-runs` | Starts the selected scenario with an active mandate; returns `run_id` and run information. |
| `GET` | `/v1/scenario-runs/{run_id}` | Reads the run's progress and event counters. |
| `GET` | `/v1/decision-requests/next?wait=25` | Waits up to 25 seconds for work; returns a `200` envelope or an empty `204`. |
| `POST` | `/v1/authorizations/{authorization_id}/decision` | Records your solution's `approve`, `decline`, or `step_up` decision. |
| `POST` | `/v1/authorizations/{authorization_id}/resolve` | Records a real customer's `approve` or `decline` after `step_up`. |
| `GET` | `/v1/authorizations` | Lists pending and final runtime authorizations so you can inspect results. |
| `GET` | `/v1/events?since=0` | Reads the event feed; use returned `next_cursor` as the next `since`. |
| `POST` | `/v1/team/reset` | Clears team development state. Disabled during judging. |

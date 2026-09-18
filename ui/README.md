# Control UI

The customer-facing half of the wallet control layer (step 7). Kept separate
from the engine so the two can be deployed and scaled independently — Viseca
intends to fold this surface into the existing `one` app, while the decision
engine stays a backend hot path.

Two jobs:

1. **Policy management** — draft a mandate from the customer's own words, show
   back the checks the system understood plus anything it is unsure about, and
   let the customer confirm, tighten, or revoke.
2. **Step-up inbox** — when the engine pauses a purchase, show the reason and
   the evidence, and carry the customer's approve/decline answer back through
   `/resolve`.

Scaffolding lands in step 7.

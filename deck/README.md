# The deck

`agent-on-a-leash.pptx` — 10 slides, 16:9, built to be edited.

Everything is a real shape or text box, never a flattened image of a slide, so
Canva can move, restyle and rewrite any of it. Upload the `.pptx` directly:
**Canva → Create a design → Import file**.

`agent-on-a-leash.pdf` is the same deck, for presenting or printing without
Canva in the loop.

## Rebuilding it

```bash
.venv/bin/python deck/build_deck.py
```

The script is the source of truth; the `.pptx` is a build artefact. Edit the
script when the numbers change, not the file.

`assets/` holds the team photos, centre-cropped square and masked to circles
with a supersampled edge — the originals range from 200×302 to 1600×1600, so
they could not be dropped in as-is.

## Fonts

The deck asks for **IBM Plex Sans** and **IBM Plex Mono**, which Canva has. If
a viewer lacks them it substitutes silently and the layout still holds.

## Slides

| # | Slide |
| --- | --- |
| 1 | Title — with the live result stated up front |
| 2 | The problem: four ways an agent goes wrong |
| 3 | The boundary: what Viseca provides, what we build |
| 4 | Architecture: two deployables, one contract |
| 5 | The seven stages, and why "uncertain" is separate from "no" |
| 6 | Demo: the shop tries to give us orders |
| 7 | Results: all 45 purchases, answered live |
| 8 | Who is in charge: the customer keeps the leash |
| 9 | The optional model: built, measured, switched off |
| 10 | The team |
| 11 | Closing |

The deck and [`docs/report/`](../docs/report/) carry the same numbers from the
same source. When a figure changes, rerun both.

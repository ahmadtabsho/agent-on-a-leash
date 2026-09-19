#!/usr/bin/env bash
# Rebuild the report PDF and the deck. The numbers on both come from the same
# sources, so they are rebuilt together and never drift apart.
set -e
cd "$(dirname "$0")/.."

google-chrome --headless --disable-gpu --no-sandbox \
  --print-to-pdf=docs/report/agent-on-a-leash-report.pdf \
  --no-pdf-header-footer --virtual-time-budget=12000 \
  docs/report/report.html 2>/dev/null
echo "report : docs/report/agent-on-a-leash-report.pdf"

google-chrome --headless --disable-gpu --no-sandbox \
  --print-to-pdf=docs/report/technical-report.pdf \
  --no-pdf-header-footer --virtual-time-budget=14000 \
  docs/report/technical-report.html 2>/dev/null
echo "tech   : docs/report/technical-report.pdf"

.venv/bin/python deck/build_deck.py >/dev/null
soffice --headless --convert-to pdf --outdir /tmp/_deckpdf deck/agent-on-a-leash.pptx >/dev/null 2>&1
cp /tmp/_deckpdf/agent-on-a-leash.pdf deck/agent-on-a-leash.pdf
rm -rf /tmp/_deckpdf
echo "deck   : deck/agent-on-a-leash.pptx (+ .pdf)"

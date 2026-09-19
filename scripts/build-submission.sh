#!/usr/bin/env bash
# Build the submission archive, and refuse to produce one containing a secret.
set -e
cd "$(dirname "$0")/.."

OUT=submission/agent-on-a-leash.zip
rm -f "$OUT"
zip -rq "$OUT" . \
  -x '.venv/*' '.git/*' '*/node_modules/*' 'node_modules/*' \
     'runs/*' '.env' '*.pyc' '*/__pycache__/*' '.pytest_cache/*' '.ruff_cache/*' \
     'ui/dist/*' 'deck/preview/*' 'docs/report/*.png' 'submission/*' \
     'Screenshot*' '*.zip' '*/.DS_Store'

# The archive is only safe while .env stays out of it, so check rather than trust.
TMP=$(mktemp -d)
unzip -qo "$OUT" -d "$TMP"
FAIL=0
if [ -f "$TMP/.env" ]; then echo "REFUSING: .env is in the archive"; FAIL=1; fi
if [ -f .env ]; then
  while IFS='=' read -r name value; do
    case "$name" in TEAM_API_KEY|OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY) ;; *) continue ;; esac
    [ -z "$value" ] && continue
    if grep -rqF "$value" "$TMP" 2>/dev/null; then
      echo "REFUSING: the value of $name appears inside the archive"
      FAIL=1
    fi
  done < .env
fi
rm -rf "$TMP"
[ "$FAIL" -eq 1 ] && { rm -f "$OUT"; exit 1; }

echo "$OUT  $(du -h "$OUT" | cut -f1)  — no credentials found"

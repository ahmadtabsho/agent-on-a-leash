#!/usr/bin/env bash
# Arm the demo: authorise the monitor policy and run the manipulated-agent
# scenario, leaving two purchases waiting with a live countdown.
set -e
B=http://127.0.0.1:8000/api
curl -s -X POST $B/session/reset -o /dev/null
curl -s -X POST $B/policy/draft -H 'Content-Type: application/json' -d '{
  "instruction": "Buy the 27-inch monitor I chose, from a seller I have bought from before, for CHF 400 or less. Do not add anything I did not ask for. Ask me when uncertain."
}' -o /dev/null
curl -s -X POST $B/policy/confirm -H 'Content-Type: application/json' -d '{"confirmed":true}' -o /dev/null
curl -s -X POST $B/runs/SCEN0004 -o /dev/null
echo "Armed. Two purchases are now waiting — go and watch them."

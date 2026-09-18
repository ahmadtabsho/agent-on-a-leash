VENV := .venv
PY   := $(VENV)/bin/python
BIN  := $(VENV)/bin

.PHONY: setup check health test lint replay demo serve ui build clean all

all: check lint test ## the full gate

setup: ## create the venv and install the engine
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip --quiet
	$(PY) -m pip install -e "engine[dev]"

check: ## verify the vendored data pack against its manifest
	$(BIN)/leash check

health: ## probe the hosted sandbox API
	$(BIN)/leash health

test:
	$(BIN)/pytest engine -q

lint:
	$(BIN)/ruff check engine

replay: ## replay all five scenarios offline
	$(BIN)/leash replay

demo: ## the three things the brief asks to see
	$(BIN)/leash demo

serve: ## run the control API on :8000
	$(BIN)/python -m uvicorn leash.api.control:app --reload --port 8000

worker: ## poll the sandbox and answer purchases (needs TEAM_API_KEY)
	$(BIN)/leash worker

run: ## the whole live sequence for one scenario (needs TEAM_API_KEY)
	$(BIN)/leash run --scenario SCEN0000

ui: ## run the control interface on :5173
	cd ui && npm install && npm run dev

build:
	cd ui && npm install && npm run build

clean:
	rm -rf $(VENV) engine/src/*.egg-info .pytest_cache .ruff_cache ui/node_modules ui/dist

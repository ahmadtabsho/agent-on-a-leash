VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: setup check health test lint ui clean

setup: ## create the venv and install the engine in editable mode
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip --quiet
	$(PY) -m pip install -e "engine[dev]"

check: ## verify the vendored data pack against its manifest
	$(VENV)/bin/leash check

health: ## probe the hosted sandbox API
	$(VENV)/bin/leash health

test:
	$(VENV)/bin/pytest engine

lint:
	$(VENV)/bin/ruff check engine

ui:
	cd ui && npm install && npm run dev

clean:
	rm -rf $(VENV) engine/src/*.egg-info .pytest_cache .ruff_cache

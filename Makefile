# Orqentis FabricOps developer commands. Requires Python 3.12+ (override with PYTHON=python3.12).
PYTHON   ?= python3
VENV     ?= .venv
BIN      := $(VENV)/bin
SCENARIO ?= schema_drift
SCENARIOS := schema_drift auth_failure capacity_pressure unknown_failure

.DEFAULT_GOAL := help
.PHONY: help install demo demo-all serve test lint format typecheck check runbooks audit clean

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

$(VENV)/.installed: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --upgrade pip
	$(BIN)/python -m pip install --quiet -e ".[dev,llm]"
	@touch $@

install: $(VENV)/.installed ## Create .venv and install the package with dev + llm extras

demo: install ## Run the synthetic incident end to end (no Fabric or Azure credentials needed)
	$(BIN)/orqentis demo --scenario $(SCENARIO)

demo-all: install ## Run all synthetic scenarios (summary only)
	@for s in $(SCENARIOS); do $(BIN)/orqentis demo --scenario $$s --data-dir var/demo-$$s --no-report || exit 1; done

serve: install ## Run the REST API on http://127.0.0.1:8000 (OpenAPI UI at /docs)
	$(BIN)/orqentis serve

test: install ## Run the test suite
	$(BIN)/pytest -q

lint: install ## Lint and check formatting
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests

format: install ## Format and auto-fix
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

typecheck: install ## Strict type checking
	$(BIN)/mypy

runbooks: install ## Validate the runbook catalog
	$(BIN)/orqentis runbooks

audit: install ## Verify the demo audit trail hash chain
	$(BIN)/orqentis audit --data-dir var/demo

check: lint typecheck test runbooks ## Everything CI runs

clean: ## Remove local runtime data and caches
	rm -rf var .pytest_cache .mypy_cache .ruff_cache

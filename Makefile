# PYTHONPATH is explicit: macOS can mark uv's editable-install .pth file hidden, and
# Python then skips it. Being explicit works everywhere.
export PYTHONPATH := src:tests:.

COMPOSE := docker compose -f docker/compose.yml

.PHONY: setup lint test test-live up down reset seed seed-file serve eval eval-live eval-live-fresh report

setup:
	uv sync --all-extras

lint:
	uv run ruff check src tests scripts evals

test:            ## unit tests + fast eval (in-memory ERP, no network)
	uv run pytest -m "not live" --cov=erp_guard --cov-report=term-missing

test-live:       ## integration tests against the local ERPNext (make up && make seed first)
	uv run pytest -m live -v

up:              ## start ERPNext v16 locally on http://localhost:8080 (~4 GB RAM)
	$(COMPOSE) up -d
	@echo "waiting for ERPNext (a brand-new site takes ~5 minutes to install)..."
	@# Poll the one-off create-site container rather than `compose wait`, which fails on
	@# Compose v5 once the container has already exited.
	@until [ "$$($(COMPOSE) ps -a --format '{{.State}} {{.ExitCode}}' create-site)" = "exited 0" ]; do \
		if $(COMPOSE) ps -a --format '{{.State}} {{.ExitCode}}' create-site | grep -Eq '^exited [1-9]'; then \
			echo "create-site failed; see: $(COMPOSE) logs create-site"; exit 1; fi; \
		sleep 3; done
	@until curl -sf http://localhost:8080/api/method/ping >/dev/null; do sleep 3; done
	@echo "ERPNext is up. First time? run: make seed"

down:            ## stop ERPNext, keep its data
	$(COMPOSE) down

reset:           ## stop ERPNext and wipe its data (then: make up && make seed)
	$(COMPOSE) down -v

seed:            ## setup wizard, custom fields, audit doctype, demo data, API keys -> .env
	uv run python scripts/seed_erpnext.py

seed-file:       ## rebuild data/seed/suppliers.csv from spend-leakage-finder's name map
	uv run --extra seed python scripts/build_seed.py

serve:           ## run the MCP server on stdio as the requester (ERPNext)
	uv run erp-guard-mcp

eval:            ## guardrail eval on the in-memory ERP (what CI runs)
	uv run python -m evals.run --erp memory

eval-live:       ## guardrail eval against a freshly seeded local ERPNext
	uv run python -m evals.run --erp erpnext

eval-live-fresh: ## wipe ERPNext, reseed, run guarded; wipe, reseed, run the naive baseline
	$(MAKE) reset up seed
	uv run python -m evals.run --erp erpnext --mode guarded
	$(MAKE) reset up seed
	uv run python -m evals.run --erp erpnext --mode naive

report:          ## rerun the matching eval, then rewrite README's measured sections from evals/results/
	uv run python -m evals.matching
	uv run python -m evals.report

.DEFAULT_GOAL := help
.PHONY: help dev test lint codegen migrate install

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

dev: ## Run api + web dev servers via docker-compose (no .env required)
	docker compose up --build

test: ## Backend test suite (pytest incl. schema validation)
	cd api && uv run pytest

lint: ## ruff + mypy (api), tsc + eslint (web)
	cd api && uv run ruff check . && uv run ruff format --check . && uv run mypy .
	cd web && pnpm run typecheck && pnpm run lint

codegen: ## Regenerate web/src/api from FastAPI OpenAPI spec
	cd api && uv run python openapi_export.py > openapi.json
	cd web && pnpm run openapi-ts

migrate: ## STUB — no database configured in the credential-free slice
	@echo "migrate: no database configured yet (stub) — see ARCHITECTURE.md §4"

install: ## Install api (uv) and web (pnpm) dependencies locally
	cd api && uv sync
	cd web && pnpm install

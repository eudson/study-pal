.DEFAULT_GOAL := help
.PHONY: help dev test test-fixtures lint codegen migrate install

MIGRATIONS_DIR := api/migrations
DB_DSN         ?= postgresql://studypal:studypal@localhost:5432/studypal

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

dev: ## Run api + web + db dev servers via docker-compose (no .env required)
	docker compose up --build

test: ## Backend test suite (pytest incl. schema validation + RLS isolation tier)
	cd api && uv run pytest -v

test-fixtures: ## Fail loud when /fixtures has no expected artefacts (D-R3)
	cd api && uv run pytest -m fixture_gate -v

lint: ## ruff + mypy (api), tsc + eslint (web)
	cd api && uv run ruff check . && uv run ruff format --check . && uv run mypy .
	cd web && pnpm run typecheck && pnpm run lint

codegen: ## Regenerate web/src/api from FastAPI OpenAPI spec
	cd api && uv run python openapi_export.py > openapi.json
	cd web && pnpm run openapi-ts

migrate: ## Apply api/migrations/*.sql in lexical order via the migration/owner role
	@echo "Applying migrations to $(DB_DSN) ..."
	@for f in $(sort $(wildcard $(MIGRATIONS_DIR)/*.sql)); do \
		echo "  --> $$f"; \
		psql "$(DB_DSN)" -v ON_ERROR_STOP=1 -f "$$f"; \
	done
	@echo "Migrations applied."

install: ## Install api (uv) and web (pnpm) dependencies locally
	cd api && uv sync
	cd web && pnpm install

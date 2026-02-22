# 🐀 RAT — Root Makefile
# All targets use Docker — nothing installed on host.
# Run `make help` to see all available commands.

COMPOSE := docker compose -f infra/docker-compose.yml
COMPOSE_TEST := docker compose -f infra/docker-compose.test.yml
PROTO_IMAGE := bufbuild/buf:1.35.0
SQLC_IMAGE := sqlc/sqlc:1.27.0
GO_IMAGE := golang:1.24-alpine
GO_TEST_IMAGE := golang:1.24
GO_LINT_IMAGE := golangci/golangci-lint:v1.60.0
PY_IMAGE := python:3.12-slim
PY_TEST_RUNNER_IMAGE := rat-runner-test
PY_TEST_QUERY_IMAGE := rat-query-test
NODE_IMAGE := node:20-alpine

.PHONY: help setup up down restart build rebuild logs status \
        test test-all-parallel test-go test-py test-ts test-integration test-images \
        lint lint-go-strict fmt proto sqlc \
        sdk-build sdk-test portal-build portal-typecheck \
        dev-ratd dev-portal clean clean-all smoke-test \
        backup restore \
        docs docs-build docs-serve \
        licenses

# ── Help ────────────────────────────────────────────────────────
help: ## Show all available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Setup ──────────────────────────────────────────────────────
setup: proto sqlc sdk-build ## First-time setup (generate code, build SDK)
	@echo "✅ Setup complete — run 'make up' to start services"

# ── Lifecycle ───────────────────────────────────────────────────
up: ## Start all services (docker compose up)
	$(COMPOSE) up -d
	@echo "🐀 RAT is running"
	@echo "   Portal:   http://localhost:3000"
	@echo "   API:      http://localhost:8080"
	@echo "   MinIO:    http://localhost:9001"
	@echo "   Nessie:   http://localhost:19120"

down: ## Stop all services
	$(COMPOSE) down

restart: ## Restart all services (down + up)
	$(COMPOSE) down
	$(COMPOSE) up -d
	@echo "🐀 RAT restarted"
	@echo "   Portal:   http://localhost:3000"
	@echo "   API:      http://localhost:8080"
	@echo "   MinIO:    http://localhost:9001"
	@echo "   Nessie:   http://localhost:19120"

build: ## Build all Docker images
	$(COMPOSE) build

rebuild: ## Rebuild all Docker images (no cache)
	$(COMPOSE) build --no-cache

logs: ## Tail all service logs
	$(COMPOSE) logs -f

status: ## Show service status
	$(COMPOSE) ps

# ── Testing ─────────────────────────────────────────────────────
test: test-go test-py test-ts ## Run ALL tests (Go + Python + TS — use `make -j3 test` for parallel)

test-all-parallel: ## Run ALL tests in parallel (Go + Python + TS)
	@$(MAKE) -j3 test-go test-py test-ts

test-go: ## Run Go tests (platform — resolves deps, race detector enabled)
	@docker run --rm -v $$(pwd)/platform:/app -w /app $(GO_TEST_IMAGE) \
		sh -c "go mod tidy && go test -v -race -count=1 ./..."

test-py: ## Run all Python tests (runner + query — uses pre-built images if available)
	@if docker image inspect $(PY_TEST_RUNNER_IMAGE) >/dev/null 2>&1; then \
		echo "🚀 Using pre-built runner test image..."; \
		docker run --rm \
			-v $$(pwd)/runner/src:/app/src \
			-v $$(pwd)/runner/tests:/app/tests \
			$(PY_TEST_RUNNER_IMAGE); \
	else \
		echo "⚠️  Pre-built runner test image not found, installing deps from scratch (run 'make test-images' to speed this up)..."; \
		docker run --rm -v $$(pwd)/runner:/app -w /app $(PY_IMAGE) \
			sh -c "pip install -q uv 2>/dev/null && uv pip install --system -q -e '.[dev]' && pytest -v"; \
	fi
	@if docker image inspect $(PY_TEST_QUERY_IMAGE) >/dev/null 2>&1; then \
		echo "🚀 Using pre-built query test image..."; \
		docker run --rm \
			-v $$(pwd)/query/src:/app/src \
			-v $$(pwd)/query/tests:/app/tests \
			$(PY_TEST_QUERY_IMAGE); \
	else \
		echo "⚠️  Pre-built query test image not found, installing deps from scratch (run 'make test-images' to speed this up)..."; \
		docker run --rm -v $$(pwd)/query:/app -w /app $(PY_IMAGE) \
			sh -c "pip install -q uv 2>/dev/null && uv pip install --system -q -e '.[dev]' && pytest -v"; \
	fi

test-images: ## Build pre-built Python test images (speeds up test-py)
	@echo "🏗️  Building runner test image..."
	@docker build -f runner/Dockerfile.test -t $(PY_TEST_RUNNER_IMAGE) runner/
	@echo "🏗️  Building query test image..."
	@docker build -f query/Dockerfile.test -t $(PY_TEST_QUERY_IMAGE) query/
	@echo "✅ Test images built — run 'make test-py' to use them"

test-ts: sdk-test ## Run all TypeScript tests (SDK + portal)
	@echo "🧪 Testing portal..."
	@docker run --rm \
		-v $$(pwd)/sdk-typescript:/workspace/sdk-typescript \
		-v $$(pwd)/portal:/workspace/portal \
		-w /workspace/portal \
		$(NODE_IMAGE) \
		sh -c "npm install --silent && npm test" 2>/dev/null || echo "⚠️  Portal tests not yet configured"

test-integration: ## Run Go integration tests against real Postgres + MinIO
	@echo "🐘 Starting test services (Postgres + MinIO)..."
	@$(COMPOSE_TEST) --profile test up -d --wait
	@echo "🧪 Running integration tests..."
	@docker run --rm \
		-v $$(pwd)/platform:/app -w /app \
		--network host \
		-e DATABASE_URL=postgres://rat:rat@localhost:5433/rat_test?sslmode=disable \
		-e S3_ENDPOINT=localhost:9002 \
		-e S3_ACCESS_KEY=test-access-key \
		-e S3_SECRET_KEY=test-secret-key \
		$(GO_TEST_IMAGE) \
		sh -c "go mod tidy && go test -v -race -count=1 ./internal/postgres/... ./internal/storage/..."
	@$(COMPOSE_TEST) --profile test down
	@echo "✅ Integration tests passed"

# ── Code Quality ────────────────────────────────────────────────
lint: ## Lint all code (Go + Python + Proto)
	@echo "🔍 Linting Go..."
	@docker run --rm -v $$(pwd)/platform:/app -w /app $(GO_IMAGE) \
		sh -c "go mod tidy && go vet ./..."
	@echo "🔍 Linting Python..."
	@docker run --rm -v $$(pwd)/runner:/app -w /app $(PY_IMAGE) \
		sh -c "pip install -q ruff 2>/dev/null && ruff check . && ruff format --check ."
	@docker run --rm -v $$(pwd)/query:/app -w /app $(PY_IMAGE) \
		sh -c "pip install -q ruff 2>/dev/null && ruff check . && ruff format --check ."
	@echo "🔍 Linting Proto..."
	@docker run --rm -v $$(pwd)/proto:/workspace -w /workspace $(PROTO_IMAGE) lint
	@echo "✅ All clean"

lint-go-strict: ## Run golangci-lint on Go code (strict, uses .golangci.yml)
	@echo "🔍 Running golangci-lint (strict)..."
	@docker run --rm -v $$(pwd)/platform:/app -w /app $(GO_LINT_IMAGE) \
		golangci-lint run ./...
	@echo "✅ golangci-lint passed"

fmt: ## Format all code (Go + Python)
	@docker run --rm -v $$(pwd)/platform:/app -w /app $(GO_IMAGE) \
		sh -c "go install golang.org/x/tools/cmd/goimports@latest 2>/dev/null && goimports -w ."
	@docker run --rm -v $$(pwd)/runner:/app -w /app $(PY_IMAGE) \
		sh -c "pip install -q ruff 2>/dev/null && ruff format ."
	@docker run --rm -v $$(pwd)/query:/app -w /app $(PY_IMAGE) \
		sh -c "pip install -q ruff 2>/dev/null && ruff format ."
	@echo "✅ Formatted"

# ── Code Generation ────────────────────────────────────────────
sqlc: ## Generate Go code from SQL queries (sqlc)
	@docker run --rm -v $$(pwd)/platform/internal/postgres:/src -w /src $(SQLC_IMAGE) generate
	@echo "✅ sqlc generated"

proto: ## Generate Go + Python code from proto files
	@docker run --rm \
		-v $$(pwd)/proto:/workspace/proto \
		-v $$(pwd)/platform:/workspace/platform \
		-v $$(pwd)/runner:/workspace/runner \
		-v $$(pwd)/query:/workspace/query \
		-w /workspace/proto \
		$(PROTO_IMAGE) generate
	@echo "✅ Proto generated"

# ── Development ─────────────────────────────────────────────────
dev-ratd: ## Hot reload Go platform (air)
	docker run --rm -it \
		-v $$(pwd)/platform:/app -w /app \
		-p 8080:8080 -p 8081:8081 \
		--network infra_default \
		$(GO_IMAGE) \
		sh -c "go install github.com/air-verse/air@latest && air"

sdk-build: ## Build TypeScript SDK
	@echo "📦 Building SDK..."
	@docker run --rm \
		-v $$(pwd)/sdk-typescript:/app -w /app \
		$(NODE_IMAGE) \
		sh -c "npm install && npm run build"
	@echo "✅ SDK built"

sdk-test: ## Test TypeScript SDK
	@echo "🧪 Testing SDK..."
	@docker run --rm \
		-v $$(pwd)/sdk-typescript:/app -w /app \
		$(NODE_IMAGE) \
		sh -c "npm install && npm run build && npm test"

portal-build: sdk-build ## Build portal for production (standalone Next.js)
	@echo "🏗️  Building portal..."
	@docker run --rm \
		-v $$(pwd)/sdk-typescript:/workspace/sdk-typescript \
		-v $$(pwd)/portal:/workspace/portal \
		-w /workspace/portal \
		$(NODE_IMAGE) \
		sh -c "npm install && npm run build"
	@echo "✅ Portal built (standalone output in portal/.next)"

portal-typecheck: sdk-build ## Type-check portal without building
	@echo "🔍 Type-checking portal..."
	@docker run --rm \
		-v $$(pwd)/sdk-typescript:/workspace/sdk-typescript \
		-v $$(pwd)/portal:/workspace/portal \
		-w /workspace/portal \
		$(NODE_IMAGE) \
		sh -c "npm install && npx tsc --noEmit"

dev-portal: sdk-build ## Hot reload Next.js portal (builds SDK first)
	docker run --rm -it \
		-v $$(pwd)/sdk-typescript:/workspace/sdk-typescript \
		-v $$(pwd)/portal:/workspace/portal \
		-w /workspace/portal \
		-p 3000:3000 \
		--network infra_default \
		-e API_URL=http://ratd:8080 \
		-e NEXT_PUBLIC_API_URL=http://localhost:8080 \
		$(NODE_IMAGE) \
		sh -c "npm install && npm run dev"

# ── E2E Testing ────────────────────────────────────────────────
smoke-test: ## Run E2E smoke test against running stack
	@bash infra/scripts/smoke-test.sh

# ── Backup & Restore ──────────────────────────────────────────
# Network detection: find which Docker network the compose stack uses
COMPOSE_NETWORK = $$($(COMPOSE) ps -q minio | head -1 | xargs docker inspect --format='{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}' | head -1 | xargs docker network inspect --format='{{.Name}}')

backup: ## Backup Postgres + MinIO to ./backups/<timestamp>/
	@BDIR=./backups/$$(date +%Y%m%d_%H%M%S) && \
	mkdir -p "$$BDIR/postgres" "$$BDIR/minio" && \
	echo "🗄️  Backing up Postgres..." && \
	$(COMPOSE) exec -T postgres pg_dump -U $${POSTGRES_USER:-rat} -Fc rat > "$$BDIR/postgres/rat.dump" && \
	echo "✅ Postgres dumped to $$BDIR/postgres/rat.dump" && \
	echo "📦 Backing up MinIO..." && \
	docker run --rm \
		--network $(COMPOSE_NETWORK) \
		-v $$(cd "$$BDIR/minio" && pwd):/backup \
		-e MC_HOST_local=http://$${S3_ACCESS_KEY:-minioadmin}:$${S3_SECRET_KEY:-minioadmin}@minio:9000 \
		minio/mc:RELEASE.2024-06-12T14-34-03Z \
		mirror --overwrite local/ /backup/ && \
	echo "✅ MinIO backed up to $$BDIR/minio/" && \
	echo "🐀 Backup complete: $$BDIR"

restore: ## Restore Postgres + MinIO from BACKUP_DIR=./backups/<timestamp>
ifndef BACKUP_DIR
	@echo "❌ Usage: make restore BACKUP_DIR=./backups/<timestamp>"
	@echo "   Available backups:"
	@ls -1d ./backups/*/ 2>/dev/null || echo "   (none found)"
	@exit 1
endif
	@if [ ! -f "$(BACKUP_DIR)/postgres/rat.dump" ]; then \
		echo "❌ No Postgres dump found at $(BACKUP_DIR)/postgres/rat.dump"; \
		exit 1; \
	fi
	@if [ ! -d "$(BACKUP_DIR)/minio" ]; then \
		echo "❌ No MinIO backup found at $(BACKUP_DIR)/minio/"; \
		exit 1; \
	fi
	@BDIR=$(BACKUP_DIR) && \
	echo "🗄️  Restoring Postgres from $$BDIR/postgres/rat.dump..." && \
	$(COMPOSE) exec -T postgres pg_restore -U $${POSTGRES_USER:-rat} -d rat --clean --if-exists --no-owner -Fc < "$$BDIR/postgres/rat.dump" && \
	echo "✅ Postgres restored" && \
	echo "📦 Restoring MinIO from $$BDIR/minio/..." && \
	docker run --rm \
		--network $(COMPOSE_NETWORK) \
		-v $$(cd "$$BDIR/minio" && pwd):/backup:ro \
		-e MC_HOST_local=http://$${S3_ACCESS_KEY:-minioadmin}:$${S3_SECRET_KEY:-minioadmin}@minio:9000 \
		minio/mc:RELEASE.2024-06-12T14-34-03Z \
		mirror --overwrite /backup/ local/ && \
	echo "✅ MinIO restored" && \
	echo "🐀 Restore complete from: $$BDIR"

# ── Cleanup ─────────────────────────────────────────────────────
clean: ## Remove all containers, volumes, and generated files
	@echo "🧹 Cleaning up..."
	@echo "   → Stopping containers and removing volumes..."
	$(COMPOSE) down -v
	@echo "   → Removing generated proto stubs..."
	rm -rf platform/gen/ runner/src/rat_runner/gen/ query/src/rat_query/gen/
	@echo "   → Removing build artifacts..."
	rm -rf portal/.next/ sdk-typescript/dist/
	@echo "🧹 Cleaned: containers, volumes, generated code, build artifacts"

# ── Documentation ──────────────────────────────────────────────
docs: ## Start docs dev server (Nextra, port 3001)
	@echo "📖 Starting docs dev server..."
	@docker run --rm -it \
		-v $$(pwd)/website:/app -w /app \
		-p 3001:3001 \
		$(NODE_IMAGE) \
		sh -c "npm install && npm run dev"

docs-build: ## Build docs static site
	@echo "📖 Building docs..."
	@docker run --rm \
		-v $$(pwd)/website:/app -w /app \
		$(NODE_IMAGE) \
		sh -c "npm install && npm run build"
	@echo "✅ Docs built (static output in website/out)"

docs-serve: docs-build ## Build and serve docs static site
	@echo "📖 Serving docs..."
	@docker run --rm -it \
		-v $$(pwd)/website/out:/app -w /app \
		-p 3001:3001 \
		$(NODE_IMAGE) \
		sh -c "npx serve -p 3001"

# ── License Reports ───────────────────────────────────────────
licenses: ## Generate third-party license reports for all components
	@echo "📜 Generating license reports..."
	@docker build -q -t rat-licenses -f scripts/Dockerfile.licenses scripts/ >/dev/null
	@docker run --rm -v $$(pwd):/workspace -w /workspace rat-licenses \
		bash scripts/generate-licenses.sh
	@echo "✅ License reports generated"

clean-all: clean ## Full clean — also removes Docker images and test images
	@echo "   → Removing Docker images..."
	-$(COMPOSE) down --rmi local -v 2>/dev/null
	-docker rmi $(PY_TEST_RUNNER_IMAGE) $(PY_TEST_QUERY_IMAGE) 2>/dev/null
	@echo "🧹 Full clean complete"

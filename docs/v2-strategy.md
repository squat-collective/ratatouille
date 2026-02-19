# RAT v2 — Strategic Direction

> Captured: Feb 2026
> Status: **Discussion / Pre-planning**
> v1: Python (FastAPI) — ship and learn
> v2: Go platform rewrite + Python runner containers

---

## Vision

**Community Edition** = the SQLite of data platforms. `docker compose up`, you're running.
No login, no config, no accounts. One user, full power. Everything in the browser.

**Pro Edition** = when you need teams, security, sharing, cloud. Delivered as closed-source
container plugins that snap into Community's extension points.

**Development model**: No local dev. No CLI. The web portal IS the IDE. Users write code
in the browser editor (backed by S3), run pipelines, test quality, view lineage — all from
the UI. CI/CD is built into the platform (run → test → promote), not external tools.

---

## Data Model: Namespaces + Per-Object Ownership

**Decision**: Namespace-based model with per-object ownership and sharing.

### Why Namespaces

- **No naming collisions** — two teams can both have `gold.revenue` in different namespaces
- **"Owner shares anything"** — any pipeline/table owner can share with anyone
- **Global discovery** — one catalog, one search, see everything you have access to
- **Global lineage** — full platform DAG across namespaces
- **Community simplicity** — one implicit namespace, invisible to user

### Namespace Model

```
namespace.layer.table_name

Community:  (implicit default namespace — user just sees layer.table)
  bronze.raw_orders
  silver.orders
  gold.revenue

Pro:  (explicit namespaces — like GitHub orgs)
  ecommerce.bronze.raw_orders        owner: alice
  ecommerce.silver.orders            owner: alice   shared_with: [bob:read]
  ecommerce.gold.revenue             owner: alice   shared_with: [bob:read, charlie:read]
  marketing.bronze.campaigns         owner: bob
  marketing.gold.attribution         owner: bob     shared_with: [alice:read]
```

- **Ownership**: every table and pipeline has an owner (the creator). Transferable.
- **Sharing**: owner grants per-object access — `SHARE gold.revenue WITH bob READ`
- **Projects** (soft grouping, Pro): organize tables/pipelines into logical groups. A table can belong to multiple projects. No isolation, just organization.
- **Namespaces** (Pro): organizational boundaries that prevent naming collisions. Users can belong to multiple namespaces. Cross-namespace sharing is natural.

### S3 Layout

```
s3://rat/
├── {namespace}/                        # "default" for Community
│   ├── pipelines/
│   │   ├── bronze/{pipeline_name}/
│   │   │   ├── pipeline.sql
│   │   │   ├── config.yaml
│   │   │   ├── pipeline.meta.yaml
│   │   │   └── tests/quality/*.sql
│   │   ├── silver/{pipeline_name}/...
│   │   └── gold/{pipeline_name}/...
│   ├── data/
│   │   ├── bronze/{table_name}/        # Iceberg table data
│   │   ├── silver/{table_name}/...
│   │   └── gold/{table_name}/...
│   └── docs/
│       └── README.md
└── ...                                 # other namespaces (Pro)
```

### Nessie Catalog Layout

- **Main branch**: All production tables. Flat namespace: `{namespace}.{layer}.{table_name}`
- **Dev branches**: For experimentation / preview. Users branch off main, test changes, merge back.
- Namespaces are NOT branches — they're part of the table path on `main`.

### How It Maps to Editions

| | Community | Pro |
|---|---|---|
| **Namespace** | One implicit (`default`), invisible | Multiple, user-managed (like GitHub orgs) |
| **Naming** | `layer.table` | `namespace.layer.table` |
| **Sharing** | Not needed (single user) | Owner shares anything with anyone |
| **Projects** | Not needed | Soft grouping for organization |
| **Discovery** | Browse your layers | Search/browse all tables you can access |
| **Permissions** | Allow-all | Per-object ACLs via plugin |

---

## Architecture: Microservices (Max Split)

All services are independent containers, orchestrated via `docker compose up`.

```
┌─────────────────────────────────────────────────────────────────┐
│                    docker compose up                              │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │  ratd    │  │  ratq    │  │  runner   │  │  portal          │ │
│  │  (Go)    │  │  (Python)│  │  (Python) │  │  (Next.js)       │ │
│  │          │  │          │  │           │  │                  │ │
│  │  API     │  │  DuckDB  │  │  Pipeline │  │  Web IDE         │ │
│  │  Auth    │◄─┤  Queries │  │  Execution│  │  Editor          │ │
│  │  Sched   │  │  Read-   │  │  DuckDB   │  │  Query           │ │
│  │  Plugins │  │  only    │  │  PyArrow  │  │  Pipelines       │ │
│  │  Catalog │  │          │  │  Iceberg  │  │  DAG / Explorer  │ │
│  └────┬─────┘  └──────────┘  └───────────┘  └──────────────────┘ │
│       │                                                           │
│  ┌────┴─────┐  ┌──────────┐  ┌──────────┐                       │
│  │ postgres │  │  minio   │  │  nessie   │                       │
│  │ (state)  │  │  (S3)    │  │ (catalog) │                       │
│  └──────────┘  └──────────┘  └──────────┘                       │
└─────────────────────────────────────────────────────────────────┘
```

### Service Responsibilities

| Service | Language | Role | Lifecycle |
|---------|----------|------|-----------|
| **ratd** | Go | API server, auth, scheduling, plugin host, catalog ops, executor orchestrator | Long-running |
| **ratq** | Python | Interactive DuckDB queries (read-only), schema introspection | Long-running sidecar |
| **runner** | Python | Pipeline execution, DuckDB writes, PyArrow, Iceberg, quality tests | Long-running (warm pool) |
| **portal** | Next.js | Web IDE — editor, query, pipelines, DAG, explorer. THE user interface | Long-running |
| **postgres** | — | Platform state: runs, schedules, ownership, plugin config | Long-running |
| **minio** | — | S3-compatible object storage | Long-running |
| **nessie** | — | Iceberg REST catalog with git-like branching | Long-running |

**7 containers total for Community**. One `docker compose up` starts everything.

### Communication

- **Portal → ratd**: REST API (HTTP)
- **ratd → ratq**: gRPC (query dispatch, schema introspection)
- **ratd → runner**: gRPC (pipeline submission, log streaming)
- **ratd → plugins**: gRPC (auth, sharing, executor, audit)
- **ratd → postgres**: SQL (platform state)
- **ratd → minio**: S3 API (file operations)
- **ratd → nessie**: Iceberg REST API (catalog operations)

---

## 1. Editions

### Community (Free, Open Source, Single-User)

Full-featured single-user data platform. Zero friction.

- Full ingestion flow (bronze layer, CSV/API/CDC sources)
- Full transform pipeline (silver/gold, SQL + Python)
- Full quality testing framework
- Full code execution (pipeline runs, scheduling, triggers)
- Full web IDE (editor, query, pipelines, DAG, explorer)
- Full query engine (DuckDB + Iceberg)
- S3 file management (MinIO)
- Namespace-based flat catalog — no limit on tables/pipelines
- No login required — open access, single user
- Plugin system — extensible by anyone
- `docker compose up` and go

### Pro (Paid, Closed-Source Container Plugins)

Everything in Community, plus:

- Authentication (OIDC / Keycloak / Cognito)
- Multi-user with roles (data engineer, analyst, maintainer)
- Multi-namespace (organizational boundaries)
- Per-object ownership + sharing (any owner shares anything)
- Projects (soft grouping for organization)
- Data catalog / discovery (search all accessible tables)
- Row-Level Security (auto-generated from sharing grants)
- Container-per-run execution isolation
- Cloud profiles (AWS / GCP deployment)
- Landing zones (shared inbox for data maintainers)
- Audit logging
- Support

---

## 2. Plugin Architecture

### Plugin = Container

Each plugin is a container image, not a Python package. The Go platform
communicates with plugins via gRPC.

```yaml
# rat.yaml
plugins:
  auth:
    image: ghcr.io/rat/plugin-auth-keycloak:latest
    config:
      issuer_url: http://keycloak:8180/realms/rat

  executor:
    image: ghcr.io/rat/plugin-executor-container:latest
    config:
      runtime: podman

  portal-pro:
    image: ghcr.io/rat/portal-pro:latest

  # Community default: no plugins = no auth, local executor, base portal
```

### Extension Points

| Slot | Community Default | Pro Plugin Examples |
|------|-------------------|--------------------|
| **Auth** | No-op (open access) | Keycloak OIDC (first), Cognito, Auth0 |
| **Enforcement** | Allow-all (single user) | API-level (ratd checks ACLs), S3 IAM (STS per-user for AWS) |
| **Permission** | Allow-all (single user) | Generic permission engine — relation tuples, action graphs, resource hierarchy, groups |
| **Sharing** | Allow-all (single user) | Per-object ownership, ACLs, projects |
| **RLS** | None | Row-Level Security (auto-generated from sharing grants) |
| **Executor** | WarmPoolExecutor (single warm runner) | ContainerExecutor (on-demand), LambdaExecutor, K8sJobExecutor |
| **Storage** | MinIO (S3 API) | AWS S3 native, GCS adapter |
| **Catalog** | Nessie | AWS Glue, BigLake |
| **Portal** | Base web IDE | Pro portal (extends base — sharing dialog, admin, audit viewer) |
| **Ingestion** | File upload, basic API | CDC connectors, streaming, bulk loaders |
| **Audit** | None | Audit logger, compliance reports |

### Plugin Interface (gRPC)

```protobuf
service AuthPlugin {
  rpc Authenticate(AuthRequest) returns (AuthResponse);
  rpc Authorize(AuthzRequest) returns (AuthzResponse);
}

service ExecutorPlugin {
  rpc Submit(PipelineSpec) returns (RunHandle);
  rpc Status(RunHandle) returns (RunStatus);
  rpc Logs(RunHandle) returns (stream LogLine);
  rpc Cancel(RunHandle) returns (CancelResponse);
}

service SharingPlugin {
  rpc Share(ShareRequest) returns (ShareResponse);
  rpc Revoke(RevokeRequest) returns (RevokeResponse);
  rpc ListAccess(AccessQuery) returns (AccessList);
  rpc Transfer(TransferRequest) returns (TransferResponse);
}

service EnforcementPlugin {
  rpc CanAccess(AccessRequest) returns (AccessResponse);
  rpc GetCredentials(CredentialRequest) returns (S3Credentials);  // for S3 IAM mode
}
```

### Portal Plugin Model

Pro portal is a **separate container image built FROM the community portal base**.

```dockerfile
# portal-pro/Dockerfile
FROM ghcr.io/rat/portal:latest
COPY pro-pages/ /app/src/app/(pro)/
COPY pro-components/ /app/src/components/pro/
RUN npm run build
```

- Community portal is the full open-source web IDE
- Pro portal image extends it with Pro pages (sharing dialog, admin dashboard, audit viewer)
- `ratd` exposes `GET /api/v1/features` → portal shows/hides nav items based on active plugins
- Pro UI code never touches the public repo

### Plugin Discovery & Distribution

- Plugins declared in `rat.yaml`
- Platform starts plugin containers as sidecars (or connects to existing)
- Health check on startup, graceful degradation if plugin unavailable
- Community with zero plugins = fully functional, zero overhead
- **Distribution**: ghcr.io private packages (token-gated). Local registry (`registry:2`) for dev.

---

## 3. Repository Structure

Community monorepo + separate Pro repo. Shared local dev folder.

```
~/rat/                                  # umbrella folder (not a repo)
│
├── rat/                                # PUBLIC repo — community (monorepo)
│   ├── platform/                       # Go — the brain
│   │   ├── cmd/ratd/                   # Single binary: API + scheduler + plugin host
│   │   ├── internal/
│   │   │   ├── api/                    # HTTP API (chi or echo router)
│   │   │   ├── auth/                   # Auth middleware + plugin slot
│   │   │   ├── executor/              # Dispatches runs to executor plugin or local
│   │   │   ├── scheduler/             # Cron / trigger engine
│   │   │   ├── plugins/               # Plugin loader + gRPC clients
│   │   │   ├── catalog/               # Nessie / Iceberg REST client
│   │   │   ├── ownership/             # Per-object ownership + sharing registry
│   │   │   └── storage/               # S3 operations (MinIO Go SDK)
│   │   ├── go.mod
│   │   └── Dockerfile                 # ~20MB final image (scratch + binary)
│   │
│   ├── runner/                         # Python — the muscle
│   │   ├── src/rat_runner/
│   │   │   ├── __main__.py            # Entrypoint (sys.path + serve)
│   │   │   ├── server.py              # gRPC RunnerServiceImpl (6 RPCs) + cleanup daemon
│   │   │   ├── executor.py            # 5-phase pipeline execution (branch → write → test → merge)
│   │   │   ├── engine.py              # DuckDB engine (S3 + Iceberg exts)
│   │   │   ├── templating.py          # Jinja SQL — ref(), this, is_incremental(), watermark
│   │   │   ├── iceberg.py             # PyIceberg writes (overwrite + merge) + watermark reads
│   │   │   ├── nessie.py              # Nessie v2 REST client (branch lifecycle)
│   │   │   ├── python_exec.py         # Python pipeline execution via exec()
│   │   │   ├── quality.py             # Quality test discovery + execution
│   │   │   ├── config.py              # S3Config, NessieConfig, YAML parsing, S3 listing
│   │   │   ├── models.py              # RunState, RunStatus, LogRecord, QualityTestResult
│   │   │   ├── log.py                 # RunLogger (deque + Python logging)
│   │   │   └── gen/                   # Generated gRPC stubs
│   │   ├── tests/unit/                # 140 tests, 91% coverage
│   │   ├── pyproject.toml
│   │   └── Dockerfile                 # ~200MB (Python + DuckDB + PyArrow)
│   │
│   ├── query/                          # Python — the reader
│   │   ├── src/rat_query/
│   │   │   ├── __main__.py            # Entrypoint (sys.path + serve)
│   │   │   ├── server.py              # gRPC QueryServiceImpl (4 RPCs) + shutdown
│   │   │   ├── engine.py              # Long-lived DuckDB engine (S3 + DDL lock)
│   │   │   ├── catalog.py             # NessieCatalog — discovery, view registration, 30s refresh
│   │   │   ├── arrow_ipc.py           # Arrow IPC serialization helpers
│   │   │   ├── config.py              # S3Config, NessieConfig (reuses runner pattern)
│   │   │   └── gen/                   # Generated gRPC stubs
│   │   ├── tests/unit/                # 52 tests, 87% coverage
│   │   ├── pyproject.toml
│   │   └── Dockerfile
│   │
│   ├── portal/                         # Next.js — the face (web IDE)
│   │   └── ...
│   │
│   ├── sdk-typescript/                 # TS SDK (for portal)
│   │
│   ├── proto/                          # Shared gRPC protobuf definitions
│   │   ├── common/v1/common.proto      # Shared types (Layer, RunStatus, GetRunStatus, etc.)
│   │   ├── runner/v1/runner.proto      # Runner service (6 RPCs)
│   │   ├── query/v1/query.proto        # Query service (4 RPCs)
│   │   ├── executor/v1/executor.proto  # Executor plugin service
│   │   ├── auth/v1/auth.proto          # Auth plugin
│   │   ├── cloud/v1/cloud.proto        # Cloud plugin
│   │   ├── plugin/v1/plugin.proto      # Base plugin interface
│   │   ├── sharing/v1/sharing.proto    # Sharing plugin
│   │   ├── enforcement/v1/enforcement.proto
│   │   └── permission/v1/permission.proto  # Permission engine (17 RPCs)
│   │
│   ├── infra/                          # Docker compose, configs
│   │   ├── docker-compose.yml          # Community: all 7 services
│   │   └── docker-compose.pro.yml      # Pro: override with plugin containers
│   │
│   └── docs/
│
└── rat-pro/                            # PRIVATE repo — Pro plugins only
    ├── plugins/
    │   ├── auth-keycloak/              # Go container
    │   ├── executor-container/         # Go container
    │   ├── sharing/                    # Go container — ownership, ACLs, projects
    │   └── cloud-aws/                  # Go container
    ├── portal-pro/                     # Pro portal extension (FROM community portal)
    └── infra/
        └── docker-compose.pro.yml      # Plugin containers compose
```

**Removed from v1**: `cli/`, `sdk-python/` (no local dev, no CLI — everything through web IDE).

---

## 4. Why Go

| Factor | Go | Python (current) |
|--------|----|--------------------|
| Binary size | ~10-15MB | ~200MB+ with deps |
| Memory baseline | ~20MB | ~100MB+ |
| Startup | Instant | Seconds |
| Concurrency | Goroutines (native) | asyncio (fragile) |
| Container ecosystem | Native (Docker SDK, K8s client-go, MinIO SDK) | Wrappers |
| Deployment | Single static binary | virtualenv + deps |
| Long-running server | Built for this | GIL, memory leaks |

Python stays for what it does best: data manipulation, DuckDB, PyArrow, Iceberg writes.

---

## 5. Execution Model

### Runner Contract

The Go platform dispatches pipeline runs to the Python runner via gRPC:

```
ratd (Go) ──── gRPC ────▶ rat-runner (Python container)
                              │
                              ├── Receives: pipeline spec, S3 creds, config
                              ├── Executes: DuckDB SQL or Python script
                              ├── Writes: results to S3/Iceberg
                              ├── Streams: logs + progress back via gRPC
                              └── Long-running: warm pool, 4 concurrent runs
```

### Query Contract

Interactive queries go through the long-running query sidecar:

```
portal ── REST ──▶ ratd (Go) ── gRPC ──▶ ratq (Python)
                                             │
                                             ├── DuckDB read-only queries
                                             ├── Schema introspection
                                             ├── Table preview / stats
                                             └── Long-lived, connection pooled
```

### Executor Implementations

Every executor is a plugin. Community ships with WarmPoolExecutor. Pro swaps in others.

| Executor | How | Startup | Edition |
|----------|-----|---------|---------|
| WarmPoolExecutor | Single pre-started runner sidecar, picks up jobs from queue | ~0 sec (warm) | Community default |
| ContainerExecutor | Fresh container per run (podman/docker), full isolation | ~3-5 sec (cold) | Pro plugin |
| LambdaExecutor | ratd invokes Lambda with runner image | ~1-2 sec (warm) | Pro plugin |
| K8sJobExecutor | ratd submits K8s Job manifest | ~5-10 sec | Pro plugin |

### Executor Interface (Implemented)

Defined in `platform/internal/api/executor.go`:

```go
type Executor interface {
    Submit(ctx context.Context, run *domain.Run, pipeline *domain.Pipeline) error
    Cancel(ctx context.Context, runID string) error
}
```

**WarmPoolExecutor** (`platform/internal/executor/warmpool.go`):
- Connects to runner via ConnectRPC (`RunnerServiceClient`)
- `Submit()`: sends `SubmitPipelineRequest`, updates run to `running` or `failed`
- `Cancel()`: sends `CancelRunRequest`, removes from active map
- Background goroutine polls `GetRunStatus` every 5s for active runs
- Terminal status (`success`/`failed`) → updates DB + removes from active map
- Wired in `main.go` when `RUNNER_ADDR` env var is set

### Runner Implementation (v2.1)

- Base: `python:3.12-slim` + DuckDB + PyArrow + PyIceberg + gRPC
- Long-running gRPC server on port 50052 (sync `grpcio`, not async)
- **4 pipeline workers** (ThreadPoolExecutor) + **10 gRPC server workers**
- One DuckDB connection per run (connections aren't thread-safe)
- In-memory run registry (`dict[str, RunState]`) — ratd owns truth in Postgres
- Bounded log deque (10K entries/run) for StreamLogs + Python logging for stdout
- Cancellation via `threading.Event` (cooperative, checked between phases)
- **Run cleanup daemon** — evicts terminal runs past `RUN_TTL_SECONDS` (default 3600)
- **Per-run STS credentials** — `s3_config` map in SubmitPipelineRequest for multi-tenant isolation
- **Ephemeral Nessie branches** — each run writes to `run-{run_id}`, merged to main on success
- **Pipeline types**: SQL (Jinja + DuckDB) and Python (`exec()` with injected globals)
- **Write modes**: 6 merge strategies — full refresh (overwrite), incremental (ANTI JOIN dedup), append only, delete+insert, SCD Type 2 (history tracking), snapshot (partition-aware). See ADR-014
- **Quality tests**: discovers `tests/quality/*.sql` from S3, 0 violation rows = pass, error severity blocks merge
- Entrypoint: `python -m rat_runner`
- See `docs/adr/005-runner-service.md` for full design rationale

### Scheduler (Implemented)

The scheduler runs as a background goroutine inside ratd (`platform/internal/scheduler/`).

- **Check interval**: 30 seconds (configurable via constructor)
- **Cron parser**: `robfig/cron/v3` (5-field: minute, hour, dom, month, dow)
- **Starts when**: both `DATABASE_URL` and `RUNNER_ADDR` are set (needs stores + executor)

**Tick cycle**:
1. Load all schedules from Postgres
2. Skip disabled schedules and invalid cron expressions
3. For schedules with nil `next_run_at`: compute it, persist, don't fire (first-time setup)
4. For schedules where `next_run_at <= now()`: create run, submit to executor, advance `next_run_at`

**Missed schedule policy**: Catch up once, then advance to future. If ratd was down for 3 hours
and a schedule was due, it fires exactly one run and sets `next_run_at` to the next future occurrence.
No backfill storm.

### Auth Middleware (Implemented)

Auth is a plugin slot on the `Server` struct:

```go
Auth func(http.Handler) http.Handler
```

- **Community**: `auth.Noop()` — passes every request through unchanged
- **Pro**: replace with real OIDC middleware from auth plugin container
- **Health endpoint** (`GET /health`): always unauthenticated (outside `/api/v1`)
- Applied via `r.Use(srv.Auth)` in the `/api/v1` route group

---

## 6. Ownership & Sharing Model (Pro Plugin)

### Per-Object Ownership

Every table and pipeline has an **owner** (the user who created it). Ownership is transferable.

```
ecommerce.bronze.raw_orders    owner: alice
ecommerce.silver.orders        owner: alice
ecommerce.gold.revenue         owner: alice
marketing.gold.attribution     owner: bob
```

### Sharing

Owners grant access per-object. Cross-namespace sharing is natural.

```
SHARE ecommerce.silver.orders WITH bob READ
SHARE ecommerce.gold.revenue WITH charlie READ
SHARE ecommerce.bronze.* WITH role:data-engineer WRITE
TRANSFER marketing.gold.attribution TO alice
```

### Projects (Soft Grouping)

Projects are labels, not containers. A table can belong to multiple projects.
No isolation — just organization for humans.

```
PROJECT ecommerce: [ecommerce.bronze.raw_orders, ecommerce.silver.orders, ecommerce.gold.revenue]
PROJECT marketing: [marketing.bronze.campaigns, marketing.gold.attribution, ecommerce.gold.revenue]
                                                                              ↑ shared across projects
```

### Access Resolution

1. No user in context (community) → **allow** (NoopAuthorizer)
2. Owner of resource → **allow** (checked locally in ratd Postgres)
3. Permission engine (v2.12+) → expand principals (user + groups + JWT roles) → expand actions (implication graph) → check direct grants → walk resource hierarchy → **allow** if match
4. Legacy sharing fallback → granted access level (read or write)
5. Default → **deny**

---

## 7. Web IDE (Portal)

The portal is the **only** user interface. No CLI, no local dev.

### Core Features (Community)

- **Code Editor**: CodeMirror 6 + SQL/Python syntax + autocomplete + `ref()` resolution
- **Query Console**: Interactive DuckDB queries with result grid
- **Pipeline Manager**: Create, configure, run, schedule pipelines
- **DAG Viewer**: Full lineage graph (Mermaid)
- **Data Explorer**: Browse tables, preview data, view schema
- **File Browser**: S3-backed tree view, create/edit/delete files
- **Quality Dashboard**: Test results, trends, severity badges
- **Run History**: Timeline of pipeline runs, logs, metrics
- **Markdown Docs**: README.md rendering + in-place editing

### Pro Portal Extension

Built FROM community base image, adds:

- **Sharing Dialog**: Share tables/pipelines with users, set permissions
- **Admin Dashboard**: User management, namespace management, resource usage
- **Audit Viewer**: Who did what, when, compliance trail
- **Namespace Switcher**: Switch between namespaces in nav

### Built-in CI/CD

No external CI/CD needed. The platform handles the full lifecycle:

```
Write code (editor) → Run pipeline → Quality tests auto-run
       ↓                    ↓                  ↓
  Save to S3        Results to Iceberg    Results to .meta.yaml
                                                  ↓
                                          Dashboard updates
```

- **Schedules**: Cron-based triggers, configured in UI
- **Sensors**: Watch for upstream changes, auto-trigger downstream
- **Promote**: Run in bronze → test → promote to silver (all in UI)
- **Rollback**: One-click rollback via Iceberg time-travel

---

## 8. Cloud Compatibility (Pro Plugins)

| Component     | Community (local)  | Pro Plugin (aws)   | Pro Plugin (gcp) |
|---------------|--------------------|--------------------|------------------|
| Storage       | MinIO              | S3 native          | GCS              |
| Catalog       | Nessie             | Glue / Nessie      | BigLake          |
| Compute       | LocalExecutor      | Lambda / ECS       | Cloud Run        |
| Auth          | No-op              | Cognito            | Cloud IAM        |
| Orchestration | Built-in scheduler | Step Functions     | Cloud Composer   |
| State DB      | Postgres           | RDS Postgres       | Cloud SQL        |

---

## 9. Sequencing

| Phase | Focus |
|-------|-------|
| **v1 (now)** | Ship working Python product, learn from it |
| **v2.0** | Go platform (`ratd`) — API ✅, auth middleware ✅, executor ✅, scheduler ✅, plugin host |
| **v2.1** | Python runner container + gRPC contract ✅ (140 tests, 91% coverage). Includes: incremental pipelines, Python pipelines, quality tests, ephemeral Nessie branches, run cleanup, per-run STS. See ADR-005 |
| **v2.2** | Python query service (`ratq`) + gRPC contract ✅ (52 tests, 87% coverage). Includes: long-lived DuckDB, Nessie catalog discovery, 30s background refresh, Arrow IPC serialization, Go ConnectRPC client. See ADR-006 |
| **v2.3** | Portal rewrite — full web IDE connected to Go API ✅ |
| **v2.4** | Plugin system foundation ✅ — `rat.yaml` config, proto definitions (auth, sharing, enforcement, plugin), plugin loader with health checks, auth middleware delegation, dynamic features. 31 new tests. See ADR-007 |
| **v2.5** | First Pro plugin: auth-keycloak ✅ — Go container (~26MB) validates Keycloak JWTs via ConnectRPC. JWKS cache with rate-limited refresh, OIDC discovery, background prefetch with retry. Claim mapping: sub→user_id, email, name→display_name, policy→roles. Authorize stub (CodeUnimplemented until v2.7). Docker Compose overlay pattern established. Adapted v1 realm JSON. 17 tests. See ADR-008 |
| **v2.6** | ContainerExecutor Pro plugin ✅ — per-run container isolation via Podman API. Runner single-shot mode (`RUN_MODE=single`), raw HTTP to Podman socket (~25MB image), PluginExecutor adapter in community, reaper for container cleanup. Warm runner disabled in Pro (`replicas: 0`). ~40 new tests. See ADR-009 |
| **v2.7** | ACL plugin: per-object ownership + sharing + enforcement. Authorizer abstraction in community (NoopAuthorizer), PluginAuthorizer in Pro. SQLite-backed ACL store. REST sharing endpoints. Permission hierarchy (admin > write > read). ~55 new tests. See ADR-010 |
| **v2.8** | Cloud plugin (AWS first) ✅ — STS credential vending (per-namespace scoped S3 creds via AssumeRole + inline policy), ECS Fargate executor (Submit/GetRunStatus/StreamLogs/Cancel), CloudWatch log streaming. New `cloud/v1/cloud.proto` with `CloudService.GetCredentials`. Runner STS support (session_token in S3Config, DuckDB, PyIceberg, boto3). Cloud plugin slot in ratd loader. ~33 plugin tests. See ADR-011 |
| **v2.9** | License gating + plugin distribution ✅ — RSA-256 signed JWT license keys, offline validation (no phone-home). Per-plugin entitlements via `plugins[]` claim. Single `RAT_LICENSE_KEY` env var. Plugins validate signature at startup, return `STATUS_NOT_SERVING` if unlicensed. ratd decodes JWT for display (no crypto deps in community). `pkg/license` shared Go module. `rat-license` CLI for key generation. Portal settings page with license status + expiry warning banner. ghcr.io distribution via GitHub Actions. ~10 license lib tests, ~3 CLI tests, ~4 handler tests per plugin, ~5 ratd decode tests. See ADR-012 |
| **v2.10** | Landing zones ✅ — standalone file drop areas for raw data uploads. Postgres-tracked metadata (zone name, namespace, file registry). S3 path: `{namespace}/landing/{zoneName}/{filename}`. 8 REST endpoints (`/api/v1/landing-zones`). DuckDB preview via existing `/query` endpoint (`read_csv_auto`, `read_json_auto`, `read_parquet`). Multipart upload (32MB max). TS SDK `LandingResource` (8 methods). Portal: zone list page, zone detail with drag-and-drop upload, file preview modal. ~14 handler tests, ~8 store tests. See ADR-013 |
| **v2.11** | Merge strategies ✅ — 6 write strategies (full_refresh, incremental, append_only, delete_insert, scd2, snapshot). Config merge: config.yaml base + annotation overrides per-field. 4 new Iceberg write functions (append, delete+insert, SCD2 history, partition snapshot). 4 new Jinja helpers (is_append_only, is_delete_insert, is_scd2, is_snapshot). Portal merge strategy settings card. SDK MergeStrategy + PipelineConfig types. ~30 new runner tests, full-stack typecheck. See ADR-014 |
| **v2.12** | Generic permission engine ✅ — Zanzibar-inspired relation tuples replace flat permission levels. New `permission/v1/permission.proto` (17 RPCs): resource type registration, action implication graphs (BFS), resource hierarchy inheritance, engine-managed groups (nested, depth limit 5), JWT/external role support, batch access checks, introspection queries. 3 principal types (user/group/role). Backward-compat: legacy SharingService dual-writes, EnforcementService falls back. SQLite schema: 6 new tables. ~83 new tests. See ADR-017 |

---

## 10. Resolved Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Plugin communication | **gRPC** via **ConnectRPC** (gRPC-compatible + HTTP/1.1 friendly) |
| 2 | Plugin distribution | **ghcr.io private** (token-gated) + **local registry** (`registry:2`) for dev |
| 3 | Portal plugins | **Separate container** built FROM community portal base |
| 4 | Runner ↔ ratd streaming | **gRPC streaming** (logs + progress) |
| 5 | Service split | **Max split** — 7 containers, one `docker compose up` |
| 6 | Portal embedded? | **No** — separate container, independent release cycle |
| 7 | Platform state DB | **Postgres** |
| 8 | CLI | **Removed** — all interaction through web IDE. REST API remains for programmatic access |
| 9 | v1 backward compat | **None** — fresh start, clean v2 |
| 10 | Naming collisions | **Namespaces** — `namespace.layer.table` (Community: implicit `default` ns) |
| 11 | Repo structure | **Community monorepo** + **Pro separate private repo** |
| 12 | Enforcement model | **Plugin slot** — API-level (Community/local) or S3 IAM (AWS). Auth plugin is separate (OIDC) |
| 13 | Auth first impl | **Keycloak (OIDC)** — standard protocol, swap to Cognito by changing `issuer_url` |
| 14 | Nessie branches | **Flat main branch** for production. Ephemeral per-run branches for write isolation + quality gating. Dev branches for experimentation |
| 15 | RLS | **Plugin, later** — not in v2.0. Sharing = full table access or nothing |
| 16 | Community executor | **WarmPoolExecutor** — single pre-started runner sidecar, ~0 sec startup |
| 17 | Pro executor | **On-demand** — fresh container per run (ContainerExecutor), fully pluggable |
| 18 | Service discovery | **Compose DNS** — `ratq:50051`, no service registry needed |
| 19 | Proto tooling | **buf.build** (linting, breaking change detection) + **ConnectRPC** framework |
| 20 | Feature flags | **`GET /api/v1/features`** — JSON with plugin status, edition, capabilities |
| 21 | Namespace creation | **Admin-only** — Community gets `default` auto-created |
| 22 | Data retention | **100 runs/pipeline** in Postgres, **10 in .meta.yaml**, **30d logs** in S3, all configurable |
| 23 | Postgres schema | **Done** — 14 tables defined in `docs/postgres-schema.sql` (12 original + landing_zones + landing_files) |
| 24 | S3 StorageStore | **MinIO Go SDK** (`minio-go/v7`) — `internal/storage/S3Store` implements `api.StorageStore`. Bucket auto-created on startup. 8 integration tests. See `docs/adr/001-s3-storage.md` |
| 25 | File upload flow | **Through ratd** — multipart POST `/api/v1/files/upload` (32MB max). No presigned URLs for Community. |
| 26 | Auth middleware | **Plugin slot** — `func(http.Handler) http.Handler` field on Server. Community: `auth.Noop()` (pass-through). Pro: swappable via plugin. See `docs/adr/002-auth-middleware.md` |
| 27 | Community executor | **WarmPoolExecutor** — dispatches to single warm runner via ConnectRPC. Polls status every 5s. See `docs/adr/003-warmpool-executor.md` |
| 28 | Scheduler | **Background goroutine** — 30s ticker, `robfig/cron/v3` for parsing. Missed schedules catch up once, then advance. See `docs/adr/004-scheduler.md` |
| 29 | Executor interface | **In `api/` package** — `Submit(ctx, run, pipeline) error` + `Cancel(ctx, runID) error`. Avoids import cycle. |
| 30 | Run dispatch | **Best-effort** — run is persisted in DB before executor dispatch. Executor failure doesn't lose the run. |
| 31 | Runner architecture | **Sync gRPC + ThreadPoolExecutor** — 4 pipeline workers, 10 gRPC workers. One DuckDB per run, in-memory state, bounded log deques. Cleanup daemon evicts terminal runs past TTL. See `docs/adr/005-runner-service.md` |
| 32 | Runner config | **Env vars** — S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, S3_USE_SSL, NESSIE_URL, GRPC_PORT, RUN_TTL_SECONDS. Per-run STS via `s3_config` map for Pro. |
| 33 | ref() resolution | **Parquet glob** for MVP — `read_parquet('s3://bucket/ns/layer/name/**/*.parquet')`. Future: `iceberg_scan()` once metadata paths standardized. |
| 34 | Pipeline types | **SQL + Python** — SQL via Jinja/DuckDB, Python via `exec()` with injected globals. `.py` checked first, then `.sql`. |
| 35 | Write modes | **6 merge strategies** — full_refresh, incremental, append_only, delete_insert, scd2, snapshot. Config merge: config.yaml base + annotation overrides per-field. See ADR-014 |
| 36 | Quality tests | **Post-write SQL tests** — discover from S3 `tests/quality/*.sql`, compile via Jinja, execute, 0 rows = pass. `-- @severity: error` blocks merge. |
| 37 | Nessie branches | **Ephemeral per-run** — `run-{run_id}` branch created, writes happen there, merged to main on success, deleted on quality failure. |
| 38 | ratq architecture | **Long-lived DuckDB sidecar** — single connection, Nessie v2 REST for table discovery, 30s background refresh, Arrow IPC serialization. DDL lock for view registration only. 52 tests, 87% coverage. See `docs/adr/006-query-service.md` |
| 39 | ratq table discovery | **Nessie REST API** — `GET /api/v2/trees/main/entries`, filter for ICEBERG_TABLE. Authoritative (no S3 listing). Tables registered as DuckDB views via `read_parquet()` glob. |
| 40 | Arrow IPC deserialization (Go) | **Apache Arrow Go** (`apache/arrow-go/v18`) — ratd deserializes Arrow IPC bytes from ratq into `[]map[string]interface{}` for JSON REST responses. Zero-copy friendly, type-preserving. |
| 41 | Plugin config format | **`rat.yaml`** — YAML with `edition` + `plugins` map. `RAT_CONFIG` env var > `./rat.yaml` > community defaults (no file needed). `gopkg.in/yaml.v3`. See ADR-007 |
| 42 | Plugin lifecycle | **ratd connects, never starts containers.** Compose/K8s manages lifecycle. `addr` field only (not `image`). See ADR-007 |
| 43 | Plugin health check | **Custom `PluginService.HealthCheck` RPC** — every plugin implements it. 5s timeout. Unhealthy → disabled with warning, ratd continues. See ADR-007 |
| 44 | Auth delegation | **Bearer token → `AuthService.Authenticate` RPC** — middleware extracts token, calls plugin, stores `UserIdentity` in context. Falls back to `auth.Noop()` when no plugin. See ADR-007 |
| 45 | Dynamic features | **`PluginRegistry` interface** — `Registry.Features()` returns actual plugin status. Portal uses `GET /api/v1/features` for dynamic UI. See ADR-007 |
| 46 | Pro executor | **ContainerExecutor** — fresh container per run via Podman API. Raw HTTP over Unix socket, no external deps. `RUNNER_IMAGE` env var for runner image. See ADR-009 |
| 47 | Runner single-shot mode | **`RUN_MODE=single`** — runner reads env vars, calls `execute_pipeline()`, prints JSON result to stdout, exits 0/1. No gRPC overhead. See ADR-009 |
| 48 | Executor plugin protocol | **`ExecutorService` (ConnectRPC)** — `Submit`, `GetRunStatus`, `StreamLogs`, `Cancel`. `PluginExecutor` adapter in community mirrors `WarmPoolExecutor` pattern. See ADR-009 |
| 49 | Container resource limits | **CFS quota** — CPU cores × period microseconds. Default 2.0 cores + 1GB memory. Configurable via env vars. |
| 50 | Container cleanup | **Reaper goroutine** — sweeps exited containers after TTL (default 10min). Orphan cleanup on startup (kill running + remove all with executor label). |
| 51 | Executor fallback | **Graceful degradation** — if executor plugin unhealthy, ratd falls back to WarmPoolExecutor. `Features()` reports "container" vs "warmpool". |
| 52 | Merge strategies | **6 strategies** — full_refresh (overwrite), incremental (ANTI JOIN dedup), append_only, delete_insert, scd2 (history tracking), snapshot (partition-aware). Config merge: config.yaml + annotations overlay. See ADR-014 |
| 53 | Config merge | **Annotations overlay config.yaml** — config.yaml is base, annotations override per-field. Enables both portal UI and power-user annotations. See ADR-014 |
| 54 | Template helpers | **Strategy-aware** — `is_incremental()`, `is_append_only()`, `is_delete_insert()`, `is_scd2()`, `is_snapshot()` Jinja functions for strategy-specific SQL. |
| 55 | Permission model | **Path-based grant tuples** — `(principal_type, principal_id, resource, verb)`. Resources are slash-delimited paths (e.g., `gold/pipeline/bronze/orders`). Explicit wildcards (`gold/*`) for cascading. See ADR-017 |
| 56 | Verb implications | **Global directed graph** — BFS traversal. `admin` → `{write, read, execute, publish, delete}`, `write` → `{read}`, `execute` → `{read}`, `publish` → `{read}`. Not per-resource-type. |
| 57 | Resource hierarchy | **Implicit via path structure** — no `RegisterResourceType` or `SetResourceParent`. `BuildCandidatePaths` generates wildcard patterns. Single SQL query for CheckAccess. |
| 58 | Group model | **Dual-source** — engine-managed groups (nested, depth limit 5) + JWT/external roles (always fresh from token). Three principal types: USER, GROUP, ROLE. |
| 59 | Permission backward compat | **Dual-write + fallback** — SharingService writes to both legacy and engine tables. EnforcementService checks engine first, falls back to legacy. `MigrateFromLegacy()` runs on startup. |

---

## 11. Open Questions (Remaining)

### Architecture

- [x] Runner warm pool: **4 concurrent workers** (ThreadPoolExecutor). Single runner container, not configurable yet. Pro swaps in ContainerExecutor for per-run isolation.
- [x] ratq connection pooling: **Single long-lived DuckDB connection**, DuckDB 1.0+ handles concurrent reads internally. `threading.Lock` for DDL only (view registration). 10 gRPC server workers. Memory limits not yet configurable.
- [x] Should ratd manage the runner lifecycle? **No** — compose `restart: unless-stopped` + gRPC healthcheck. ratd detects stale runs via polling.

### Proto & API Design

- [ ] Full proto message definitions (PipelineSpec, RunHandle, AccessRequest, etc.)
- [x] ratd REST API endpoint spec — done (`docs/api-spec.md`, 43 endpoints implemented)
- [x] ratq gRPC service definition — done (`proto/query/v1/query.proto`, 4 RPCs: ExecuteQuery, GetSchema, PreviewTable, ListTables). Arrow IPC for result serialization. See ADR-006
- [ ] Error handling strategy across gRPC services (status codes, error details)

### Data & Storage

- [x] Postgres schema design — done (`docs/postgres-schema.sql`, 12 tables)
- [ ] Migrations strategy (sqlc, goose, or manual?)
- [x] S3 metadata headers convention: **content-type auto-detected** by extension (.sql → `application/sql`, .py → `text/x-python`, .yaml → `application/x-yaml`, etc.). File type classification (`pipeline-sql`, `config`, `test`, etc.) returned in `ListFiles` response.
- [x] Iceberg table naming convention: **`namespace.layer.table_name`** as Nessie table path (dot-separated)
- [x] How does runner resolve `ref("silver.orders")`? **Jinja template** — `ref('layer.name')` → `read_parquet('s3://bucket/ns/layer/name/**/*.parquet')`. Cross-namespace: `ref('ns.layer.name')`. Future: Iceberg scan.

### Portal & UX

- [ ] Portal route map (which pages, navigation structure, which API calls per page)
- [ ] How does the portal handle real-time updates? (polling, WebSocket, SSE for run logs?)
- [ ] Editor experience: how does autocomplete resolve `ref()` calls? (ratq introspection?)
- [x] File upload flow: **through ratd** — multipart POST to `/api/v1/files/upload` (32MB max), ratd writes to S3. Simpler auth, no presigned URL complexity.

### DevOps & Tooling

- [ ] Docker compose file for 7 services (ports, volumes, health checks, dependencies)
- [ ] Dev workflow: how to develop platform + runner + portal simultaneously? (hot reload?)
- [ ] CI/CD for the community monorepo (GitHub Actions? What runs on PR?)
- [ ] Go module structure: single `go.mod` or per-package?

### Plugin System

- [x] Plugin lifecycle: **ratd connects, never starts containers.** Compose/K8s manages lifecycle. ratd connects to already-running gRPC endpoints. See ADR-007.
- [x] Plugin health check protocol: **Custom `PluginService.HealthCheck` RPC** — every plugin implements it. STATUS_SERVING = enabled, anything else = disabled with warning. 5s timeout. See ADR-007.
- [x] Plugin config validation: **Yes** — `rat.yaml` validated on load. Missing `addr` → error. Unknown plugin names → warning + skip. See `internal/config/`.
- [x] Can community users write their own plugins? **Yes** — proto definitions are in the public repo (`proto/plugin/v1/`, `proto/auth/v1/`, etc.). Any container implementing the gRPC interface can be loaded via `rat.yaml`.

---

## 12. What We Keep From v1

Not everything gets rewritten. These survive as-is or with minor adaptation:

- **Portal** (Next.js) — enhanced to be the full web IDE, talks to Go API
- **SDK-TypeScript** — for portal, updated for new API
- **Runner core logic** — DuckDB engine, PyArrow I/O, quality tests, SQL templating
- **Pipeline format** — `pipeline.sql`, `config.yaml`, `tests/` structure
- **S3 layout** — medallion layers, `.meta.yaml` sidecars (under namespace prefix)
- **Iceberg tables** — data is data, format doesn't change
- **UI theme** — underground/squat aesthetic carries forward

What gets rewritten in Go:
- API server (FastAPI → Go chi/echo)
- Auth system (JWT/Keycloak middleware → plugin slot)
- Ownership registry (replaces workspace manager)
- Pipeline discovery
- Run orchestration
- Scheduler/triggers

What gets removed:
- **CLI** (replaced by web IDE)
- **Python SDK** (no local dev)
- **Python API** (replaced by Go ratd)

---

*"Not everyone can become a great data engineer, but a great data platform can come from anywhere."* 🐀

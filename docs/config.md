# Configuration Reference

> All `ratd` configuration is done via environment variables.
> No config files, no CLI flags — just env vars in `docker-compose.yml`.

---

## Platform env vars vs plugin env vars

Two distinct concerns are documented in this file:

- **Platform env vars** — read by `ratd` (and its sidecars `ratq`, `runner`,
  `portal`) at boot. Examples: `DATABASE_URL`, `LISTEN_ADDR`,
  `INTERNAL_LISTEN_ADDR`, `RAT_HEARTBEAT_POOL_ENABLED`, `RUNNER_ADDR`,
  `S3_*`, `NESSIE_URL`, `QUERY_TIMEOUT_SECS`, `QUALITY_TEST_TIMEOUT_SECS`.
  These control the platform itself.
- **Plugin env vars** — read by individual plugin containers
  (`rat-plugin-*`). Examples: `PLUGIN_NAME`, `PLUGIN_ADDR`, `GRPC_PORT`,
  `RATD_URL`, `RATD_INTERNAL_URL`, `PLUGIN_ALLOW_LOOPBACK`, plus
  plugin-specific keys (`KEYCLOAK_URL`, `RAT_SECRETS_KEY`, …). The standard
  ones are read via `sdk.LoadPluginEnv()`; see
  [`docs/PLUGIN_AUTHOR_GUIDE.md`](PLUGIN_AUTHOR_GUIDE.md).

Section headings below indicate which side reads which variable.

---

## Postgres

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | No | — | Postgres connection string. If not set, ratd runs without persistence (in-memory, for dev/testing). |

**Format**: `postgres://user:password@host:port/dbname?sslmode=disable`

**Example**:
```
DATABASE_URL=postgres://rat:rat@postgres:5432/rat?sslmode=disable
```

Migrations run automatically on startup when `DATABASE_URL` is set.

---

## S3 Storage (MinIO)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `S3_ENDPOINT` | No | — | S3-compatible endpoint (`host:port`, no scheme). If not set, ratd runs without file storage. |
| `S3_ACCESS_KEY` | When `S3_ENDPOINT` set | — | Access key (MinIO root user or IAM key). |
| `S3_SECRET_KEY` | When `S3_ENDPOINT` set | — | Secret key (MinIO root password or IAM secret). |
| `S3_BUCKET` | No | `rat` | Bucket name. Auto-created on startup if missing. |
| `S3_USE_SSL` | No | `false` | Set to `"true"` for HTTPS (production/AWS S3). |
| `S3_METADATA_TIMEOUT` | No | (SDK default) | Per-request timeout for small metadata ops (stat/list). Accepts a Go duration, e.g. `15s`. Raise on slow/remote S3. |
| `S3_DATA_TIMEOUT` | No | (SDK default) | Per-request timeout for data transfers (get/put object). Go duration, e.g. `120s`. Raise for large objects over slow links. |

**Example**:
```
S3_ENDPOINT=minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=rat
S3_USE_SSL=false
```

The SDK derives `http://` or `https://` from `S3_USE_SSL` — don't include the scheme in `S3_ENDPOINT`.

---

## Runner Dispatch (ratd → runner)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RUNNER_ADDR` | No | — | ConnectRPC address of the runner service. If not set, pipeline runs are created but never dispatched. |

**Example**:
```
RUNNER_ADDR=runner:50052
```

When `RUNNER_ADDR` is set, ratd creates a `WarmPoolExecutor` that:
- Dispatches pipeline runs to the runner via ConnectRPC
- Polls runner for status updates every 5 seconds
- Updates run status in Postgres (running → success/failed)

When `RUNNER_ADDR` is **not** set, runs stay in `pending` status.

---

## Runner Service (Pipeline Execution)

> Configuration for the runner service itself (`rat-runner`).
> These env vars are read by the Python runner container, not ratd.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRPC_PORT` | No | `50052` | gRPC server listen port. |
| `S3_ENDPOINT` | No | `minio:9000` | S3-compatible endpoint (`host:port`, no scheme). |
| `S3_ACCESS_KEY` | No | `minioadmin` | S3 access key for reading pipeline code and writing results. |
| `S3_SECRET_KEY` | No | `minioadmin` | S3 secret key. |
| `S3_BUCKET` | No | `rat` | S3 bucket for pipeline code and Iceberg data. |
| `S3_USE_SSL` | No | `false` | Set to `"true"` for HTTPS (production/AWS S3). |
| `NESSIE_URL` | No | `http://nessie:19120/api/v1` | Nessie REST API URL for Iceberg catalog operations. |
| `RUN_TTL_SECONDS` | No | `3600` | Time-to-live for completed runs in the in-memory registry. A background cleanup thread evicts terminal runs (SUCCESS, FAILED, CANCELLED) older than this value every 60 seconds. |
| `QUERY_TIMEOUT_SECS` | No | `60` | Per-query DuckDB timeout (seconds) for pipeline SQL execution. Raise for long analytical pipelines; lower if you want a tighter SLA on hung queries. |
| `QUALITY_TEST_TIMEOUT_SECS` | No | `60` | Per-quality-test DuckDB timeout (seconds). Quality tests in `tests/quality/*.sql` are bounded by this. Raise if a heavy assertion legitimately needs more time. |

**Example**:
```
GRPC_PORT=50052
S3_ENDPOINT=minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=rat
S3_USE_SSL=false
NESSIE_URL=http://nessie:19120/api/v1
RUN_TTL_SECONDS=3600
```

The runner uses these to:
- Read `pipeline.sql` / `pipeline.py` and `config.yaml` from S3 (via boto3)
- Configure DuckDB httpfs extension for S3 access
- Write results to Iceberg tables via PyIceberg + Nessie catalog
- Create/merge/delete ephemeral Nessie branches per run (via Nessie v2 REST API)
- Discover and execute quality tests from S3 (`tests/quality/*.sql`)
- Evict stale terminal runs from memory after `RUN_TTL_SECONDS`

The runner's thread pool (4 pipeline workers) and gRPC server pool (10 workers) are not
yet configurable via env vars — hardcoded in `server.py`.

See `docs/adr/005-runner-service.md` for architecture decisions.

---

## Decoupled Data Architecture (ADR-024)

> Opt-in env vars for the decoupled Storage / Catalog / Engine composition. Read by
> both the runner and the query service (`rat-query`). When `RAT_ENGINE_MODE` is
> unset, both fall back to their embedded local paths (current default), so these
> are inert until you turn the composition on.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RAT_ENGINE_MODE` | No | (off) | Set to `1`/`true`/`yes` to route execution (runner) and reads (ratq) through the engine/v1 composition instead of the embedded DuckDB path. |
| `RAT_BINDINGS` | No | — | Path to a `rat.yaml`-shaped file declaring `data_planes` + `bindings`. Resolution is most-specific-wins (pipeline → layer → namespace → default). With no file, a single `default` plane is built from the `*_ADDR` vars below. |
| `ENGINE_ADDR` | No | `engine:50081` | gRPC address of the engine/v1 service (the compute axis). |
| `CATALOG_ADDR` | No | `catalog:50082` | gRPC address of the catalog/v1 service (descriptors + branch lifecycle + discovery). |
| `STORAGE_ADDR` | No | `storage:50083` | gRPC address of the storage/v1 service (vends storage descriptors). |

**Example** (single default composition):
```
RAT_ENGINE_MODE=true
ENGINE_ADDR=engine:50081
CATALOG_ADDR=catalog:50082
STORAGE_ADDR=storage:50083
```

With `RAT_BINDINGS` pointing at a file, a query or pipeline can span multiple
compositions (e.g. an Iceberg/Nessie default plane plus a DuckLake plane bound to
a namespace). See `docs/adr/024-decoupled-data-architecture.md`.

---

## Pipeline Config (`config.yaml`)

> Per-pipeline configuration stored at `{namespace}/pipelines/{layer}/{name}/config.yaml` on S3.
> Managed via the portal's Merge Strategy settings card or by editing the file directly.
> Source annotations (`@key: value` in pipeline.sql) can override any field — annotations win.

### Merge Strategy

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `merge_strategy` | string | `full_refresh` | Write strategy for the pipeline output. See table below. |

**Available strategies**:

| Strategy | Behavior | Required Fields |
|----------|----------|-----------------|
| `full_refresh` | Overwrite entire table each run | — |
| `incremental` | Merge new rows using unique key (dedup via ROW_NUMBER QUALIFY) | `unique_key`, `watermark_column` (optional) |
| `append_only` | Always append, never overwrite existing rows | — |
| `delete_insert` | Delete rows matching unique key, insert all new rows (no dedup) | `unique_key` |
| `scd2` | SCD Type 2 — track history with valid_from/valid_to columns | `unique_key`, `scd_valid_from`, `scd_valid_to` |
| `snapshot` | Replace only partitions present in new data | `partition_column` |

If a strategy requires a field (e.g., `unique_key`) but it's missing, the runner logs a warning
and falls back to `full_refresh`.

### Key & Column Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `unique_key` | list of strings | `[]` | Column names for key matching. Used by `incremental`, `delete_insert`, and `scd2` strategies. |
| `watermark_column` | string | `""` | Column used for incremental reads — runner reads `MAX(watermark_column)` from existing table to filter incoming data. Used by `incremental` strategy. |
| `partition_column` | string | `""` | Column identifying partitions. Used by `snapshot` strategy — only partitions present in new data are replaced. |
| `scd_valid_from` | string | `valid_from` | Column name for SCD2 "valid from" timestamp. Used by `scd2` strategy. |
| `scd_valid_to` | string | `valid_to` | Column name for SCD2 "valid to" timestamp. `NULL` means record is current. Used by `scd2` strategy. |

### Materialization & Archiving

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `materialized` | string | `table` | Output type: `table` or `view`. |
| `archive_landing_zones` | boolean | `false` | When `true`, archive associated landing zone files after pipeline run. |

### Other Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | string | `""` | Human-readable pipeline description. |

### Example `config.yaml`

```yaml
# Incremental pipeline with daily watermark
merge_strategy: incremental
unique_key:
  - id
  - email
watermark_column: updated_at
materialized: table

# SCD Type 2 example
# merge_strategy: scd2
# unique_key:
#   - customer_id
# scd_valid_from: effective_date
# scd_valid_to: expiry_date

# Snapshot (partition-aware) example
# merge_strategy: snapshot
# partition_column: report_date
```

### Config Merge: Annotations + config.yaml

The runner merges both config sources per-field:

1. **config.yaml** (from S3) is loaded as the base config
2. **Source annotations** (`@key: value` in pipeline.sql/pipeline.py) are parsed as overrides
3. Annotation values **win** over config.yaml values per-field
4. Missing annotation fields fall through to config.yaml values

This allows the portal UI to manage config.yaml while power users can fine-tune via annotations.

**Example**: config.yaml sets `merge_strategy: incremental`, but the pipeline source has
`-- @merge_strategy: full_refresh` — the annotation wins and the pipeline uses full refresh.

### Annotation Format

In SQL pipelines:
```sql
-- @merge_strategy: incremental
-- @unique_key: id, email
-- @watermark_column: updated_at
-- @partition_column: date
-- @scd_valid_from: valid_from
-- @scd_valid_to: valid_to
-- @materialized: table
-- @archive_landing_zones: true

SELECT * FROM {{ ref('bronze.raw_orders') }}
```

### Jinja Template Helpers

Strategy-aware helpers available in SQL pipelines:

| Function | Returns `True` when |
|----------|---------------------|
| `is_incremental()` | `merge_strategy == "incremental"` |
| `is_append_only()` | `merge_strategy == "append_only"` |
| `is_delete_insert()` | `merge_strategy == "delete_insert"` |
| `is_scd2()` | `merge_strategy == "scd2"` |
| `is_snapshot()` | `merge_strategy == "snapshot"` |

```sql
-- @merge_strategy: incremental
-- @unique_key: id
-- @watermark_column: updated_at

SELECT *
FROM {{ ref('bronze.raw_orders') }}
{% if is_incremental() and watermark_value %}
WHERE updated_at > '{{ watermark_value }}'
{% endif %}
```

See `docs/adr/014-merge-strategies.md` for architecture decisions.

---

## Scheduler

The scheduler has no dedicated env vars. It starts automatically when **both** conditions are met:
1. `DATABASE_URL` is set (needs `ScheduleStore` + `PipelineStore`)
2. `RUNNER_ADDR` is set (needs `Executor` to dispatch runs)

The scheduler evaluates enabled cron schedules every **30 seconds** and fires runs that are due.

---

## Query Dispatch (ratd → ratq)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RATQ_ADDR` | No | — | ConnectRPC address of the query service (ratq). If not set, query endpoints (`/query`, `/tables`) return 500 (nil QueryStore). |

**Example**:
```
RATQ_ADDR=http://ratq:50051
```

When `RATQ_ADDR` is set, ratd creates a `query.Client` that:
- Proxies `POST /query` → ratq `ExecuteQuery` RPC
- Proxies `GET /tables` → ratq `ListTables` RPC
- Proxies `GET /tables/:ns/:layer/:name` → ratq `GetSchema` RPC
- Proxies `GET /tables/:ns/:layer/:name/preview` → ratq `PreviewTable` RPC
- Deserializes Arrow IPC responses into JSON for the REST API

---

## Query Service (ratq)

> Configuration for the query service itself (`rat-query`).
> These env vars are read by the Python query container, not ratd.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRPC_PORT` | No | `50051` | gRPC server listen port. |
| `S3_ENDPOINT` | No | `minio:9000` | S3-compatible endpoint (`host:port`, no scheme). |
| `S3_ACCESS_KEY` | No | `minioadmin` | S3 access key for reading parquet data. |
| `S3_SECRET_KEY` | No | `minioadmin` | S3 secret key. |
| `S3_BUCKET` | No | `rat` | S3 bucket containing Iceberg table data. |
| `S3_USE_SSL` | No | `false` | Set to `"true"` for HTTPS (production/AWS S3). |
| `NESSIE_URL` | No | `http://nessie:19120/api/v1` | Nessie REST API URL for table discovery via Nessie v2 API. |

**Example**:
```
GRPC_PORT=50051
S3_ENDPOINT=minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=rat
S3_USE_SSL=false
NESSIE_URL=http://nessie:19120/api/v1
```

The query service uses these to:
- Configure DuckDB httpfs extension for S3 access (read-only)
- Discover tables via Nessie v2 REST API (`GET /api/v2/trees/main/entries`)
- Register DuckDB views for each discovered Iceberg table
- Refresh catalog every 30 seconds in a background thread
- Serialize query results as Arrow IPC for efficient transfer to ratd

The gRPC server pool (10 workers) is not yet configurable via env vars — hardcoded in `server.py`.

See `docs/adr/006-query-service.md` for architecture decisions.

---

## Nessie (Iceberg Catalog)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NESSIE_URL` | No | — | Nessie REST API URL. Used for catalog operations (table discovery, schema introspection). |

**Example**:
```
NESSIE_URL=http://nessie:19120/api/v1
```

---

## Server (platform)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RAT_LISTEN_ADDR` | No | `127.0.0.1:8080` | Public listener address (`host:port`) for end-user APIs. Bind to `0.0.0.0:8080` in compose / k8s. Default binds to localhost only — opening it to the network without `RAT_API_KEY` set logs a warning. |
| `PORT` | No | `8080` | Legacy single-port form. Used as `:${PORT}` when `RAT_LISTEN_ADDR` is unset. Prefer `RAT_LISTEN_ADDR` for new deployments. |
| `INTERNAL_LISTEN_ADDR` | No | `127.0.0.1:8090` | Private listener for service-to-service callbacks (`POST /api/v1/internal/runs/{id}/status`, `POST /api/v1/internal/plugins/register`). MUST NOT be exposed beyond the container network. Compose binds it to `0.0.0.0:8090` inside the network and `127.0.0.1:8090` on the host. Refuses to start if equal to `RAT_LISTEN_ADDR`. See [ADR-019](adr/019-internal-listener-split.md). |
| `RAT_API_KEY` | No | — | When set, every request to the public listener must carry `Authorization: Bearer <key>` or `X-API-Key: <key>`. The internal listener is unaffected (its auth model is network isolation). Use for single-tenant deployments behind a reverse proxy where you want a simple shared secret. For multi-user auth, install the auth plugin instead. |
| `CORS_ORIGINS` | No | — | Comma-separated list of allowed origins for CORS. Defaults to no CORS (same-origin only). Set to `http://localhost:3000` for portal-on-different-port dev setups, or your portal's public URL in production. |
| `RATE_LIMIT` | No | `100` | Requests per minute per client IP on the public listener. Set to `0` to disable. Applied after auth so authenticated requests share the per-IP budget. |
| `RAT_TRUSTED_PROXIES` | No | — | Comma-separated CIDRs / IPs of reverse proxies you trust (e.g. `10.0.0.0/8,192.168.1.5`). Only requests arriving directly from these peers have their `X-Forwarded-For` / `X-Real-IP` honored when ratd resolves the client IP (used for rate-limit keys and audit logging); everyone else is identified by their direct connection address. Empty (the default) trusts no proxy — the spoof-safe choice when ratd is bound directly. Set this to your proxy/load-balancer's address when running behind one, so per-IP rate limits and audit logs reflect the real client instead of the proxy. An invalid entry stops startup. |
| `SCHEDULER_ENABLED` | No | `true` | When `false`, ratd starts without the cron scheduler — useful for multi-replica deployments where only one instance should fire schedules. Pair with leader election (the `internal/leader` advisory-lock + heartbeat — see [ADR-023](adr/023-leader-heartbeat-dedicated-pool.md)). |
| `GRPC_TLS_CA` | No | — | CA cert file for verifying ratd's gRPC sidecars (ratq/runner/plugins). Set all three `GRPC_TLS_*` to enable mTLS on the gRPC transport; unset = plaintext h2c (fine inside a private network). |
| `GRPC_TLS_CERT` | No | — | Client cert file for mTLS to the gRPC sidecars. |
| `GRPC_TLS_KEY` | No | — | Client key file for mTLS to the gRPC sidecars. |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | No | — | When both are set, the public listener serves HTTPS instead of HTTP. Mutually inclusive (only one set → startup error). For typical deployments, prefer terminating TLS at a reverse proxy and leaving ratd on plain HTTP. |
| `RAT_HEARTBEAT_POOL_ENABLED` | No | `true` | When `true`, the leader heartbeat uses a dedicated 1-connection pgx pool so handler load can't starve it. Set to `false` for tiny deployments where one extra Postgres connection isn't worth it (falls back to the shared pool, loses the saturation guard). See [ADR-023](adr/023-leader-heartbeat-dedicated-pool.md). |
| `RAT_PPROF_ADDR` | No | — | Enables Go pprof endpoints (goroutine, heap, allocs, CPU profile, trace) on a dedicated listener. Disabled by default. **SECURITY**: pprof exposes sensitive runtime state — NEVER bind to a public interface. Use `127.0.0.1:6060` in production and access via SSH tunnel. |

---

## Licensing

RAT is **100% free and open-source** — there are no editions, tiers, or license keys. Every capability ships in this monorepo; the auth, executor, sharing, and cloud features below are free, optional plugins you install when you need them.

> **Legacy:** `EDITION` and `RAT_LICENSE_KEY` are retained only for backward compatibility with older deployments. They gate nothing and can be omitted. `GET /api/v1/features` no longer reports an edition. See ADR-012 (historical) for the retired license-gating design.

---

## Plugins (plugin-side env vars)

> Standard env vars every example plugin reads via `sdk.LoadPluginEnv()`.
> See [`docs/PLUGIN_AUTHOR_GUIDE.md`](PLUGIN_AUTHOR_GUIDE.md) for the full
> plugin-author guide.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PLUGIN_NAME` | No | (plugin's canonical short name) | The name the plugin registers with ratd. Usually left at default; override only when running the same image twice. |
| `PLUGIN_ADDR` | No | `<name>:<port>` | Address ratd dials back to reach the plugin. Override when the docker network name differs from the canonical short name. |
| `GRPC_PORT` | No | per-plugin | The HTTP/2 port the plugin binds. Named `GRPC_PORT` for historical reasons — it serves both ConnectRPC and the REST mux on the same h2c listener. |
| `RATD_URL` | No | `http://ratd:8080` | ratd's public API base URL. Plugins use this for `/api/v1/*` (sharing, capability registration, etc). |
| `RATD_INTERNAL_URL` | No | `RATD_URL` | ratd's internal listener base URL — where plugins POST phone-home. Falls back to `RATD_URL` when unset (single-port dev setups). In compose, set to `http://ratd:8090` so phone-home never crosses the public API. See [ADR-019](adr/019-internal-listener-split.md). |
| `PLUGIN_ALLOW_LOOPBACK` | No | `false` | Escape hatch for the Wave 2 SSRF guard on plugin registration: when `true`, ratd accepts a plugin address that resolves to loopback (127/8, ::1). Use ONLY for out-of-container local plugin development. Never enable in production — combined with the SSRF guard's other rejections (link-local, AWS metadata), this is the last line of defence against a hostile address ending up in the plugin registry. |

---

## Startup Wiring Order

`main.go` wires components in this order:

```
1. Logger (slog JSON)
2. Load rat.yaml (if RAT_CONFIG set)
   └── Plugin registry: health-check each plugin
3. Auth middleware (Plugin → AuthMiddleware, or Noop when no auth plugin)
4. Postgres stores (if DATABASE_URL set)
   └── PipelineStore, RunStore, NamespaceStore, ScheduleStore
5. S3 storage (if S3_ENDPOINT set)
   └── StorageStore
6. Executor:
   └── If executor plugin healthy → PluginExecutor
   └── Else if RUNNER_ADDR set → WarmPoolExecutor (local warm runner)
   └── Else → no executor (runs stay pending)
7. Query (if RATQ_ADDR set)
   └── query.Client → connects to ratq via ConnectRPC
8. Scheduler (if Executor available)
   └── Starts 30s ticker goroutine
9. HTTP server (chi router on PORT)
```

Each component is optional — ratd degrades gracefully when env vars are missing. This allows running a minimal ratd for development/testing without the full infrastructure stack. When no `rat.yaml` is present, steps 2-3 use defaults (no plugins, no-op auth).

---

## Portal (Next.js)

> Configuration for the Next.js portal (`rat-portal`).
> These env vars are set in the portal's Dockerfile or docker-compose.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXT_PUBLIC_API_URL` | No | `http://localhost:8080` | ratd REST API URL (browser-accessible). Used by the TypeScript SDK in the browser. |
| `NODE_ENV` | No | `production` | Node.js environment. Set to `production` for standalone builds. |
| `NEXT_TELEMETRY_DISABLED` | No | `1` | Disable Next.js anonymous telemetry. |
| `PORT` | No | `3000` | HTTP port for the portal. |
| `HOSTNAME` | No | `0.0.0.0` | Bind address for the portal server. |

**Example**:
```
NEXT_PUBLIC_API_URL=http://localhost:8080
NODE_ENV=production
PORT=3000
```

The portal is a **static Next.js standalone build** — no server-side API calls, no session management, and no auth by default. All API calls are made from the browser via the TypeScript SDK (`@squat-collective/rat-client`). (Auth is added by installing the auth plugin.)

---

## Auth Plugin: Keycloak

> Configuration for the optional `auth-keycloak` plugin container.
> These env vars are read by the plugin container, not ratd.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KEYCLOAK_URL` | Yes | — | Keycloak base URL (e.g., `http://keycloak:8180`). |
| `KEYCLOAK_REALM` | Yes | — | Keycloak realm name (e.g., `rat`). |
| `GRPC_PORT` | No | `50060` | ConnectRPC listen port. |
| `ISSUER_URL` | No | `{KEYCLOAK_URL}/realms/{REALM}` | Override issuer URL for JWT `iss` validation. Useful when the public issuer URL differs from the internal one. |

**Example**:
```
KEYCLOAK_URL=http://keycloak:8180
KEYCLOAK_REALM=rat
GRPC_PORT=50060
```

The plugin fetches OIDC discovery from `{ISSUER_URL}/.well-known/openid-configuration`,
caches JWKS RSA public keys, and validates JWTs locally (no per-request Keycloak call).

### ratd Plugin Config (`rat.yaml`)

ratd discovers the auth plugin via `rat.yaml` (set `RAT_CONFIG` env var to the file path):

```yaml
plugins:
  auth:
    addr: "http://auth-keycloak:50060"
```

See ADR-007 (plugin system) and ADR-008 (auth-keycloak) for architecture details.

---

## Executor Plugin: ContainerExecutor

> Configuration for the optional `executor-container` plugin container.
> These env vars are read by the plugin container, not ratd.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRPC_PORT` | No | `50070` | ConnectRPC listen port. |
| `RUNNER_IMAGE` | **Yes** | — | Docker/Podman image for spawned runner containers (e.g., `infra-runner:latest`). |
| `PODMAN_SOCKET` | No | `/run/podman/podman.sock` | Path to the Podman API Unix socket. |
| `CONTAINER_NETWORK` | No | `infra_default` | Docker/Podman network to attach spawned runners to. Must match the compose network so runners can reach MinIO/Nessie. |
| `CONTAINER_CPU_LIMIT` | No | `2.0` | CPU cores per runner container (CFS quota). |
| `CONTAINER_MEMORY_LIMIT` | No | `1073741824` | Memory limit in bytes per runner container (default 1GB). |
| `REAPER_INTERVAL` | No | `60s` | How often the reaper sweeps for exited containers to clean up. |
| `CONTAINER_TTL` | No | `600s` | Time to keep exited runner containers before the reaper removes them. |
| `S3_ENDPOINT` | **Yes** | — | Injected into spawned runner containers for S3 access. |
| `S3_ACCESS_KEY` | **Yes** | — | Injected into spawned runner containers. |
| `S3_SECRET_KEY` | **Yes** | — | Injected into spawned runner containers. |
| `S3_BUCKET` | No | `rat` | Injected into spawned runner containers. |
| `S3_USE_SSL` | No | `false` | Injected into spawned runner containers. |
| `NESSIE_URL` | **Yes** | — | Injected into spawned runner containers for Iceberg catalog operations. |

**Example**:
```
GRPC_PORT=50070
RUNNER_IMAGE=infra-runner:latest
PODMAN_SOCKET=/run/podman/podman.sock
CONTAINER_NETWORK=infra_default
CONTAINER_CPU_LIMIT=2.0
CONTAINER_MEMORY_LIMIT=1073741824
REAPER_INTERVAL=60s
CONTAINER_TTL=600s
S3_ENDPOINT=minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=rat
NESSIE_URL=http://nessie:19120/api/v1
```

The plugin spawns a fresh runner container per pipeline run via the Podman REST API.
Each runner gets `RUN_MODE=single` injected automatically, plus the run parameters
(`RUN_ID`, `NAMESPACE`, `LAYER`, `PIPELINE_NAME`, `TRIGGER`) and base infra config
(S3, Nessie). Per-run S3 overrides from `s3_config` in the `SubmitRequest` are also
injected, enabling per-namespace STS credential isolation.

### Runner Single-Shot Mode

When the executor plugin spawns a runner, it sets `RUN_MODE=single`. In this mode,
the runner skips gRPC server startup and instead:

1. Reads pipeline config from env vars
2. Calls `execute_pipeline()` (same logic as gRPC mode)
3. Prints a JSON result to stdout: `{"status":"success","rows_written":1234,"duration_ms":5678}`
4. Exits with code 0 (success) or 1 (failure)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RUN_MODE` | No | `server` | `"single"` for single-shot execution, `"server"` for gRPC server. |
| `RUN_ID` | When `RUN_MODE=single` | — | UUID of the run (set by executor plugin). |
| `NAMESPACE` | When `RUN_MODE=single` | — | Target namespace. |
| `LAYER` | When `RUN_MODE=single` | — | Target layer (`bronze`, `silver`, `gold`). |
| `PIPELINE_NAME` | When `RUN_MODE=single` | — | Pipeline name. |
| `TRIGGER` | No | `manual` | Trigger source (e.g., `manual`, `schedule:hourly`). |

### ratd Plugin Config

ratd discovers the executor plugin via `rat.yaml`:

```yaml
plugins:
  executor:
    addr: "http://executor-container:50070"
```

When the executor plugin is enabled, ratd uses `PluginExecutor` instead of `WarmPoolExecutor`.
If the plugin is unavailable (unhealthy), ratd falls back to `WarmPoolExecutor` when
`RUNNER_ADDR` is set.

See ADR-009 (container executor) for architecture details.

---

## Docker Compose

### Default stack

See `infra/docker-compose.yml` for the full 7-service setup with all env vars pre-configured. `docker compose up` starts everything with sensible defaults.

| Service | Port(s) |
|---------|---------|
| ratd | 8080 (REST), 8081 (gRPC) |
| ratq | 50051 (gRPC) |
| runner | 50052 (gRPC) |
| portal | 3000 (HTTP) |
| postgres | 5432 |
| minio | 9000 (S3 API), 9001 (Console) |
| nessie | 19120 (REST) |

### Optional plugin overlay

The auth, executor, sharing, and cloud plugins run as an overlay on the default compose:

```bash
docker compose \
  -f rat/infra/docker-compose.yml \
  -f rat-pro/infra/docker-compose.pro.yml \
  up -d
```

This adds the following services and disables the warm runner (`replicas: 0`):

| Service | Port(s) | Description |
|---------|---------|-------------|
| keycloak | 8180 (HTTP) | OIDC provider (realm auto-imported) |
| auth-keycloak | 50060 (gRPC) | JWT validation plugin (v2.5) |
| executor-container | 50070 (gRPC) | Container-per-run executor plugin (v2.6) |
| acl | 50080 (gRPC) | Sharing + enforcement plugin (v2.7) |

### AWS Cloud (Overlay)

For AWS deployments, add the `docker-compose.aws.yml` overlay to replace local executor
with cloud-aws (STS + ECS Fargate):

```bash
docker compose \
  -f rat/infra/docker-compose.yml \
  -f rat-pro/infra/docker-compose.pro.yml \
  -f rat-pro/infra/docker-compose.aws.yml \
  up -d
```

This disables `executor-container` and adds:

| Service | Port(s) | Description |
|---------|---------|-------------|
| cloud-aws | 50090 (gRPC) | STS credential vending + ECS Fargate executor (v2.8) |

---

## ACL Plugin: Sharing + Enforcement

> Configuration for the optional `acl` plugin container.
> These env vars are read by the plugin container, not ratd.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRPC_PORT` | No | `50080` | ConnectRPC listen port. |
| `DB_PATH` | No | `/data/acl.db` | Path to the SQLite database for access grants. |

**Example**:
```
GRPC_PORT=50080
DB_PATH=/data/acl.db
```

The plugin stores access grants in SQLite (pure Go, no CGO). Both `SharingService`
and `EnforcementService` run on the same port — ConnectRPC routes by service path.

### Permission Hierarchy

`admin > write > read`:
- An `admin` grant allows everything including `delete` and `admin` actions
- A `write` grant allows both `write` and `read` actions
- A `read` grant allows `read` only

### ratd Plugin Config

ratd discovers the ACL plugin via `rat.yaml`. Both `sharing` and `enforcement`
point to the same container:

```yaml
plugins:
  sharing:
    addr: "http://acl:50080"
  enforcement:
    addr: "http://acl:50080"
```

When the enforcement plugin is enabled, ratd creates a `PluginAuthorizer` that:
1. Checks ownership locally (pipeline owner in Postgres)
2. Delegates to the enforcement plugin for sharing grants
3. Denies access if neither check passes

See ADR-010 (ACL plugin) for architecture details.

---

## Cloud Plugin: AWS

> Configuration for the optional `cloud-aws` plugin container.
> This plugin provides both `CloudService` (STS credential vending) and `ExecutorService` (ECS Fargate execution).
> Both services run on the same port — ConnectRPC routes by service path.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRPC_PORT` | No | `50090` | ConnectRPC listen port. |
| `AWS_REGION` | **Yes** | — | AWS region (e.g., `us-east-1`). |
| `STS_ROLE_ARN` | **Yes** | — | IAM role ARN to assume for per-namespace scoped credentials. |
| `STS_SESSION_DURATION` | No | `3600` | STS session duration in seconds (900–43200). |
| `S3_BUCKET` | **Yes** | — | Target S3 bucket for pipeline data. |
| `S3_ENDPOINT` | No | — | Custom S3 endpoint override (empty = real AWS S3). |
| `ECS_CLUSTER` | **Yes** | — | ECS cluster name or ARN. |
| `ECS_TASK_DEFINITION` | **Yes** | — | ECS task definition family:revision (runner image). |
| `ECS_SUBNETS` | **Yes** | — | Comma-separated subnet IDs for Fargate tasks. |
| `ECS_SECURITY_GROUPS` | **Yes** | — | Comma-separated security group IDs. |
| `ECS_ASSIGN_PUBLIC_IP` | No | `DISABLED` | `ENABLED` or `DISABLED` — whether Fargate tasks get public IPs. |
| `ECS_LOG_GROUP` | No | `/rat/runs` | CloudWatch log group for pipeline log streaming. |
| `NESSIE_URL` | **Yes** | — | Nessie REST API URL injected into spawned runner tasks. |

**Example**:
```
GRPC_PORT=50090
AWS_REGION=us-east-1
STS_ROLE_ARN=arn:aws:iam::123456789012:role/rat-runner
STS_SESSION_DURATION=3600
S3_BUCKET=rat-data
ECS_CLUSTER=rat
ECS_TASK_DEFINITION=rat-runner:1
ECS_SUBNETS=subnet-abc123,subnet-def456
ECS_SECURITY_GROUPS=sg-123456
ECS_ASSIGN_PUBLIC_IP=DISABLED
ECS_LOG_GROUP=/rat/runs
NESSIE_URL=http://nessie.internal:19120/api/v1
```

### STS Credential Scoping

The plugin calls `STS AssumeRole` with an inline IAM policy that restricts S3
access to the requesting namespace's prefix within the bucket:

- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `BUCKET/NAMESPACE/*`
- `s3:ListBucket` on `BUCKET` with `s3:prefix` condition `NAMESPACE/*`

This ensures each pipeline run can only read/write data within its own namespace.

### ECS Fargate Execution

The plugin spawns a Fargate task per pipeline run with environment overrides:

| Env Var | Value |
|---------|-------|
| `RUN_MODE` | `single` |
| `RUN_ID` | UUID of the run |
| `NAMESPACE` | Target namespace |
| `LAYER` | Target layer (`bronze`, `silver`, `gold`) |
| `PIPELINE_NAME` | Pipeline name |
| `TRIGGER` | Trigger source |
| `NESSIE_URL` | Nessie REST API URL |
| `S3_ACCESS_KEY` | STS temporary access key |
| `S3_SECRET_KEY` | STS temporary secret key |
| `S3_SESSION_TOKEN` | STS session token |

### Runner STS Support

The runner (`rat-runner`) reads `S3_SESSION_TOKEN` and uses it with:

- **DuckDB**: `SET s3_session_token = ?;`
- **PyIceberg**: `s3.session-token` catalog property
- **boto3**: `aws_session_token` parameter

### ratd Plugin Config

ratd discovers the cloud-aws plugin via `rat.yaml`. Both `cloud` and
`executor` point to the same container:

```yaml
plugins:
  cloud:
    addr: "http://cloud-aws:50090"
  executor:
    addr: "http://cloud-aws:50090"
```

### AWS Cloud Compose Overlay

For AWS deployments, use the `docker-compose.aws.yml` overlay which replaces the
local `executor-container` with `cloud-aws`:

```bash
docker compose \
  -f rat/infra/docker-compose.yml \
  -f rat-pro/infra/docker-compose.pro.yml \
  -f rat-pro/infra/docker-compose.aws.yml \
  up -d
```

See ADR-011 (cloud-aws plugin) for architecture details.

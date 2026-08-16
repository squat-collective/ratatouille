# ADR-025: On-demand, fully decoupled data planes (format axis + plane manager)

## Status: Proposed (2026-05-30) — design questions resolved, ready for Phase 0 spike

## Context

ADR-024 decomposed the data tier into three plugin axes (Storage / Catalog /
Engine) with Format as an in-engine *capability*. That shipped: each axis is its
own gRPC service today, mix-and-match works at the protocol level, and the
runner is a pure orchestrator. **What it did not do** is decouple delivery from
the engine process or decouple plane *composition* from `docker-compose.yml`:

- The format adapters (`rat-format-iceberg`, `rat-format-ducklake`) are pip-
  installed into the engine container at build time. Adding a format = a new
  engine image + redeploy. Operators cannot drop in a third format at runtime.
- The compose stack hosts exactly **one** engine, **one** catalog, **one**
  storage. Multiple planes require multiple compose entries written by hand,
  pre-deployed, and always running.
- The portal has no awareness of planes. There is no API to declare or
  inspect them; there is no UI to bind a pipeline to one.

The asked-for end state is **full decoupling, on demand**: every axis (Storage
/ Catalog / Engine / **Format**) is an independent service; composition is
declarative; instances are spun up when a pipeline asks for them and torn
down when idle; an operator can install a brand-new axis plugin without
touching `docker-compose.yml` or restarting the platform.

## Decision

Three changes, in this order.

### 1. Format becomes a fourth axis (new `format/v1` contract)

A FormatService converts engine-produced Arrow batches into format-native
storage layout (Iceberg metadata + Parquet, DuckLake postgres + Parquet, …),
and resolves a logical table ref back into something the engine can scan.

```protobuf
service FormatService {
  // Persist a result-set as an N-th snapshot of a logical table.
  // The format service owns the on-disk layout, snapshot metadata, and
  // any per-strategy machinery (merge, scd2, snapshot).
  rpc Write(WriteRequest) returns (WriteResponse);

  // Resolve a logical ref ("layer.table"@snapshot) to an engine-readable
  // scan: a list of object URIs + filter pushdown + schema. Engines stay
  // dialect-pure (no iceberg_scan() in DuckDB, no read_iceberg in spark).
  rpc Resolve(ResolveRequest) returns (ResolveResponse);

  // Optional housekeeping: compact, expire snapshots, rewrite manifests.
  // Hot path is Write/Resolve; Maintain is for the compaction plugin et al.
  rpc Maintain(MaintainRequest) returns (MaintainResponse);
}
```

Key idea: **the format service tells the engine what files to read**, not how
to read them. `ResolveResponse` returns `{files: [s3://…], schema, predicates,
projection}`. The engine reads Parquet (or whatever the format leaves on disk)
through its native S3 path. This avoids baking format-specific SQL
(`iceberg_scan()`, `read_iceberg()`) into engines.

`rat-format-iceberg` and `rat-format-ducklake` become standalone services; the
engine ceases to depend on `pyiceberg`, `s3fs`, or `duckdb-ducklake`.

### 2. Plane Manager — process lifecycle for the whole data tier

The plane manager is a **ratd subsystem** (not its own service) — registry +
binding resolution + lifecycle policy live next to the pipeline model in
postgres. It talks to a separate **`plane-runtime-proxy`** service that owns
the container-runtime socket (docker / podman). The proxy is the only
component that touches the socket; ratd never does directly.

```
   portal
      │ REST
      ▼
    ratd  ───────────────  registry  ─┐
      │                    lifecycle  │  one binary, one postgres,
      │                    bindings   │  one deploy unit
      │  HTTP + bearer token   ───────┘
      ▼
   plane-runtime-proxy  ◄── allowlist:
      │                     pull / run / stop / inspect / health
      ▼
   docker.sock  /  podman.sock  /  (later: k8s API)
```

The plane-manager → proxy contract is **k8s-shaped lowest-common-denominator**
(`image` ref by digest, `env`, `resource_limits`, `healthcheck`, `ports`,
`labels`). Today the proxy implements it against `docker.sock`; a future
k8s-talking proxy is a drop-in swap, plane manager unchanged. The proxy
neither knows nor cares about RAT semantics — it's a constrained container
API.

Responsibilities:

- **Registry** — what plane *kinds* are available (image refs by digest,
  default configs, capabilities), and what plane *instances* are currently
  running (addr, last-used, refcount). Persisted in postgres alongside the
  pipeline / binding tables.
- **Spawn / kill** — calls proxy `POST /containers` with the k8s-shaped
  spec; proxy returns address + healthcheck future. Operators register a
  new plane kind by `POST`-ing an image ref + JSON descriptor to ratd; no
  compose edits, no restart.
- **Discovery** — the runner, on resolving a binding, asks ratd for
  `(engine_addr, catalog_addr, storage_addr, format_addr)`. ratd returns
  the addresses of an already-running plane *or* spawns one (via proxy) and
  waits-for-healthy *or* returns `202 { state: warming, eta_seconds: N }`
  if the caller can take async.
- **Eviction** — **idle timeout per plane kind + min-instances pin**.
  Each kind has `min_instances` (default plane: 1; everything else: 0) and
  `idle_timeout` (default plane: 10 min; everything else: 5 min). Reference
  counts on running pipelines prevent mid-flight kills. Default plane stays
  warm; long tail goes cold.
- **Cold-start UX** — plane-not-running returns `202` with `state:
  warming`, `eta_seconds`, `plane_id`. Portal renders a "Warming plane…"
  badge and polls `GET /planes/{id}/status`. The **scheduler pre-warms**
  planes T-2min before each scheduled run, so scheduled work never pays
  cold-start latency; only ad-hoc work (ratq, manual portal triggers) can.
- **Multi-tenant** — out of scope for core. The plane API exposes the same
  decision-hook pattern as `permission/v1` / `sharing/v1`: a tenant plugin
  (when installed) gets called at list/bind boundaries and filters /
  scopes. With no tenant plugin: every operator sees every plane.

```
   PIPELINE RUN
       │
       ▼
   runner ──── BindingConfig.resolve ───┐
                                        ▼
                              ┌─── plane-manager ───┐
                              │ already running?    │
                              │   yes → return      │
                              │   no  → spawn + wait│
                              └──────────┬──────────┘
                                         ▼
                              (engine, catalog, storage, format)
                                         │
                                         ▼
                                  pipeline runs
```

### 3. Composition becomes a first-class resource

`data_planes` move out of `rat.yaml` and into the **platform DB** + a ratd
REST API. Operators (or the portal) can CRUD planes. Each plane is a
composition of `(engine_kind, catalog_kind, storage_kind, format_kind)` +
per-axis config. Bindings (which pipeline / layer / namespace → which plane)
become per-pipeline metadata stored alongside the pipeline row.

Tables (single-tenant by design, tenant plugin overlays scoping later):

```sql
CREATE TABLE plane_kinds (
  kind         TEXT PRIMARY KEY,    -- 'rat-format-iceberg', 'rat-catalog-nessie', ...
  axis         TEXT NOT NULL,       -- 'format' | 'catalog' | 'storage' | 'engine'
  image_ref    TEXT NOT NULL,       -- ghcr.io/.../...@sha256:... (pinned)
  config_schema JSONB NOT NULL,     -- JSON Schema for per-instance config
  capabilities JSONB NOT NULL,      -- {languages, formats, dialects, ...}
  registered_at TIMESTAMP NOT NULL
);

CREATE TABLE planes (
  id           UUID PRIMARY KEY,
  name         TEXT UNIQUE NOT NULL,
  engine_kind  TEXT NOT NULL REFERENCES plane_kinds(kind),
  catalog_kind TEXT NOT NULL REFERENCES plane_kinds(kind),
  storage_kind TEXT NOT NULL REFERENCES plane_kinds(kind),
  format_kind  TEXT NOT NULL REFERENCES plane_kinds(kind),
  config       JSONB NOT NULL,      -- per-axis overrides
  min_instances INT NOT NULL DEFAULT 0,
  idle_timeout_sec INT NOT NULL DEFAULT 300,
  created_at   TIMESTAMP NOT NULL
);

CREATE TABLE bindings (
  scope_type TEXT NOT NULL,         -- 'default' | 'namespace' | 'layer' | 'pipeline'
  scope_key  TEXT NOT NULL,         -- '' | 'shop' | 'shop.silver' | pipeline_id
  plane_id   UUID NOT NULL REFERENCES planes(id),
  PRIMARY KEY (scope_type, scope_key)
);
```

That unlocks: a pipeline-settings dropdown that lists planes, a "+ New plane"
wizard, drift between planes shown as plain-old DB diff. Tenant-aware
filtering is added later by a `rat-plugin-tenant` that registers a
decision-hook against the plane list / bind endpoints (same shape as
`permission/v1` / `sharing/v1`).

## Consequences

**Positive.** Drop-in plugins at runtime: install a `rat-format-deltalake`
container, ratd's plane manager discovers the new kind, operators compose
planes that use it — zero rebuild, zero restart. Multi-tenant becomes
tractable (per-org plane catalogs, per-pipeline isolation). Cost model
matches usage (warm pool tunable; cold-start tax only on the long tail).
The platform's "anyone can add a connector" identity extends through every
on-disk format, not just every protocol.

**Negative — accepted.**
1. **Latency tax on every write.** Engine → format service → storage adds
   1-2 hops; Arrow IPC over local gRPC is fast but real. Hot-path budget
   needs measurement before commit (Phase 0 spike).
2. **Container orchestration from inside containers.** Either docker-socket
   access (privileged blast radius) or a small mediating proxy (more code).
   Either way, ratd's threat model expands.
3. **Operational complexity.** This is mini-Kubernetes: a registry, a
   scheduler, a healthchecker, an evictor. At some scale, the right answer
   is "actually run on Kubernetes" — ADR needs to call out the off-ramp.
4. **Cold-start UX.** First request to a cold plane waits 5-30s. Warm pools
   mask it for the common case but the failure mode (`503 cold_start_in:
   12s`) needs portal support.
5. **Failure modes multiply.** Orphan containers if plane-manager dies,
   eviction races, image-pull failures mid-spawn. Needs a recovery model
   (reconciliation loop on startup, leader heartbeat à la ADR-023).

**Neutral.** Wire-protocol package names follow house convention
(`ratatouille.format.v1`, etc.) and are frozen on release.

## Resolved design decisions

Worked through with Tom on 2026-05-30. Each was a real fork; what's
locked in is below, with the rejected alternative noted so the rationale
survives.

| # | Decision | Rejected | Why |
|---|---|---|---|
| **D1** | **Mediating proxy** (`plane-runtime-proxy`) owns the container socket. ratd → proxy via HTTP + bearer token; allowlisted ops only. | Direct `docker.sock` from ratd; or a `ContainerRuntime` SDK-with-backends inside ratd. | Real security boundary without writing a runtime abstraction; blast radius is the proxy, not all of ratd. |
| **D2** | **Plane manager = ratd subsystem.** Registry, binding resolution, lifecycle live in ratd + postgres. Proxy is a dumb runtime API. | Plane manager as its own service between ratd and proxy. | Two services not three; plane state belongs next to pipeline state. Promote later only if scale demands. |
| **D3** | **Idle timeout + min-instances pin per plane kind.** Default plane: `min=1, idle=10m`. Long tail: `min=0, idle=5m`. Reference counts pin in-flight pipelines. | Pure idle-timeout; pure LRU; no-eviction. | Predictable two-knob model. Default stays hot through weekend gaps; long tail evicts cleanly. |
| **D4** | **Async cold-start + scheduler pre-warm.** Cold request returns `202 { state: warming, eta_seconds, plane_id }`; portal polls `/planes/{id}/status`. Scheduler warms planes T-2min before scheduled runs. | Synchronous wait; reject + retry hint; pre-flight-only (no on-demand warm). | Scheduled work pays no cold-start tax; ad-hoc work pays it visibly with a status badge. No client-side retry contract. |
| **D5** | **`format/v1.Resolve` returns `{files: [...], schema, predicates, projection}`.** Engine reads Parquet from S3 via its native path. Reserved escape hatch (separate `ReadArrow` RPC) for formats that can't fit the file-list model. | Arrow IPC stream from format (doubles data hop); pure hybrid up-front. | For analytical scans, format's value is the metadata work; bytes flow direct. Escape hatch keeps the door open without paying the cost by default. |
| **D6** | **Tenant is a plugin concern.** Core schema has no `org_id`. Plane API exposes a decision-hook (`tenant/v1`-style) called at list/bind boundaries; with no tenant plugin installed it returns "allow." | `org_id` on every table from day 1; full multi-tenant code now. | Matches the existing plugin pattern (`permission/v1`, `sharing/v1`, `identity/v1`). Zero tenant code in core; tenant plugin lands later without core migration. |
| **D7** | **K8s-shaped contract, docker backend today.** plane-manager → proxy speaks `{image, env, resource_limits, healthcheck, ports, labels}` — the LCD k8s also speaks. | Docker-shaped contract; runtime abstraction interface up-front; no off-ramp planning. | Near-zero cost now; future k8s-backed proxy is a drop-in swap, plane manager unchanged. |

## Alternatives considered

- **Stay with ADR-024 + build-time format plugins.** Cheap, but doesn't
  decouple delivery; adding a format still requires an engine rebuild +
  redeploy. Hits a wall the first time an operator wants a 3rd-party
  format.
- **Pure multiplexing (no container spawn).** Make catalog + storage multi-
  backend the way engine already is multi-format. Cheap; no docker.sock;
  no cold start. **Rejected** as the *only* path because it can't host
  genuinely novel plugin code (anything that needs its own process /
  language runtime / native lib). Worth keeping as a **complement**: lite
  planes (URL-and-protocol variants) stay multiplexed; novel-code planes
  go through the manager.
- **Embed Kubernetes.** Correct end state at scale, but too heavy for the
  current product surface. Keep the contract k8s-shaped (image refs +
  resource asks + healthchecks) so the swap is mechanical when the time
  comes.
- **Function-as-a-Service style ephemeral per-request planes.** Spawn a
  plane per pipeline run, kill after. Pure but pays cold start on every
  run. Rejected for the default; could be opt-in for batch / nightly
  workloads.

## Migration

Phased; each phase reviewable on its own. Phase decisions baked in are
listed under each phase (D-references = the table above).

- **Phase 0 — spike** (1-2 days): define `format/v1` proto with `Write` +
  `Resolve` per D5 (file list + predicates). Extract `rat-format-iceberg`
  as a standalone gRPC service. Wire ONE write path + ONE scan path
  through it in dev mode. Measure latency vs bundled — *the* number we
  need before Phase 1.
- **Phase 1 — formats extracted** (3-5 days): `rat-format-ducklake`
  extracted too; engine container drops `pyiceberg` / `duckdb-ducklake`
  deps; engine speaks `format/v1` for every write + scan. Default compose
  adds a `format` service alongside `engine`.
- **Phase 2 — plane-runtime-proxy + ratd registry** (1 week): standalone
  proxy service implementing the k8s-shaped contract (D7) against
  `docker.sock`. ratd subsystem (D2) gains the `plane_kinds` + `planes` +
  `bindings` tables (no `org_id` — D6). Operators register a new plane
  kind via `POST /api/v1/plane-kinds`. No eviction yet; manual stop only.
- **Phase 3 — lifecycle** (1 week): idle eviction with min-instances pin
  (D3), reference counting, healthcheck-driven readiness gating, orphan
  reconciliation on ratd restart, cold-start async response shape (D4).
  Scheduler pre-warm is in this phase too — it's the difference between a
  smooth product and a flaky one.
- **Phase 4 — composition as data** (3-5 days): deprecate `rat.yaml`
  `data_planes:` with a one-shot migration script. Tenant decision-hook
  proto sketched (D6) but no plugin implementation — that's a separate
  ADR / future work.
- **Phase 5 — portal UI** (1 week): list planes, create plane wizard,
  pipeline-settings plane selector. Surface cold-start status from (3).

Total: ~4-6 weeks of focused work, with 5 natural ship-points.

## Related

- ADR-024 — the decoupling-by-axis foundation this builds on; that ADR's
  "format as capability" decision is the one this ADR revises (format
  becomes an axis).
- ADR-019 (internal listener split) — pattern for ratd hosting a
  privileged subsystem that other services cannot reach.
- ADR-020 (platform token) — analogous trust model: ratd hands a token to a
  spawned service, validates it on callback.
- ADR-009 (container executor) — the closest existing analog of ratd
  spawning a container on demand and managing its lifecycle. Plane manager
  is "ADR-009 generalised to every axis."
- ADR-023 (leader heartbeat) — orphan reconciliation pattern when the
  manager dies.
- `proto/engine/v1`, `proto/catalog/v1`, `proto/storage/v1` — the existing
  contracts; `format/v1` joins them at the same versioning discipline.

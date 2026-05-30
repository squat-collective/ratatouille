# ADR-024: Decoupled data architecture (Storage / Catalog / Engine axes)

## Status: Accepted (2026-05-30)

## Context

Today the runner fuses three concerns that should be independent:

- **Compute** — DuckDB is instantiated in-process (`runner/.../executor.py:291`) and runs every transform.
- **Catalog** — the Nessie Iceberg REST catalog is hardcoded; branch lifecycle lives in `nessie.py`.
- **Storage** — S3 access is hardcoded via PyIceberg FileIO + DuckDB's S3 config.

The *runner* side is already pluggable — `rat.pipeline_types`, `rat.strategies`,
`rat.jinja_helpers`, `rat.hooks`, `rat.sources` (proven by the `prql`,
`soft-delete`, `dbt-compat` plugins). The *warehouse* side is not. That asymmetry
is the smell: a user cannot run RAT on **Unity Catalog + DuckDB**, **Lakekeeper +
ClickHouse**, or with **mixed storage**, without forking core.

A prior attempt modelled the warehouse as a single `Warehouse` plugin. It drew the
boundary too coarsely — fusing storage + catalog + format + compute into one box —
so it could not express mix-and-match compositions. That effort was abandoned; this
ADR replaces it.

The deepest realisation: **compute is itself an axis.** Once compute is pluggable,
the runner stops being "the thing that runs DuckDB" and becomes a pure
**orchestrator** that never touches a table byte.

## Decision

Split the warehouse into **three independent, mix-and-match plugin axes** —
**Storage**, **Catalog**, **Engine (compute)** — with **Format as a capability**
(not an axis). Each axis is a plugin service behind a versioned proto contract.
The runner orchestrates; it does not compute.

### The axes and the two planes

```
   PIPELINE  (source + strategy name + config)
       │
       ▼
   RUNNER  ── pure ORCHESTRATOR (control plane only; no table bytes)
       │   compile · resolve binding · plan-time capability match · descriptors
       ├── catalog/v1 ─► CATALOG axis  (Nessie · Unity · Lakekeeper · Glue)
       ├── engine/v1  ─► ENGINE  axis  (DuckDB · ClickHouse · …)
       └── storage/v1 ─► STORAGE axis  (S3 · GCS · ADLS · MinIO)

   DATA PLANE (bulk table bytes — bypasses the runner entirely):
   ENGINE ─ native catalog protocol (e.g. Iceberg REST) ─► CATALOG
   ENGINE ─ native storage API (e.g. S3 GET/PUT) ───────► STORAGE
```

- **Control plane** (thin, metadata): runner → each axis via a typed `*/v1` contract.
- **Data plane** (fat, bulk bytes): the engine does its **own** I/O against the
  catalog (native protocol) and storage (native API). The runner is not on this path.

This split is the decoupling. It is locked by three sub-decisions:

1. **Binding is fully configurable**, resolved **pipeline → layer → namespace →
   default** (most-specific wins), via `rat.yaml` `data_planes` + `bindings`.
2. **Strategies are universal-name plugins** tagged by `(engine, format)`
   compatibility, dispatched **inside the engine** (`__add__`/`__radd__`-style).
   Only the strategy *name* crosses the wire.
3. **The engine does its own catalog + storage I/O.** The runner sends the job +
   descriptors; it never brokers Arrow data. (Proxying table bytes through the
   runner is explicitly rejected.)

### The three contracts

| Contract | Plane | Caller | Responsibility |
|---|---|---|---|
| `storage/v1` | data (vends) | runner | `Describe` (schemes); `VendDescriptor` → `StorageDescriptor`; `ListObjects`/`MoveObjects` (e.g. landing-zone archival) |
| `catalog/v1` | **control** | runner | `Describe` (capabilities); discovery (`ListNamespaces/ListTables/GetTable`); branch lifecycle (`Create/Merge/DeleteBranch`); `GetHistory`. The engine does NOT commit through this — it commits via the catalog's *native* protocol |
| `engine/v1` | data | runner | `Describe` (languages/formats/capabilities); `Execute` (transform + write, streams logs then result); `Query` (read-only); `Preview` (editor) |

### Descriptors — the composition glue (in `common/v1`)

Plugins never import each other. They exchange **values** and speak standard
protocols:

- `CatalogDescriptor{ protocol, uri, branch, options }`
- `StorageDescriptor{ scheme, s3, options }`
- `TableDescriptor{ ref, format, identifier, catalog, storage }`

`ExecuteRequest` carries `inputs[TableDescriptor]` + `output TableDescriptor`. The
engine reads `catalog`/`storage` straight out of each descriptor and talks their
native protocols. Change any field → a different composition, identical engine code.

### Capabilities are open-set strings, not enums

`languages`, `formats`, `capabilities`, `schemes`, catalog `protocol` are **strings**,
not proto enums — deliberately. The entire point is that a third-party plugin can
declare a new engine, format, catalog, or language **without a core proto change**.
The runner matches by string equality at plan time. (Closed enums like `Layer` and
`RunStatus` remain enums; those sets are genuinely closed.)

### Two refinements the current code forces

1. **Phases ②(execute)+③(write) collapse into one `engine.Execute`.** Today the
   runner builds an Arrow table then writes it. In the decoupled model the engine
   reads inputs, transforms, applies the strategy recipe, *and* commits — so the
   runner never holds table data. The runner's remaining phases map to:
   `①/⑤ branch ops → catalog/v1`, `④ quality → engine.Query`, `① detect/compile →
   stays runner-side`.

2. **Physical ref-resolution moves engine-side.** Today `ref()` resolves at compile
   time by querying Nessie for the `metadata.json` path and emitting
   `iceberg_scan('…')` (`templating.py:156–200`) — both catalog-I/O *and*
   format-specific. In the decoupled model, runner-side `ref()` emits an
   **engine-neutral logical name** (`ns.layer.name`) and registers an input
   descriptor; the **engine** maps logical → native scan. Compilation (Jinja, PRQL
   transpile, plugin helpers) stays runner-side because `rat.jinja_helpers` are
   Python entry points a remote engine can't run; runtime `ref()` (Python pipelines)
   is provided by the engine sandbox, only on language-capable engines.

### Default bundle (one-line deploy preserved)

`rat-engine-duckdb` + `rat-catalog-nessie` (Iceberg REST) + `rat-storage-s3` +
Iceberg format, pre-bound as the `default` data_plane. Ships preinstalled →
"data in 5 minutes" still works out of the box. Every axis is replaceable. This is
the **Postgres model** (opinionated core + well-defined extension points + curated
defaults), not the VSCode model (pure shell).

### ratd is unchanged

The `ExecutorService` contract (ratd → runner) is untouched. ratd keeps owning
scheduling, the DAG, Postgres run state, and plugin lifecycle. This refactor happens
*below* that boundary: the runner's internals are rewired to call out to
engine/catalog/storage instead of embedding them.

## Consequences

**Positive.** Storage, catalog, and engine become independently swappable;
mix-and-match compositions ("Unity + DuckDB", "Lakekeeper + ClickHouse") become
config, not forks. The runner shrinks to an orchestrator with a clear contract.
Capability negotiation gives fail-fast config errors instead of mid-run crashes.
The platform's identity ("anyone can add their own connector") is realised on every
data-plane axis, matching the already-pluggable runner side.

**Negative — accepted.** (1) An extra network hop: the engine is now a service, not
in-process. Mitigated by the engine doing its own bulk I/O (data never round-trips
through the runner). (2) A larger surface to test and document — three new contracts
+ reference plugins. (3) Out-of-the-box "just works" now depends on a curated default
bundle; a user who disables it without installing replacements gets an inert
platform (same trade as any plugin host). (4) SQL is not portable across engines;
`dialect` is a tag with plan-time fail-fast matching now, transpilation deferred.

**Neutral.** Wire-protocol package names follow house convention
(`ratatouille.storage.v1`, etc.) and are frozen once released.

## Migration

Phased, behavior-preserving, tracked as 13 tasks:

- **A — Foundation:** this ADR; the three proto contracts + `common/v1` descriptors; codegen green.
- **B — Reference services:** `rat-storage-s3`, `rat-catalog-nessie`, `rat-engine-duckdb` (the big extraction: DuckDB + `iceberg.py` writes + `strategies.py` + `python_exec.py` + physical ref-resolution).
- **C — Rewire runner → orchestrator:** clients + binding resolution + descriptor assembly; map the phases onto catalog/v1 + engine.Execute/Query; move ref-resolution engine-side; plan-time capability matching; delete embedded DuckDB/Iceberg/Nessie; prove behavior-preserving.
- **D — Strategies:** `(engine,format)` compat tags; drop `_iceberg` suffix; install into the engine image.
- **E — Consumers:** migrate `ratq` reads to `engine.Query`; surface composition to ratd/portal.
- **F — Bundle + proof:** wire the default bundle; validate decoupling with a second composition (swap catalog → Lakekeeper/Unity, then engine → ClickHouse on the same pipeline).

## Related

- `proto/engine/v1/engine.proto`, `proto/catalog/v1/catalog.proto`, `proto/storage/v1/storage.proto`, `proto/common/v1/data_plane.proto` — the contracts this ADR defines.
- ADR-014 (merge strategies) — strategies become `(engine,format)`-tagged here.
- ADR-009 (container executor) / `proto/executor/v1` — the closest existing analog (ratd ↔ external plugin over a typed contract); the pattern this ADR replicates one layer down.
- `docs/v2-strategy.md` — architecture source of truth (to be updated as phases land).

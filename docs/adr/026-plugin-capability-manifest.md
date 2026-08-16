# ADR-026: Plugin capability manifest + dependency negotiation

## Status: Proposed (2026-05-30) — sketch, not yet accepted

## Context

RAT has accumulated **five separate plugin-extension mechanisms**, each invented for its own moment, all conceptually doing the same job ("declare what I provide; look up by name; dispatch"):

1. **Interconnect broker** (`rat-plugin-interconnect`) — JSON capability dict; informal `{name, provider, path}` shape; no schema; no versioning.
2. **Engine format adapters** (`rat_engine_duckdb.format_adapters`) — Python entry-points; load class, call 4 Protocol methods; no version negotiation.
3. **Runner internal plugins** (`rat.pipeline_types`, `rat.strategies`, `rat.sources`, `rat.hooks`, `rat.jinja_helpers`) — five separate entry-point groups, each with its own class shape.
4. **Platform plugin Describe** — Go ConnectRPC; declares routes + UI slots + config_schema; capability set is ad-hoc per plugin.
5. **Decision hooks** (`permission/v1`, `sharing/v1`, `identity/v1`) — typed protos but no declaration that a given plugin implements them; runtime detection only.

This causes real problems:

- **Mismatches surface at runtime, not install time.** A pipeline configured with `strategy=scd2` on a `format=delta` plane errors mid-execution, not at bind time. A plugin needing `secrets.get` discovers the broker is down only when it tries to call it.
- **No structured discovery.** Each mechanism has its own listing/registry; portal can't show a unified "what plugins / what they do / what they need" view.
- **N×M coupling reappears at every layer.** Strategy implementations live inside format adapters → adding a strategy requires touching every format. Adding a format reimplements every strategy. (ADR-024 noted this and punted; the cost is now visible.)
- **No dependency story.** A plugin that needs another plugin can't declare that need. It tries to call → fails → user debugs.
- **Versioning is absent or implicit.** A breaking change to `secrets.get` silently breaks every consumer because there's no version field.
- **Decorators-as-dispatch is starting to creep in** as plugin authors paper over the coupling. That fragments the same pattern across plugins instead of concentrating it in a registry.

Prior art (OSGi, VSCode, K8s CRDs, Cargo features, npm peerDependencies, Chrome Manifest v3) converges on a single pattern: **static manifest + central registry + install-time validation**. The manifest is the source of truth; the registry resolves it; the runtime never sees an unsatisfied graph.

## Decision

Introduce **one plugin-capability framework** that subsumes all five mechanisms above. Three components:

### 1. A six-axis taxonomy (`kind:` in the manifest)

Each plugin declares which axis it belongs to. The set is open (community can add kinds), but RAT ships with six load-bearing ones plus the existing platform-plugin kind:

| Kind | What it owns | What it MUST NOT know |
|---|---|---|
| `engine` | SQL → Arrow execution | Format internals; storage credentials |
| `runtime` | Arrow → Arrow operations (filter, join, compute) | SQL dialect; on-disk layout |
| `format` | Arrow ↔ on-disk layout + format metadata | SQL parsing; user-facing strategy names |
| `strategy` | Composes format + runtime ops to write a snapshot | Specific format internals; specific engine quirks |
| `catalog` | Table identity, branches, snapshot indexing | Byte layout; engine semantics |
| `storage` | Credentials, endpoints, byte-level I/O | Tables; formats |
| `platform-plugin` | UI, REST, broker capabilities (e.g. secrets, diff) | Anything in the data path |

The key new split is **engine** vs **runtime**. A SQL execution engine (DuckDB, ClickHouse) is not the same axis as an in-memory Arrow operations runtime (PyArrow, Polars, Datafusion). Strategies operate at the **runtime** layer most of the time; the engine is invoked only when there's a SQL transform to execute. This is what makes strategies finally format-agnostic: they use `runtime.merge(arrow_a, arrow_b)`, not `engine.exec(SQL_with_merge_dialect)`.

### 2. Manifest schema (`rat/1`)

Every plugin ships a `plugin.yaml` (or `[tool.rat]` section in `pyproject.toml` for Python plugins / `[plugin]` in `go.mod`-adjacent file for Go) using a single schema:

```yaml
plugin:
  id:           rat-strategy-scd2
  version:      0.3.0
  api_version:  rat/1                       # which manifest schema this conforms to
  kind:         strategy                    # one of the open-set kinds
  description:  "Slowly-changing dimension type 2 strategy."

# What this plugin provides to the world.
# Each provides entry registers under (kind, name, version) in the registry.
provides:
  - kind:    strategy
    name:    scd2
    version: v1
    schema:                                 # typed input/output for the impl
      options:
        business_key:    { type: string,  required: true }
        valid_from_col:  { type: string,  default: "valid_from" }
        valid_to_col:    { type: string,  default: "valid_to" }

# What this plugin needs from other plugins.
# The registry refuses to load this plugin if requires aren't satisfied.
requires:
  - kind: format-capability                 # uses TableFormat primitives
    capabilities: [scan, merge, append]
    version: v1
  - kind: runtime                           # uses arrow ops
    version: ">=v1"

# Informational; not blocking. Portal shows these as "known-good combinations."
suggests:
  - kind: format
    names: [iceberg, delta, hudi]

# Slots this plugin fills in the portal (replaces today's ad-hoc UI registration).
contributes:
  - kind:      portal-slot
    slot:      pipeline-strategy-configurator
    component: SCD2Configurator
```

The same schema applies to every plugin kind. A format declares `provides: format-capability` with the set of TableFormat primitives it supports. A catalog declares `provides: catalog-protocol` with its protocol family. A platform plugin declares `provides: broker-capability` (the new home for the interconnect's `secrets.get` etc.).

**Versions are SemVer-ish strings**, not enums. A consumer can specify `">=v1, <v2"`; the registry resolves the latest matching. Breaking changes bump the major; the prior version stays available as long as a provider keeps offering it.

### 3. The registry — one structure, replacing five

ratd hosts a single `PluginRegistry`. Pseudocode for its API:

```go
type Registry interface {
    Install(manifest *Manifest) error           // validates requires; fails if unsatisfied
    Uninstall(pluginID string) error            // fails if other plugins depend on this one
    Provide(kind, name, version, provider) error  // dynamic registration (e.g. broker capabilities at runtime)
    Lookup(kind, name string, versionConstraint string) (Provider, error)
    ListByKind(kind string) []*Manifest
    Satisfies(requires []Requirement) ([]Unsatisfied, []Match)
}
```

This replaces:
- Interconnect broker's capability dict → `kind: broker-capability`
- Engine's format adapter dict → `kind: format`
- Runner's 5 entry-point groups → `kind: strategy | pipeline-type | source | hook | jinja-helper`
- Plugin Describe's `routes` + `ui` → `kind: platform-plugin` + its contributes
- Decision-hook detection → `kind: decision-hook` (`permission/v1` etc. become first-class provides)

**One lookup primitive for the whole platform.** Anything that needs to find a thing of kind X named Y calls `registry.Lookup`. Anything that needs to advertise a capability calls `registry.Provide`. No more per-kind machinery.

### 4. Negotiation — at three moments, with structured errors

The registry validates compatibility at three distinct moments:

- **Install time** — when a plugin manifest is loaded. Registry checks `requires` against current state. If unsatisfied: refuses to load the plugin, logs the missing kind+name+version range, surfaces in `GET /api/v1/plugins`.
- **Bind time** — when an operator binds a pipeline to a plane (or a plane to its axis services). Registry walks the bound entities, gathers their union capabilities, checks the pipeline's strategy requirements. If unsatisfied: returns a structured `BindingError` *before* persisting the binding.
- **Run time** — when the engine dispatches to a strategy or format adapter. Registry returns the right provider; missing provider = typed error with the manifest data to suggest fixes.

Error shape (replaces today's raw `RuntimeError`):

```json
{
  "code": "capability_unsatisfied",
  "message": "Pipeline 'shop.silver.history' uses strategy 'scd2' which requires format capabilities [merge, scan, append]. Plane 'delta-prod' (format: delta) provides [scan, append, overwrite] — missing: [merge].",
  "missing": [{"kind": "format-capability", "name": "merge", "version": "v1"}],
  "suggested_fixes": [
    {"action": "rebind_plane", "candidates": ["iceberg-prod", "hudi-prod"]},
    {"action": "switch_strategy", "candidates": ["delete_insert", "full_refresh"]}
  ]
}
```

Portal renders the fixes as clickable suggestions. Support burden for "why doesn't this work?" goes from "read 30s of logs" to "click the fix."

### 5. Compat shim — existing plugins keep working

Today's 5 mechanisms keep working unchanged for migration:

- Existing `pyproject.toml` entry-points are auto-translated to manifests at registry-load time. `[project.entry-points."rat.strategies"]` becomes `kind: strategy, provides: [{kind: strategy, name: <ep-name>, version: v1-legacy}]`.
- Existing interconnect broker registrations become `provides: broker-capability` entries.
- Existing Describe `routes`/`ui`/`config_schema` becomes a synthetic manifest.

The compat shim has no `requires:` block (legacy plugins didn't declare needs). So they always satisfy. They show up in the unified registry as `api_version: rat/0-legacy`. Portal flags them with a "consider upgrading" affordance.

The shim stays as long as needed. No hard cutover. Removing it is a future ADR when adoption is broad.

## Consequences

**Positive.**

- Five ad-hoc systems become one. The mental model is uniform; documentation is one chapter; support burden drops.
- Errors move from runtime to install/bind time. The pipeline that can't run isn't created in the first place.
- Strategies become format-agnostic. A community-contributed `rat-strategy-soft-delete` works on every format declaring the right capabilities. N×M collapses to N+M.
- Engine vs runtime split is finally explicit. Strategies that don't need SQL bypass the engine entirely; faster, simpler.
- The registry becomes the source of truth for "what does this RAT install actually do?" A single endpoint (`GET /api/v1/plugins/registry`) enumerates everything.
- Capability versioning is honest. Breaking changes are flagged; consumers can pin.
- The decorator-for-multi-impl temptation goes away: dispatch belongs in the registry, not in plugin code.

**Negative — accepted.**

1. **Migration cost.** Every existing plugin should eventually ship a manifest. The compat shim hides this, but the goal is uniform manifests across the ecosystem.
2. **Manifest schema rigidity.** Once `rat/1` is published, breaking changes cost real coordination. The schema design has to be conservative; extensible-but-stable is hard.
3. **The TableFormat primitive set must be right.** If `capabilities` doesn't cover real strategy needs, every format gets retrofitted. Phase 0 needs bake-in time.
4. **More upfront thinking for plugin authors.** "What kind am I? What do I provide? What do I require?" is heavier than "drop a class with a name." The payoff is fewer downstream surprises, but the immediate UX is steeper.
5. **Six axes is a taxonomy that can shift over time.** Some plugins will land between axes (is compaction a strategy or a maintenance plugin?). Edge cases need judgement calls, and judgement calls are political.
6. **The registry becomes a critical path.** If it's wrong, nothing works. Single point of failure for everything plugin-related — better be well-tested.

**Neutral.** Manifest format (`plugin.yaml` vs in-pyproject) is a deferrable choice (see Q1).

## Open questions

Decisions to make before Phase 1:

- **Q1.** Manifest format: standalone `plugin.yaml`, or `[tool.rat]` in pyproject.toml / equivalent in go.mod? (Both have merit: standalone is language-agnostic; in-toolchain is one less file.)
- **Q2.** Schema enforcement: JSON Schema validation centrally, or per-language dataclass/struct generation from the schema? (Centrally is portable; generation gives compile-time checks.)
- **Q3.** Versioning semantics: strict SemVer with `>=`/`<` ranges, or capability flags (`["v1", "v1-experimental"]`)? (SemVer is familiar; flags are simpler.)
- **Q4.** Cross-plugin auth: when plugin A calls plugin B via the registry, do we keep the platform-token model, or introduce a per-capability auth contract? (Platform token works; capability-scoped tokens would be tighter.)
- **Q5.** Capability scope: one global namespace (`secrets.get`), or scoped (`rat-plugin-secrets/secrets.get`)? (Global is friendly; scoped prevents name collisions across community plugins.)
- **Q6.** `suggests:` semantics: purely informational, or surface as a soft "not validated" warning? (Soft warning is more honest; might be noise.)
- **Q7.** Compat shim lifetime: indefinite, or with a deprecation window (e.g. removed in `rat/2`)? (Indefinite is friendly; bounded keeps the codebase clean.)
- **Q8.** Where does the registry live: ratd memory (rebuilt at startup), postgres-persisted (survives restart), or both (memory + write-through to postgres)? (Memory is simplest; persistence enables historical audit.)

## Alternatives considered

- **Status quo — keep the five separate systems.** Cheap, but the N×M coupling problem grows linearly with every new format/strategy plugin author. Already painful with 2 formats; unacceptable at 5.
- **Pure decorator-based multi-impl dispatch.** Plugin authors ship N implementations inside one plugin, decorated by context. Easy to write, but couples every plugin to every other context they support → defeats the decoupling goal. Re-introduces N×M one plugin at a time.
- **Use an existing framework wholesale** (OSGi, Pulumi components, K8s operators). Too heavyweight for RAT's plugin surface; brings massive runtime overhead. Steal the *pattern*, not the implementation.
- **Per-kind registries instead of unified.** Keep the 5 systems but add manifests to each. Modest improvement, but doesn't collapse the mental model; portal still has to special-case each kind. Worst-of-both.
- **Static linking of capabilities.** Generate code from manifests at build time so dispatch is compile-time. Theoretically faster; in practice destroys runtime extensibility (which is the whole point of plugins).

## Migration

Phased; each phase reviewable on its own. Phase decisions baked in are referenced from the Consequences/Decisions above.

- **Phase 0 — schema design** (1 week): write the `rat/1` manifest schema (JSON Schema). Convert one of each plugin kind (one platform, one format, one strategy, one runner-internal) as a forcing function. Decide Q1, Q2, Q5 from the conversion experience.
- **Phase 1 — registry skeleton + compat shim** (1-2 weeks): build the registry in ratd (in-memory; persistence deferred to Q8). Add the compat shim that auto-translates existing pyproject entry-points + Describes into manifests. Every existing plugin keeps working with no source change. New `GET /api/v1/plugins/registry` endpoint returns the unified view.
- **Phase 2 — install-time validation** (3-5 days): registry rejects new plugins with unmet `requires`. Existing plugins (no `requires` in the shim) keep loading. Portal surfaces "plugin X failed to install: missing Y" in the plugins UI.
- **Phase 3 — strategy extraction + TableFormat capability set** (2-3 weeks per format): extract strategies from `rat-format-iceberg/recipes.py` into independent `rat-strategy-*` plugins using format-capability primitives. Format adapters lose `recipes.py`, gain a `provides: format-capability` block enumerating their primitives. Strategy plugins reusable across formats — the original ADR-026 idea, now properly framed.
- **Phase 4 — bind-time validation** (1 week): registry validates pipeline/plane bindings against strategy requires + plane capabilities. Errors include structured suggestions. Portal renders fixes as clickable.
- **Phase 5 — runtime axis extraction** (2 weeks): introduce `kind: runtime` (PyArrow as the reference impl). Strategies that don't need SQL switch from `engine.exec` to `runtime.op`. Validation: a pipeline that's pure-Arrow no longer requires an engine plane.
- **Phase 6 — portal UI** (1-2 weeks): unified plugin registry view; bind-time error rendering; capability search (e.g. "what plugins provide `secrets.get/v1`?"); manifest editor for operator-installed plugins.

Total: ~3-4 months of staged work, with 6 ship-points.

## Related

- **ADR-024 (decoupled data architecture)** — establishes engine/catalog/storage as plugin axes. This ADR formalizes the underlying contract, makes capability negotiation explicit, and adds `runtime`, `format`, and `strategy` as their own axes (revising ADR-024's "format as a capability" decision).
- **ADR-025 (on-demand decoupled planes)** — adds runtime container management for axis plugins. This ADR provides the manifest the plane manager reads to decide what to spawn; the two land together. ADR-025's plane-runtime-proxy reads `kind: engine | catalog | storage | format | runtime` manifests to know what images to start.
- **ADR-017 (Python pipeline trust model)** — the trust boundary for code in the runner. Manifests don't change that boundary, but capability `requires` can include trust assertions (e.g. `requires: [{kind: runner-trust, level: second-party}]`).
- **ADR-009 (container executor)** — the first instance of "ratd dispatches to a typed plugin contract." This ADR generalizes the pattern.
- **`proto/plugin/v1`** — the platform plugin's `Describe` proto. To be extended (or replaced) by the manifest in Phase 1.
- **`rat-plugin-interconnect`** — current broker. Phase 1 makes it a thin lookup over the new registry.
- **Prior art:** OSGi `MANIFEST.MF`, VSCode `package.json` extension manifest, K8s `CustomResourceDefinition`, Cargo `[features]`, npm `peerDependencies`, Chrome Manifest v3 — all worked out the same `provides`/`requires`/`registry` pattern this ADR adopts.

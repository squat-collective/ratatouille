# ADR-017: Generic Permission Engine — Path-Based Access Control (v2.12)

## Status: Accepted (updated — replaces original type-based design)

## Context

The v2.7 ACL plugin (ADR-010) shipped with a basic permission model:
- Hardcoded 3-level hierarchy (`admin > write > read`)
- User-only grants (no groups, no roles)
- No resource hierarchy (namespace-level grants don't cascade to pipelines)
- No action implication graphs (`execute` was lumped into `write`)

As the platform grew, we hit limitations:
- **No group-based access**: every grant is per-user, doesn't scale beyond ~10 users
- **No inheritance**: granting `admin` on a namespace requires separate grants for every pipeline within it
- **No extensibility**: adding a new resource type (e.g., `landing_zone`) or action (e.g., `execute`) requires code changes to the permission check logic
- **No external group integration**: Keycloak groups from JWT tokens are ignored by the ACL system

We need a generic, extensible permission engine where resource hierarchy is implicit
in path structure, verbs are globally extensible, and wildcards provide cascading access.

## Decision

### Path-Based Resource Model

Replace the flat `(grantee, resource, permission)` model with path-based grant tuples:

```
(principal_type, principal_id, resource, verb)
```

Resources are identified by slash-delimited paths that encode hierarchy implicitly:

```
gold                                → namespace
gold/pipeline/bronze/orders         → pipeline
gold/table/silver/clean_orders      → table
gold/landing_zone/raw               → landing zone
```

No explicit `RegisterResourceType` or `SetResourceParent` calls needed — hierarchy is
encoded in the path structure itself. New resource types are just new path prefixes.

### New proto: `permission/v1/permission.proto`

A unified `PermissionService` with 15 RPCs, organized into:

| Category | RPCs | Purpose |
|----------|------|---------|
| **Verbs** | `RegisterVerb`, `ListVerbs` | Global verb definition with implication graphs |
| **Resources** | `RemoveResource` | Remove grants for a resource path (with optional cascade) |
| **Grants** | `CreateGrant`, `RevokeGrant`, `ListGrants` | Grant tuple CRUD using resource paths |
| **Access Checks** | `CheckAccess`, `BatchCheckAccess` | Core authorization — evaluates tuples + verb implications + path wildcards |
| **Introspection** | `ListResourceAccess`, `ListPrincipalAccess` | "Who has access?" and "What can I access?" queries |
| **Groups** | `CreateGroup`, `DeleteGroup`, `AddGroupMember`, `RemoveGroupMember`, `ListGroupMembers`, `ListUserGroups` | Engine-managed group CRUD with nested group support |
| **External Sync** | `SyncExternalGroups` | Map external provider groups (Keycloak, etc.) to engine groups |

### Verb Implication Graph (Global)

Verbs form a global directed implication graph (not per resource type):

```
admin ──► write ──► read
  │                  ▲
  ├──► execute ──────┘
  ├──► publish ──────┘
  └──► delete
```

- `admin` implies `write`, `read`, `execute`, `publish`, `delete`
- `write` implies `read`
- `execute` implies `read` (but NOT `write`)
- `publish` implies `read` (but NOT `write` — devs can edit, only release managers publish)
- `read` and `delete` imply nothing

This is resolved via BFS on the reverse implication graph. The engine accepts arbitrary
verb strings — plugins can register custom verbs (e.g., `deploy`) via `RegisterVerb`.

### Wildcard Path Matching

Grants use explicit wildcards for cascading access:

```
Grant: (alice, read, gold/*)
  ✅ matches gold/pipeline/bronze/orders
  ✅ matches gold/table/silver/clean_orders
  ❌ does NOT match gold (self — explicit wildcard required)

Grant: (bob, admin, *)
  ✅ matches everything (super admin)
```

`BuildCandidatePaths("gold/pipeline/bronze/orders")` generates:
```
["gold/pipeline/bronze/orders", "gold/pipeline/bronze/*", "gold/pipeline/*", "gold/*", "*"]
```

### Three Principal Types

```protobuf
enum PrincipalType {
  PRINCIPAL_TYPE_USER  = 1;   // direct user grants
  PRINCIPAL_TYPE_GROUP = 2;   // engine-managed groups (with nesting)
  PRINCIPAL_TYPE_ROLE  = 3;   // JWT/external groups (always fresh from token)
}
```

- **USER**: direct grants to a specific user ID
- **GROUP**: engine-managed groups with nested membership (depth limit: 5)
- **ROLE**: JWT claim groups from external providers (Keycloak, Cognito)

### CheckAccess Algorithm

```
1. Expand principals:
   user_id + resolveUserGroups(user_id) + user_groups_from_jwt

2. Expand verbs:
   BFS on global reverse verb_implications → set of verbs that satisfy the requested one
   e.g., expandVerbs("read") → {"read", "write", "admin", "execute", "publish"}

3. Build candidate paths:
   "gold/pipeline/bronze/orders" → ["gold/pipeline/bronze/orders", "gold/pipeline/bronze/*", "gold/pipeline/*", "gold/*", "*"]

4. Single SQL query:
   SELECT 1 FROM grants
   WHERE resource IN (candidates) AND verb IN (expanded) AND principal matches
   LIMIT 1

5. Match → ALLOW, else → DENY
```

No recursive hierarchy walk. One SQL query.

### Backward Compatibility

The existing `SharingService` and `EnforcementService` (ADR-010) remain operational:
- **SharingService**: dual-writes — creates both legacy `access_grants` and new engine grants (path = `resource_type/resource_id`)
- **EnforcementService**: prefers new `resource`+`verb` fields, falls back to constructing path from `resource_type/resource_id`, then falls back to legacy table
- **Migration**: `MigrateFromLegacy()` copies active legacy grants into the new engine on startup (paths = `resource_type/resource_id`)

The legacy proto services are NOT deprecated — they continue to work for existing
clients. New features use `PermissionService` directly.

### Ownership Stays in ratd

The `PluginAuthorizer` in ratd checks ownership locally in Postgres before calling
the permission engine. Owner always has full access — no network call needed.

## Architecture

```
ratd (community)                      ACL plugin (pro)
┌──────────────────────┐              ┌──────────────────────────┐
│                      │              │                          │
│ PluginAuthorizer     │              │ PermissionService (v2)   │
│  1. Owner? → allow   │              │  RegisterVerb / ListVerbs│
│  2. Engine? ─────────┼─ ConnectRPC ─┤  RemoveResource (path)   │
│                      │              │  CreateGrant / Revoke    │
│ Startup:             │              │  CheckAccess / Batch     │
│  SeedDefaultVerbs()  │              │  ListResourceAccess      │
│                      │              │  Groups / Sync           │
│                      │              │                          │
│ SharingRoutes ───────┼─ ConnectRPC ─┤ SharingService (legacy)  │
│ EnforcementCheck ────┼─ ConnectRPC ─┤ EnforcementService (lgy) │
│                      │              │                          │
└──────────────────────┘              │ SQLite:                  │
                                      │  verb_implications       │
                                      │  grants (path-based)     │
                                      │  groups                  │
                                      │  group_memberships       │
                                      │  access_grants (legacy)  │
                                      └──────────────────────────┘
```

## Consequences

### Positive

- **Simple model**: 4 tables instead of 7 — no `resource_types`, `resource_parents`, `action_implications` tables
- **Implicit hierarchy**: resource paths encode hierarchy, no explicit parent registration
- **Plugin extensibility**: new plugins just use new path prefixes, zero registration needed
- **Explicit wildcards**: `gold/*` covers children, `gold` only covers itself — no surprise cascading
- **Scalable**: group-based grants scale to hundreds of users with a single tuple
- **Dual-source groups**: engine groups for permission-specific groupings + JWT groups always fresh from token
- **Introspection**: "who has access to this pipeline?" and "what can this user access?" are first-class queries
- **Backward compatible**: existing SharingService/EnforcementService clients unaffected
- **Verb graphs**: `publish` implies `read` but not `write` — fine-grained, not just "higher level"
- **Single SQL query**: CheckAccess resolves in one query (no recursive hierarchy walk)

### Negative

- **No auto-cascade**: must explicitly grant `gold/*` — granting on `gold` alone doesn't cascade (trade-off: explicitness > magic)
- **Two systems**: during migration, both legacy and engine grants coexist — dual-write until all clients migrate
- **SQLite limits**: nested group resolution with depth limit 5 uses recursive queries — performance at >1000 groups untested
- **No deny rules**: only allow grants exist — no explicit deny overrides (simplicity trade-off)

### Not Implemented (Future Scope)

- **REST endpoints** (Phase 2): `/api/v1/permissions/grants`, `/api/v1/groups`, etc.
- **Portal UI** (Phase 3): share dialog, "who has access" panel, group management, "what can I access" viewer
- **Read-path enforcement**: filtering query results by accessible namespaces
- **Audit trail**: logging all access checks and grant mutations

## References

- ADR-007: Plugin system foundation
- ADR-010: ACL plugin (v2.7) — predecessor, now wrapped by the engine
- `proto/permission/v1/permission.proto` — proto definition
- `proto/sharing/v1/sharing.proto` — legacy sharing proto (still active)
- `proto/enforcement/v1/enforcement.proto` — legacy enforcement proto (still active)

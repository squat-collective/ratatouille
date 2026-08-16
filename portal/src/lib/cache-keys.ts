/**
 * Centralized SWR cache key factory.
 *
 * Every SWR key in the portal is constructed through this object so that
 * cache invalidation stays consistent and typo-proof. Keys are plain strings
 * (SWR's default key type) built from deterministic path-like patterns.
 *
 * Usage:
 *   useSWR(KEYS.pipeline("ns", "silver", "orders"), fetcher)
 *   mutate(KEYS.pipeline("ns", "silver", "orders"))
 *   mutate(KEYS.match.pipelines)  // broad invalidation via matcher fn
 */

// ---------------------------------------------------------------------------
// Key factory
// ---------------------------------------------------------------------------

export const KEYS = {
  // --- Pipelines ---
  pipelines: (ns?: string, layer?: string) =>
    ns || layer
      ? (`pipelines-${ns ?? "all"}-${layer ?? "all"}` as const)
      : ("pipelines" as const),
  pipeline: (ns: string, layer: string, name: string) =>
    `pipeline-${ns}-${layer}-${name}` as const,
  pipelineRetention: (ns: string, layer: string, name: string) =>
    `pipeline-retention-${ns}-${layer}-${name}` as const,

  // --- Runs ---
  runs: (params?: { namespace?: string; status?: string }) =>
    params ? (`runs-${JSON.stringify(params)}` as const) : ("runs" as const),
  run: (id: string) => `run-${id}` as const,
  runLogs: (id: string) => `run-logs-${id}` as const,

  // --- Tables ---
  tables: (ns?: string, layer?: string) =>
    ns || layer
      ? (`tables-${ns ?? "all"}-${layer ?? "all"}` as const)
      : ("tables" as const),
  table: (ns: string, layer: string, name: string) =>
    `table-${ns}-${layer}-${name}` as const,
  tablePreview: (ns: string, layer: string, name: string) =>
    `table-preview-${ns}-${layer}-${name}` as const,

  // --- Files / Storage ---
  files: (prefix?: string, exclude?: string) => {
    const key = [prefix, exclude].filter(Boolean).join("-") || "all";
    return `files-${key}` as const;
  },
  file: (path: string) => `file-${path}` as const,

  // --- Query ---
  querySchema: () => "query-schema" as const,

  // --- Landing Zones ---
  landingZones: (ns?: string) =>
    ns ? (`landing-zones-${ns}` as const) : ("landing-zones" as const),
  landingZone: (ns: string, name: string) =>
    `landing-zone-${ns}-${name}` as const,
  landingFiles: (ns: string, name: string) =>
    `landing-files-${ns}-${name}` as const,
  landingSamples: (ns: string, name: string) =>
    `landing-samples-${ns}-${name}` as const,
  processedFiles: (ns: string, name: string) =>
    `processed-${ns}-${name}` as const,
  zoneLifecycle: (ns: string, name: string) =>
    `zone-lifecycle-${ns}-${name}` as const,

  // --- Namespaces ---
  namespaces: () => "namespaces" as const,

  // --- Triggers ---
  triggers: (ns: string, layer: string, name: string) =>
    `triggers-${ns}-${layer}-${name}` as const,

  // --- Quality Tests ---
  qualityTests: (ns: string, layer: string, name: string) =>
    `quality-${ns}-${layer}-${name}` as const,

  // --- Lineage ---
  lineage: (namespace?: string) =>
    namespace ? (`lineage-${namespace}` as const) : ("lineage-all" as const),

  // --- Health / Features ---
  features: () => "features" as const,

  // --- Retention ---
  retentionConfig: () => "retention-config" as const,
  reaperStatus: () => "reaper-status" as const,

  // --- Permissions ---
  grants: (filters?: { resource?: string; principal_type?: string; principal_id?: string }) =>
    filters
      ? (`perm-grants-${JSON.stringify(filters)}` as const)
      : ("perm-grants" as const),
  groups: () => "perm-groups" as const,
  groupMembers: (groupId: string) => `perm-group-members-${groupId}` as const,
  verbs: () => "perm-verbs" as const,
  resourceAccess: (resource: string) => `perm-resource-access-${resource}` as const,
  principalAccess: (userId: string) => `perm-principal-access-${userId}` as const,

  // --- Identity ---
  identityCapabilities: () => "identity-capabilities" as const,
  identityUsers: (filters?: { search?: string; limit?: number; offset?: number }) =>
    filters
      ? (`identity-users-${JSON.stringify(filters)}` as const)
      : ("identity-users" as const),
  identityUser: (userId: string) => `identity-user-${userId}` as const,
  identityGroups: () => "identity-groups" as const,
  identitySearch: (query: string) => `identity-search-${query}` as const,

  // --- Matcher functions for broad cache invalidation ---
  // Pass these to SWR's mutate() to revalidate all keys matching a prefix.
  match: {
    /** Matches all pipeline-related keys (pipeline detail, pipelines list). */
    pipelines: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("pipeline"),
    /** Matches all run-related keys (runs list, run detail, run logs). */
    runs: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("runs"),
    /** Matches all table-related keys. */
    tables: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("tables"),
    /** Matches all lineage keys. */
    lineage: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("lineage"),
    /** Matches all file tree keys (files-*). */
    files: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("files-"),
    /** Matches all landing zone list keys. */
    landingZones: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("landing-zones"),
    /** Matches landing-zone detail keys for a specific zone. */
    landingZone: (ns: string, name: string) => (key: unknown): boolean =>
      typeof key === "string" && key.startsWith(`landing-zone-${ns}-${name}`),
    /** Matches ALL landing-files keys (any zone). */
    allLandingFiles: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("landing-files"),
    /** Matches landing-files keys for a specific zone. */
    landingFiles: (ns: string, name: string) => (key: unknown): boolean =>
      typeof key === "string" && key.startsWith(`landing-files-${ns}-${name}`),
    /** Matches landing-samples keys for a specific zone. */
    landingSamples: (ns: string, name: string) => (key: unknown): boolean =>
      typeof key === "string" && key.startsWith(`landing-samples-${ns}-${name}`),
    /** Matches all permission-related keys. */
    permissions: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("perm-"),
    /** Matches all grant-related keys. */
    grants: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("perm-grants"),
    /** Matches all group-related keys. */
    groups: (key: unknown): boolean =>
      typeof key === "string" && (key.startsWith("perm-groups") || key.startsWith("perm-group-members")),
    /** Matches all identity-related keys. */
    identity: (key: unknown): boolean =>
      typeof key === "string" && key.startsWith("identity-"),
  },
} as const;

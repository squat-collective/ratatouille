import type { Metadata } from "next";
import { Shield, CheckCircle, XCircle, AlertTriangle, Trash2, KeyRound, User } from "lucide-react";
import Link from "next/link";
import { type FeaturesResponse } from "@/lib/server-api";
import { getServerApi } from "@/lib/server-auth";
import { auth, authEnabled } from "@/lib/auth/server";

export const metadata: Metadata = {
  title: "Settings | RAT",
  description: "Platform settings, plugins, and license management",
};

export default async function SettingsPage() {
  let features: FeaturesResponse | null = null;
  try {
    const api = await getServerApi();
    features = await api.features();
  } catch {
    // API unreachable — show defaults
  }

  const edition = features?.edition ?? "community";
  const license = features?.license;
  const plugins = features?.plugins ?? {};

  // Get auth session when auth is enabled
  const session = authEnabled ? await auth() : null;

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-lg font-bold tracking-wider gradient-text">
        Settings
      </h1>

      {/* Edition info */}
      <div className="brutal-card p-4 space-y-2">
        <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
          Edition
        </h2>
        <p className="text-sm font-medium">
          RAT{" "}
          <span className="text-primary neon-text">{edition}</span>
        </p>
      </div>

      {/* License card */}
      {license ? (
        <div className="brutal-card p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Shield className="h-4 w-4 text-primary" />
            <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
              License
            </h2>
          </div>

          <div className="grid grid-cols-2 gap-y-2 gap-x-4 text-xs">
            <span className="text-muted-foreground">Status</span>
            <span className="flex items-center gap-1.5">
              {license.valid ? (
                <>
                  <CheckCircle className="h-3 w-3 text-primary" />
                  <span className="text-primary">Valid</span>
                </>
              ) : (
                <>
                  <XCircle className="h-3 w-3 text-destructive" />
                  <span className="text-destructive">
                    {license.error || "Invalid"}
                  </span>
                </>
              )}
            </span>

            {license.tier && (
              <>
                <span className="text-muted-foreground">Tier</span>
                <span>{license.tier}</span>
              </>
            )}

            {license.org_id && (
              <>
                <span className="text-muted-foreground">Organization</span>
                <span>{license.org_id}</span>
              </>
            )}

            {license.seat_limit !== undefined && license.seat_limit > 0 && (
              <>
                <span className="text-muted-foreground">Seat Limit</span>
                <span>{license.seat_limit}</span>
              </>
            )}

            {license.expires_at && (
              <>
                <span className="text-muted-foreground">Expires</span>
                <LicenseExpiry expiresAt={license.expires_at} />
              </>
            )}
          </div>
        </div>
      ) : (
        <div className="brutal-card p-4 space-y-2">
          <div className="flex items-center gap-2">
            <Shield className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
              License
            </h2>
          </div>
          <p className="text-xs text-muted-foreground">
            No license key configured. Running community edition.
          </p>
        </div>
      )}

      {/* Data Retention */}
      <Link href="/settings/retention" className="block">
        <div className="brutal-card p-4 space-y-2 hover:border-primary/50 transition-colors cursor-pointer">
          <div className="flex items-center gap-2">
            <Trash2 className="h-4 w-4 text-primary" />
            <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
              Data Retention
            </h2>
          </div>
          <p className="text-xs text-muted-foreground">
            Configure automatic cleanup of old runs, logs, orphan branches, and
            Iceberg snapshots.
          </p>
        </div>
      </Link>

      {/* Permissions — link card (conditional on permission plugin) */}
      {plugins.permission?.enabled && (
        <Link href="/settings/permissions" className="block">
          <div className="brutal-card p-4 space-y-2 hover:border-primary/50 transition-colors cursor-pointer">
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-primary" />
              <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
                Permissions
              </h2>
            </div>
            <p className="text-xs text-muted-foreground">
              Manage grants, groups, and verb definitions.
            </p>
          </div>
        </Link>
      )}

      {/* Plugin status grid */}
      <div className="brutal-card p-4 space-y-3">
        <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
          Plugins
        </h2>
        <div className="grid grid-cols-2 gap-2">
          {Object.entries(plugins).map(([name, plugin]) => (
            <div
              key={name}
              className="flex items-center gap-2 text-xs p-2 bg-muted/30"
            >
              {plugin.enabled ? (
                <CheckCircle className="h-3 w-3 text-primary shrink-0" />
              ) : (
                <XCircle className="h-3 w-3 text-muted-foreground shrink-0" />
              )}
              <span
                className={
                  plugin.enabled ? "text-foreground" : "text-muted-foreground"
                }
              >
                {name}
              </span>
              {plugin.type && (
                <span className="ml-auto text-[10px] text-muted-foreground">
                  {plugin.type}
                </span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Authentication card — only when auth plugin is enabled */}
      {plugins.auth?.enabled && (
        <div className="brutal-card p-4 space-y-3">
          <div className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-primary" />
            <h2 className="text-xs font-bold tracking-wider text-muted-foreground">
              Authentication
            </h2>
          </div>
          <div className="grid grid-cols-2 gap-y-2 gap-x-4 text-xs">
            <span className="text-muted-foreground">Provider</span>
            <span>Keycloak</span>

            {process.env.KEYCLOAK_ISSUER && (
              <>
                <span className="text-muted-foreground">Issuer</span>
                <span className="truncate font-mono text-[10px]">
                  {process.env.KEYCLOAK_ISSUER}
                </span>
              </>
            )}

            <span className="text-muted-foreground">Status</span>
            <span className="flex items-center gap-1.5">
              {session?.user ? (
                <>
                  <User className="h-3 w-3 text-primary" />
                  <span className="text-primary">Authenticated</span>
                </>
              ) : (
                <>
                  <XCircle className="h-3 w-3 text-muted-foreground" />
                  <span className="text-muted-foreground">Not authenticated</span>
                </>
              )}
            </span>

            {session?.user?.email && (
              <>
                <span className="text-muted-foreground">User</span>
                <span>{session.user.email}</span>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function LicenseExpiry({ expiresAt }: { expiresAt: string }) {
  const d = new Date(expiresAt);
  const daysLeft = Math.ceil(
    (d.getTime() - Date.now()) / (1000 * 60 * 60 * 24),
  );
  return (
    <span className="flex items-center gap-1.5">
      {d.toLocaleDateString()}
      {daysLeft <= 30 && daysLeft > 0 && (
        <AlertTriangle className="h-3 w-3 text-yellow-400" />
      )}
    </span>
  );
}

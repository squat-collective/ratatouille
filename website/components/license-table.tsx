"use client";

import { useState } from "react";

interface LicenseEntry {
  name: string;
  version: string;
  license: string;
  url: string;
  component: string;
  scope: "runtime" | "dev";
}

const COMPONENT_ORDER = [
  "platform (ratd)",
  "runner",
  "query",
  "portal",
  "sdk-typescript",
  "website",
];

const LICENSE_COLORS: Record<string, string> = {
  MIT: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  "Apache-2.0": "bg-blue-500/20 text-blue-400 border-blue-500/30",
  ISC: "bg-violet-500/20 text-violet-400 border-violet-500/30",
  "BSD-2-Clause": "bg-amber-500/20 text-amber-400 border-amber-500/30",
  "BSD-3-Clause": "bg-amber-500/20 text-amber-400 border-amber-500/30",
};

function LicenseBadge({ license }: { license: string }) {
  const colorClass =
    LICENSE_COLORS[license] ||
    "bg-gray-500/20 text-gray-400 border-gray-500/30";
  return (
    <span
      className={`inline-block px-2 py-0.5 text-xs font-mono border ${colorClass}`}
    >
      {license}
    </span>
  );
}

function ComponentSection({
  component,
  entries,
  showDev,
}: {
  component: string;
  entries: LicenseEntry[];
  showDev: boolean;
}) {
  const runtime = entries.filter((e) => e.scope === "runtime");
  const dev = entries.filter((e) => e.scope === "dev");
  const visible = showDev ? entries : runtime;

  if (visible.length === 0) {
    return (
      <div className="border border-border/50 p-4 mb-4">
        <h3 className="font-mono font-bold text-lg mb-2">{component}</h3>
        <p className="text-muted-foreground text-sm font-mono">
          No dependencies found.
        </p>
      </div>
    );
  }

  return (
    <div className="border border-border/50 mb-4">
      <div className="px-4 py-3 border-b border-border/50 flex items-center justify-between">
        <h3 className="font-mono font-bold text-lg">{component}</h3>
        <span className="text-xs font-mono text-muted-foreground">
          {runtime.length} runtime
          {showDev && dev.length > 0 && ` + ${dev.length} dev`}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="border-b border-border/50 text-left">
              <th className="px-4 py-2 text-muted-foreground font-medium">
                Package
              </th>
              <th className="px-4 py-2 text-muted-foreground font-medium">
                Version
              </th>
              <th className="px-4 py-2 text-muted-foreground font-medium">
                License
              </th>
              {showDev && (
                <th className="px-4 py-2 text-muted-foreground font-medium">
                  Scope
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {visible.map((entry, i) => (
              <tr
                key={`${entry.name}-${i}`}
                className={`border-b border-border/20 ${
                  i % 2 === 0 ? "bg-background" : "bg-muted/30"
                }`}
              >
                <td className="px-4 py-1.5">
                  {entry.url ? (
                    <a
                      href={entry.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-neon hover:underline"
                    >
                      {entry.name}
                    </a>
                  ) : (
                    <span>{entry.name}</span>
                  )}
                </td>
                <td className="px-4 py-1.5 text-muted-foreground">
                  {entry.version || "—"}
                </td>
                <td className="px-4 py-1.5">
                  <LicenseBadge license={entry.license} />
                </td>
                {showDev && (
                  <td className="px-4 py-1.5 text-muted-foreground text-xs">
                    {entry.scope}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function LicenseTable({ data }: { data: LicenseEntry[] }) {
  const [showDev, setShowDev] = useState(false);

  // Group by component, maintaining order
  const grouped = new Map<string, LicenseEntry[]>();
  for (const comp of COMPONENT_ORDER) {
    grouped.set(comp, []);
  }
  for (const entry of data) {
    const list = grouped.get(entry.component);
    if (list) {
      list.push(entry);
    } else {
      grouped.set(entry.component, [entry]);
    }
  }

  const totalRuntime = data.filter((e) => e.scope === "runtime").length;
  const totalDev = data.filter((e) => e.scope === "dev").length;

  return (
    <div>
      <div className="flex items-center justify-between mb-6 border border-border/50 px-4 py-3">
        <div className="font-mono text-sm text-muted-foreground">
          <span className="text-foreground font-bold">{totalRuntime}</span>{" "}
          runtime deps
          {totalDev > 0 && (
            <>
              {" "}
              ·{" "}
              <span className="text-foreground font-bold">{totalDev}</span> dev
              deps
            </>
          )}
          {" · "}
          <span className="text-foreground font-bold">
            {COMPONENT_ORDER.length}
          </span>{" "}
          components
        </div>
        <label className="flex items-center gap-2 text-sm font-mono cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showDev}
            onChange={(e) => setShowDev(e.target.checked)}
            className="accent-[hsl(var(--neon))]"
          />
          <span className="text-muted-foreground">Show dev deps</span>
        </label>
      </div>

      {Array.from(grouped.entries()).map(([component, entries]) => (
        <ComponentSection
          key={component}
          component={component}
          entries={entries}
          showDev={showDev}
        />
      ))}
    </div>
  );
}

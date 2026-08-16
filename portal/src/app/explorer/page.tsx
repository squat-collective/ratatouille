import type { Metadata } from "next";
import { Badge } from "@/components/ui/badge";
import { Database, Table2, ChevronRight } from "lucide-react";
import Link from "next/link";
import { getServerApi } from "@/lib/server-auth";
import { formatBytes } from "@/lib/utils";

export const metadata: Metadata = {
  title: "Explorer | RAT",
  description: "Browse Iceberg tables across namespaces and layers",
};

const LAYER_STYLES: Record<string, { color: string; dimColor: string }> = {
  bronze: { color: "var(--layer-bronze)", dimColor: "var(--layer-bronze-dim)" },
  silver: { color: "var(--layer-silver)", dimColor: "var(--layer-silver-dim)" },
  gold: { color: "var(--layer-gold)", dimColor: "var(--layer-gold-dim)" },
};

function getLayerStyle(layer: string) {
  return LAYER_STYLES[layer] ?? LAYER_STYLES.bronze;
}

export default async function ExplorerPage() {
  let tableList: Array<{
    namespace: string;
    layer: string;
    name: string;
    row_count: number;
    size_bytes: number;
  }> = [];
  try {
    const api = await getServerApi();
    const data = await api.tables.list();
    tableList = data?.tables ?? [];
  } catch {
    // API unreachable
  }

  // Group by namespace then layer
  const grouped: Record<
    string,
    Record<
      string,
      Array<{ namespace: string; layer: string; name: string; row_count: number; size_bytes: number }>
    >
  > = {};
  for (const t of tableList) {
    grouped[t.namespace] ??= {};
    grouped[t.namespace][t.layer] ??= [];
    grouped[t.namespace][t.layer].push(t);
  }

  const totalCount = tableList.length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-bold tracking-wider">
          <span className="text-primary">{"//"}</span> Explorer
        </h1>
        <p className="text-xs text-muted-foreground tracking-wider">
          {totalCount} table{totalCount !== 1 ? "s" : ""} across all namespaces
        </p>
      </div>

      {Object.entries(grouped).map(([ns, layers]) => (
        <div key={ns} className="space-y-3">
          <h2 className="text-sm font-bold tracking-wider flex items-center gap-2">
            <Database className="h-3.5 w-3.5 text-primary" />
            <span className="text-primary">{ns}</span>
          </h2>

          <div className="border border-border/50">
            {Object.entries(layers).map(([layer, layerTables]) => {
              const ls = getLayerStyle(layer);
              return layerTables.map((t) => (
                <Link
                  key={`${t.namespace}.${t.layer}.${t.name}`}
                  href={`/explorer/${t.namespace}/${t.layer}/${t.name}`}
                >
                  <div
                    className="explorer-row flex items-center gap-3 px-3 py-2.5 border-b border-border/30 last:border-b-0 cursor-pointer group transition-all duration-100"
                    style={
                      {
                        borderLeftWidth: "3px",
                        borderLeftStyle: "solid",
                        borderLeftColor: ls.color,
                        "--row-hover-bg": ls.dimColor,
                      } as React.CSSProperties
                    }
                  >
                    <span
                      className="text-[10px] font-bold tracking-widest w-14 shrink-0"
                      style={{ color: ls.color }}
                    >
                      {layer}
                    </span>

                    <Table2
                      className="h-3.5 w-3.5 shrink-0 opacity-40 group-hover:opacity-100 transition-opacity"
                      style={{ color: ls.color }}
                    />
                    <span className="text-xs font-bold tracking-wider group-hover:text-foreground transition-colors flex-1">
                      {t.name}
                    </span>

                    <div className="flex items-center gap-2 shrink-0">
                      {t.row_count > 0 && (
                        <span className="text-[10px] text-muted-foreground font-mono tabular-nums">
                          {t.row_count.toLocaleString()} rows
                        </span>
                      )}
                      {t.size_bytes > 0 && (
                        <Badge
                          variant="outline"
                          className="text-[10px]"
                          style={{
                            borderColor: ls.color + "55",
                            color: ls.color,
                          }}
                        >
                          {formatBytes(t.size_bytes)}
                        </Badge>
                      )}
                      <ChevronRight className="h-3 w-3 text-muted-foreground/30 group-hover:text-muted-foreground transition-colors" />
                    </div>
                  </div>
                </Link>
              ));
            })}
          </div>
        </div>
      ))}

      {!tableList.length && (
        <p className="text-xs text-muted-foreground py-8 text-center font-mono">
          {"// no tables found"}
        </p>
      )}
    </div>
  );
}

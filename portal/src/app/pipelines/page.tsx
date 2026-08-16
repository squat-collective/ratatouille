import type { Metadata } from "next";
import { type PipelineListResponse } from "@/lib/server-api";
import { getServerApi } from "@/lib/server-auth";
import { PipelinesClient } from "./pipelines-client";

export const metadata: Metadata = {
  title: "Pipelines | RAT",
  description: "Manage and monitor your data pipelines",
};

export default async function PipelinesPage() {
  let data: PipelineListResponse = { pipelines: [], total: 0 };
  try {
    const api = await getServerApi();
    data = await api.pipelines.list();
  } catch {
    // API unreachable
  }

  return <PipelinesClient pipelines={data.pipelines ?? []} total={data.total} />;
}

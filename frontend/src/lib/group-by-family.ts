import { EngineFamily, engineFamily } from "@/lib/engine";

export interface HasEngine {
  cluster_id: string;
  engine?: string;
  resource_name?: string;
}

// Stable family order for rendering.
export const FAMILY_ORDER: EngineFamily[] = [
  "relational",
  "documentdb",
  "dynamodb",
];

export function groupByEngineFamily<T extends HasEngine>(
  items: T[],
): Record<EngineFamily, T[]> {
  const groups: Record<EngineFamily, T[]> = {
    relational: [],
    documentdb: [],
    dynamodb: [],
  };
  for (const it of items) groups[engineFamily(it.engine)].push(it);
  return groups;
}

// Human display name: resource_name when set (DynamoDB slug id is opaque), else cluster_id.
export function displayName(it: HasEngine): string {
  return it.resource_name || it.cluster_id;
}

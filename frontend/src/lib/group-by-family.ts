import {
  EngineFamily,
  engineFamily,
  EngineGroup,
  engineGroup,
} from "@/lib/engine";

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

// Finer-grained grouping for display — relational splits into PG vs MySQL.
// (groupByEngineFamily still groups both under "relational" for capability gating.)
export function groupByEngineGroup<T extends HasEngine>(
  items: T[],
): Record<EngineGroup, T[]> {
  const groups: Record<EngineGroup, T[]> = {
    "aurora-postgresql": [],
    "aurora-mysql": [],
    documentdb: [],
    dynamodb: [],
  };
  for (const it of items) groups[engineGroup(it.engine)].push(it);
  return groups;
}

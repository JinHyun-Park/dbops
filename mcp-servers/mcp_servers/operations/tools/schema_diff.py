"""schema_diff — structured diff between two schema_snapshots rows.

Older version returned the raw JSON of before/after columns. That left
the diff computation to the caller (often the LLM, which produced
inconsistent results). This version computes the diff server-side and
returns structured added/dropped/renamed/modified buckets so the agent
can answer "did anyone DROP a table this week?" with a single SQL.

Heuristics:
  - Same name, different columns → modified
  - Name in before, missing in after → dropped
  - Name in after, missing in before → added
  - Dropped + added pair with IDENTICAL column signatures → rename
    candidate (we surface both, the agent can confirm with the user)
"""

import json
from typing import Any

from mcp_servers.shared.cache_client import CacheClient


def _parse_tables(blob: Any) -> dict[str, list[str]]:
    """tables_json on a snapshot is `{table_name: [col1, col2, ...]}`
    in the canonical shape. Some older rows stored it as a string;
    handle both."""
    if not blob:
        return {}
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return {}
    if not isinstance(blob, dict):
        return {}
    # Normalize column lists to sorted tuples so unordered representations
    # still compare cleanly.
    out: dict[str, list[str]] = {}
    for tname, cols in blob.items():
        if isinstance(cols, list):
            out[str(tname)] = sorted(str(c) for c in cols)
        elif isinstance(cols, dict):
            # Some snapshotters use {col_name: type}. Pull keys.
            out[str(tname)] = sorted(str(k) for k in cols.keys())
    return out


def _compute_diff(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict:
    """Three-way bucket: added, dropped, modified. Rename candidates are
    a sub-list inside dropped — any dropped table whose column signature
    exactly matches a still-existing added one."""
    before_names = set(before)
    after_names = set(after)

    added_names = after_names - before_names
    dropped_names = before_names - after_names
    common = before_names & after_names

    modified = []
    for name in sorted(common):
        if before[name] != after[name]:
            modified.append({
                "table": name,
                "added_columns": sorted(set(after[name]) - set(before[name])),
                "dropped_columns": sorted(set(before[name]) - set(after[name])),
            })

    # Rename candidates: a dropped table and an added table with the
    # same column signature → maybe one was renamed. Surfaced as a
    # `rename_candidates` array so the agent can ask the user before
    # treating it as a DROP+ADD.
    rename_candidates = []
    consumed_drops: set[str] = set()
    consumed_adds: set[str] = set()
    for d in sorted(dropped_names):
        for a in sorted(added_names):
            if a in consumed_adds:
                continue
            if before[d] == after[a]:
                rename_candidates.append({"from": d, "to": a})
                consumed_drops.add(d)
                consumed_adds.add(a)
                break

    return {
        "added": sorted(added_names - consumed_adds),
        "dropped": sorted(dropped_names - consumed_drops),
        "modified": modified,
        "rename_candidates": rename_candidates,
    }


def get_schema_diff_impl(
    cache: CacheClient,
    cluster_id: str,
    snapshot_a: str = None,
    snapshot_b: str = None,
) -> dict:
    """Return structured diff between two snapshots.

    With both snapshots given: explicit diff between A and B.
    Without: diff between the latest snapshot and the one before it.
    """
    if snapshot_a and snapshot_b:
        sql = (
            "SELECT a.schema_name, a.tables_json AS tables_before, "
            "       b.tables_json AS tables_after "
            "FROM schema_snapshots a, schema_snapshots b "
            "WHERE a.cluster_id = :cluster_id AND b.cluster_id = :cluster_id "
            "  AND a.snapshot_time = :snapshot_a::timestamptz "
            "  AND b.snapshot_time = :snapshot_b::timestamptz "
            "  AND a.schema_name = b.schema_name"
        )
        params = {
            "cluster_id": cluster_id,
            "snapshot_a": snapshot_a,
            "snapshot_b": snapshot_b,
        }
    else:
        # Latest vs second-latest, joined by schema_name.
        sql = (
            "WITH ranked AS ( "
            "  SELECT cluster_id, schema_name, snapshot_time, tables_json, "
            "         ROW_NUMBER() OVER (PARTITION BY schema_name ORDER BY snapshot_time DESC) AS rn "
            "  FROM schema_snapshots "
            "  WHERE cluster_id = :cluster_id"
            ") "
            "SELECT a.schema_name, a.tables_json AS tables_before, "
            "       b.tables_json AS tables_after "
            "FROM ranked a JOIN ranked b "
            "  ON a.schema_name = b.schema_name "
            "WHERE a.rn = 2 AND b.rn = 1"
        )
        params = {"cluster_id": cluster_id}

    result = cache.execute(sql, params)
    diffs: list[dict] = []
    totals = {"added": 0, "dropped": 0, "modified": 0, "rename_candidates": 0}
    for row in result.rows:
        before = _parse_tables(row.get("tables_before"))
        after = _parse_tables(row.get("tables_after"))
        diff = _compute_diff(before, after)
        for k in totals:
            totals[k] += len(diff[k])
        diffs.append({
            "schema_name": row.get("schema_name"),
            **diff,
        })

    return {
        "cluster_id": cluster_id,
        "schemas_compared": len(diffs),
        "totals": totals,
        "diffs": diffs,
    }

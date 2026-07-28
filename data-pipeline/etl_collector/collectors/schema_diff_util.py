"""Canonical schema-snapshot parse + diff, shared by the READER and the PRODUCER.

get_schema_diff computes its diff live from two tables_json blobs.
get_schema_history and diagnose_root_cause instead replay the
diff_from_previous_json the COLLECTOR stored. If those two computations ever
disagree, the same DDL event gets described two different ways depending on
which tool the agent happened to call. So the computation lives in exactly one
file, and that file is copied verbatim into the collector assets (which cannot
import mcp_servers) with a byte-identity + result parity test, the same
convention engine_family.py and metric_filters.py use.

COPIES (edit all three together, tests/unit/data_pipeline/test_schema_snapshot_parity.py
asserts byte-identity AND identical diff results):
  mcp-servers/mcp_servers/operations/schema_diff_util.py   <- canonical
  data-pipeline/etl_collector/collectors/schema_diff_util.py
  data-pipeline/rds_direct_collector/schema_diff_util.py

Heuristics:
  - Same name, different column-name set -> modified
  - Name in before, missing in after     -> dropped
  - Name in after, missing in before     -> added
  - Dropped + added pair with IDENTICAL column signatures -> rename candidate
    (surfaced separately so the agent can confirm instead of claiming a DROP)

Only column NAMES are compared. Types are accepted on input and ignored.
"""

import json
from typing import Any


def parse_tables(blob: Any) -> dict[str, list[str]]:
    """tables_json on a snapshot is `{table_name: [col1, col2, ...]}` in the
    canonical shape. A jsonb column comes back from the RDS Data API as a
    string, so handle both. `{col_name: type}` per table is also accepted;
    only the keys are kept."""
    if not blob:
        return {}
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return {}
    if not isinstance(blob, dict):
        return {}
    # Normalize column lists to sorted lists so unordered representations
    # (JSON_ARRAYAGG has no ORDER BY in MySQL) still compare cleanly.
    out: dict[str, list[str]] = {}
    for tname, cols in blob.items():
        if isinstance(cols, list):
            out[str(tname)] = sorted(str(c) for c in cols)
        elif isinstance(cols, dict):
            out[str(tname)] = sorted(str(k) for k in cols.keys())
    return out


def compute_diff(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict:
    """Four-bucket diff: added, dropped, modified, rename_candidates.

    CAUTION: `dropped` is inferred from ABSENCE. Any caller that feeds a
    TRUNCATED table list here turns tables that merely fell out of the list into
    a DROP claim to the DBA (this is live today in the dashboard's table_stats
    LIMIT 100 panel). The snapshot collector avoids it by aggregating the whole
    schema server-side into one all-or-nothing blob: it either gets the complete
    map or it gets an error and writes nothing.
    """
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


def diff_is_empty(diff: dict) -> bool:
    """True when a computed diff describes no change at all. The collector uses
    this to decide whether a snapshot is worth storing, so an unchanged schema
    never produces a row the readers would then have to filter out."""
    return not any(diff.get(k) for k in ("added", "dropped", "modified", "rename_candidates"))

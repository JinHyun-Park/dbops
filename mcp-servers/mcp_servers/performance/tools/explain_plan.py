"""explain_plan — run EXPLAIN (FORMAT JSON) on a target cluster and turn the raw
plan tree into a structured analysis the agent can reason over.

The frontend already has POST /api/explain, but that returns the raw plan JSON
for client-side rendering only — the chat agent has no way to *interpret* a plan.
This tool walks the PostgreSQL plan tree and surfaces the signals a DBA actually
looks for (seq scans on big tables, bad row estimates, disk spills, nested loops
over large inputs) plus a short list of the most expensive nodes, so the agent can
explain "why is this query slow" without re-deriving plan-reading heuristics from
the full tree every time.

PostgreSQL is the project's primary engine (Aurora PG). MySQL EXPLAIN FORMAT=JSON
has a completely different shape; rather than build a second parser we detect it
and return an explicit "unsupported engine" note instead of crashing.
"""

import hashlib
import json
import re

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.sql_safety import is_read_only_safe

# A Seq Scan is normal on a small table; it only becomes a smell once the planner
# expects to read a meaningful number of rows. 10k is a pragmatic line in the sand.
_SEQ_SCAN_ROW_THRESHOLD = 10_000
# Estimate/actual divergence of 10x is the classic "stale stats / missing index"
# tell — the planner picked a plan for a row count that turned out to be very wrong.
_ESTIMATE_MISS_FACTOR = 10
# A nested loop re-scans its inner side once per outer row, so it only hurts when
# the outer side is large.
_NESTED_LOOP_OUTER_THRESHOLD = 1_000
# Cost is unitless, but a top-level total cost this high is worth a heads-up.
_HIGH_COST_THRESHOLD = 100_000
# How many hot-spot nodes to surface to the agent (avoid dumping the whole tree).
_MAX_EXPENSIVE_NODES = 10


def _strip_explain_prefix(sql: str) -> str:
    """Peel a leading EXPLAIN so we control the format options. Handles both the
    parenthesized form `EXPLAIN (ANALYZE, BUFFERS) <stmt>` and the legacy bare
    form `EXPLAIN ANALYZE VERBOSE <stmt>`. The old `[^()]*` regex greedily ate
    the whole statement when there were no parentheses (e.g. `EXPLAIN SELECT`)."""
    s = sql.lstrip()
    m = re.match(r"(?i)^EXPLAIN\b\s*", s)
    if not m:
        return sql
    s = s[m.end():]
    paren = re.match(r"(?s)^\([^)]*\)\s*", s)  # EXPLAIN ( ... )
    if paren:
        return s[paren.end():]
    # Legacy bare options: EXPLAIN ANALYZE VERBOSE ... (strip leading option words)
    while True:
        opt = re.match(
            r"(?i)^(ANALYZE|VERBOSE|COSTS|BUFFERS|TIMING|SUMMARY|WAL|SETTINGS|"
            r"GENERIC_PLAN|FORMAT\s+\w+|TRUE|FALSE|ON|OFF)\b\s*",
            s,
        )
        if not opt:
            break
        s = s[opt.end():]
    return s


def _is_select(sql: str) -> bool:
    # Mirror of api/explain/handler.py._is_select: only plan/run read-only
    # statements. EXPLAIN ANALYZE actually executes, so an INSERT/UPDATE/DELETE
    # here would mutate the target — block anything that isn't SELECT / WITH...SELECT.
    stripped = sql.strip().rstrip(";").lstrip()
    head = stripped[:6].upper()
    if head == "SELECT":
        return True
    if head[:4] == "WITH":
        return bool(re.search(r"\bSELECT\b", stripped, re.IGNORECASE))
    return False


def _extract_plan_cell(result) -> str | None:
    """Pull the EXPLAIN JSON string out of the QueryResult.

    EXPLAIN ... FORMAT JSON returns a single row with a single column (named
    "QUERY PLAN" by Postgres, but the column name can vary by client), whose
    value is the JSON document. We read defensively: the column name may not be
    exactly "QUERY PLAN", so fall back to the first value of the first row.
    """
    if not result or not getattr(result, "rows", None):
        return None
    row = result.rows[0]
    if not isinstance(row, dict):
        return None
    # Preferred: the canonical Postgres column name.
    for key in ("QUERY PLAN", "query plan", "QUERY_PLAN"):
        if key in row and row[key] is not None:
            return str(row[key])
    # Fall back to the single cell, whatever it's named.
    values = [v for v in row.values() if v is not None]
    if len(values) == 1:
        return str(values[0])
    return None


def _walk(node: dict, nodes: list, depth: int = 0) -> None:
    """Flatten the plan tree into `nodes` (pre-order). Each plan node nests its
    children under "Plans"; collect every node so callers can scan for hot spots
    and per-node smells in one pass."""
    if not isinstance(node, dict):
        return
    nodes.append(node)
    for child in node.get("Plans", []) or []:
        _walk(child, nodes, depth + 1)


def _node_relation(node: dict) -> str | None:
    # Scan/index nodes carry "Relation Name"; joins/aggregates don't.
    return node.get("Relation Name") or node.get("Alias")


def _plan_signature(nodes: list) -> str:
    """STRUCTURAL fingerprint of a plan: ordered (node type, relation, index,
    join type) per node — costs/rows/timings EXCLUDED on purpose. Same signature
    + worse latency = data growth; a different signature = a plan flip."""
    return "\n".join(
        "|".join(str(x or "") for x in (
            n.get("Node Type"),
            n.get("Relation Name") or n.get("Alias"),
            n.get("Index Name"),
            n.get("Join Type"),
        ))
        for n in nodes
    )


def _capture_plan_history(cache: CacheClient, cluster_id: str, inner_sql: str, nodes: list) -> dict:
    """Record this plan's structural signature and compare it to the most recent
    prior EXPLAIN of the same (normalized) query on this cluster, so the agent can
    say whether a slowdown is a PLAN FLIP or just DATA GROWTH. Keyed by a
    normalized-SQL md5. Writes to the cache (query_plan_history, schema_v22)."""
    plan_hash = hashlib.md5(_plan_signature(nodes).encode()).hexdigest()
    query_sig = hashlib.md5(" ".join(inner_sql.lower().split()).encode()).hexdigest()
    summary = " > ".join(str(n.get("Node Type") or "?") for n in nodes[:8])

    prior = cache.execute(
        "SELECT plan_hash, captured_at FROM query_plan_history "
        "WHERE cluster_id = :cid AND query_sig = :qs "
        "ORDER BY captured_at DESC LIMIT 1",
        {"cid": cluster_id, "qs": query_sig},
    )
    prev = prior.rows[0] if prior and prior.rows else None

    cache.execute(
        "INSERT INTO query_plan_history (cluster_id, query_sig, plan_hash, plan_summary) "
        "VALUES (:cid, :qs, :ph, :ps)",
        {"cid": cluster_id, "qs": query_sig, "ph": plan_hash, "ps": summary},
    )

    if not prev:
        return {"first_seen": True, "plan_hash": plan_hash}
    if prev.get("plan_hash") == plan_hash:
        return {
            "changed": False,
            "plan_hash": plan_hash,
            "previous_seen": prev.get("captured_at"),
            "note": "Same plan structure as the last EXPLAIN — a slowdown here points to "
                    "data growth / stale stats, not a plan flip.",
        }
    return {
        "changed": True,
        "plan_hash": plan_hash,
        "previous_plan_hash": prev.get("plan_hash"),
        "previous_seen": prev.get("captured_at"),
        "note": "Plan STRUCTURE changed since the last EXPLAIN — likely a plan flip "
                "(index/join switch), not just data growth.",
    }


def _analyze_node(node: dict, analyze: bool, findings: list) -> None:
    """Append any plan smells for a single node. Kept per-node (not per-tree) so
    each heuristic stays independent and easy to extend."""
    node_type = node.get("Node Type", "")
    plan_rows = node.get("Plan Rows", 0) or 0
    actual_rows = node.get("Actual Rows") if analyze else None
    relation = _node_relation(node)

    # Sequential scan on a large table — the #1 candidate for a missing index.
    if node_type == "Seq Scan" and plan_rows >= _SEQ_SCAN_ROW_THRESHOLD:
        findings.append({
            "severity": "high",
            "issue": "Sequential scan on large table",
            "detail": f"Seq Scan expects {plan_rows} rows on {relation or 'a relation'} "
                      f"(>= {_SEQ_SCAN_ROW_THRESHOLD}); consider an index on the filter/join columns.",
            "node": node_type,
            "relation": relation,
        })

    # Row estimate miss (analyze only) — planner expected N, got something 10x+ off.
    # This is the strongest signal for stale statistics or a missing index.
    if analyze and actual_rows is not None:
        hi = max(actual_rows, plan_rows)
        lo = max(min(actual_rows, plan_rows), 1)
        if hi / lo >= _ESTIMATE_MISS_FACTOR:
            factor = int(hi / lo)
            findings.append({
                "severity": "medium",
                "issue": f"Planner row estimate off by {factor}x",
                "detail": f"{node_type} estimated {plan_rows} rows but actually produced "
                          f"{actual_rows} — likely stale stats (ANALYZE) or a missing index.",
                "node": node_type,
                "relation": relation,
            })

    # Nested loop over a large outer input — inner side gets rescanned per outer row.
    if node_type == "Nested Loop":
        children = node.get("Plans", []) or []
        outer_rows = (children[0].get("Plan Rows", 0) or 0) if children else 0
        if outer_rows >= _NESTED_LOOP_OUTER_THRESHOLD:
            findings.append({
                "severity": "medium",
                "issue": "Nested loop over large input",
                "detail": f"Nested Loop outer side expects {outer_rows} rows "
                          f"(>= {_NESTED_LOOP_OUTER_THRESHOLD}); a hash/merge join may be cheaper.",
                "node": node_type,
                "relation": relation,
            })

    # Sort/Hash that spilled to disk — work_mem too small for this operation.
    sort_method = node.get("Sort Method", "") or ""
    if sort_method and ("external" in sort_method.lower() or "disk" in sort_method.lower()):
        findings.append({
            "severity": "medium",
            "issue": "Operation spilled to disk",
            "detail": f"{node_type} used '{sort_method}' — work_mem was too small and it spilled to disk.",
            "node": node_type,
            "relation": relation,
        })


def explain_plan_impl(cache: CacheClient, cluster_id: str, sql: str, analyze: bool = False) -> dict:
    """Run EXPLAIN on a target Aurora cluster and parse the plan into structured analysis.

    analyze=False (default): EXPLAIN (FORMAT JSON, VERBOSE) — plans only, does NOT
        execute the query. Safe and instant; use this for "why might this be slow".
    analyze=True: EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) — ACTUALLY RUNS
        the SELECT to capture real timings and row counts. Use only when you need
        the planner-vs-reality comparison (row estimate misses, disk spills).

    Only SELECT / WITH...SELECT is accepted — EXPLAIN ANALYZE on a write statement
    would mutate the target, so non-SELECT input is rejected outright.
    """
    # Peel any leading EXPLAIN the caller wrote, THEN validate the inner
    # statement (so `EXPLAIN SELECT ...` is accepted, not rejected wholesale).
    inner = _strip_explain_prefix(sql).rstrip().rstrip(";")
    if not _is_select(inner):
        return {"status": "rejected", "reason": "explain_plan only supports SELECT / WITH...SELECT"}

    # analyze=True EXECUTES the statement. A SELECT prefix isn't enough — a
    # data-modifying CTE (`WITH x AS (DELETE ... RETURNING *) SELECT ...`) or a
    # side-effecting function (pg_terminate_backend, etc.) would actually run.
    # Gate it on the shared read-only-safe check; plan-only (analyze=False) never
    # executes, so it stays unrestricted.
    if analyze and not is_read_only_safe(inner):
        return {
            "status": "rejected",
            "reason": (
                "analyze=true executes the statement; refusing a non-read-only "
                "query (data-modifying CTE, side-effecting function, or stacked "
                "statement). Use analyze=false for a plan-only estimate."
            ),
        }

    if analyze:
        explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) {inner}"
    else:
        explain_sql = f"EXPLAIN (FORMAT JSON, VERBOSE) {inner}"

    # cache.execute_on_target prepends the required `/* source=dbops-agent */`
    # audit tag (AGENTS.md) before running on the target, so we don't add it here.
    result = cache.execute_on_target(cluster_id, explain_sql)

    # Empty QueryResult == cluster not in the registry (or Data API couldn't reach it).
    if not result or not getattr(result, "rows", None):
        return {
            "status": "no_target",
            "reason": "cluster not registered or unreachable — register via /clusters",
            "cluster_id": cluster_id,
        }

    raw = _extract_plan_cell(result)
    if raw is None:
        return {"status": "error", "reason": "EXPLAIN returned no plan cell", "cluster_id": cluster_id}

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Not valid JSON. The likeliest cause on this PG-first platform is a MySQL
        # target (EXPLAIN FORMAT=JSON there has a different shape) or a client that
        # didn't return JSON. Be explicit rather than crash.
        return {
            "status": "error",
            "reason": "unsupported engine for structured analysis (plan was not PG FORMAT JSON)",
            "cluster_id": cluster_id,
            "raw_head": raw[:200],
        }

    # PG returns a list with one element: [{"Plan": {...}, "Planning Time":..., ...}].
    root = parsed[0] if isinstance(parsed, list) and parsed else parsed
    if not isinstance(root, dict) or "Plan" not in root:
        return {
            "status": "error",
            "reason": "unsupported engine for structured analysis (no top-level Plan node)",
            "cluster_id": cluster_id,
            "raw_head": raw[:200],
        }

    plan = root["Plan"]
    nodes: list = []
    _walk(plan, nodes)

    findings: list = []
    for node in nodes:
        _analyze_node(node, analyze, findings)

    # Top-level high cost is informational context, not a per-node smell.
    top_cost = plan.get("Total Cost", 0) or 0
    if top_cost >= _HIGH_COST_THRESHOLD:
        findings.append({
            "severity": "info",
            "issue": "High total plan cost",
            "detail": f"Top-level Total Cost is {top_cost} (>= {_HIGH_COST_THRESHOLD}); "
                      f"this is an expensive plan overall.",
            "node": plan.get("Node Type"),
        })

    summary = {
        "total_cost": top_cost,
        "planning_time_ms": root.get("Planning Time"),
        "estimated_rows": plan.get("Plan Rows"),
        "node_count": len(nodes),
    }
    if analyze:
        summary["execution_time_ms"] = root.get("Execution Time")
        summary["actual_rows"] = plan.get("Actual Rows")

    # Surface only the hottest nodes by cost so the agent sees the bottleneck
    # without wading through the entire tree.
    ranked = sorted(nodes, key=lambda n: n.get("Total Cost", 0) or 0, reverse=True)
    expensive_nodes = []
    for node in ranked[:_MAX_EXPENSIVE_NODES]:
        entry = {
            "node_type": node.get("Node Type"),
            "relation": _node_relation(node),
            "plan_rows": node.get("Plan Rows"),
            "total_cost": node.get("Total Cost"),
        }
        if analyze:
            entry["actual_rows"] = node.get("Actual Rows")
        expensive_nodes.append(entry)

    # Plan-history (C3): structural signature capture + flip-vs-growth comparison
    # against the last EXPLAIN of this query. Best-effort — a cache write failure
    # must never break the analysis result.
    plan_change = None
    try:
        plan_change = _capture_plan_history(cache, cluster_id, inner, nodes)
    except Exception:
        plan_change = None

    return {
        "status": "ok",
        "cluster_id": cluster_id,
        "analyzed": analyze,
        "engine": "postgresql",
        "summary": summary,
        "findings": findings,
        "expensive_nodes": expensive_nodes,
        "plan_change": plan_change,
    }

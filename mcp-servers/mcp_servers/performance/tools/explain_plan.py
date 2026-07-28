"""explain_plan — run EXPLAIN (FORMAT JSON) on a target cluster and turn the raw
plan tree into a structured analysis the agent can reason over.

The frontend already has POST /api/explain, but that returns the raw plan JSON
for client-side rendering only — the chat agent has no way to *interpret* a plan.
This tool walks the PostgreSQL plan tree and surfaces the signals a DBA actually
looks for (seq scans on big tables, bad row estimates, disk spills, nested loops
over large inputs) plus a short list of the most expensive nodes, so the agent can
explain "why is this query slow" without re-deriving plan-reading heuristics from
the full tree every time.

Aurora PostgreSQL and Aurora MySQL are BOTH handled, by two separate parsers.
MySQL's `EXPLAIN FORMAT=JSON` document has nothing structurally in common with
PG's (query_block / table / access_type / rows_examined_per_scan versus Plan /
Node Type / Plan Rows), so the two walkers stay separate on purpose: a unified
plan model would cost more than it saves and would blur which signals are real
for which engine. What IS shared is the OUTPUT contract: same status/summary/
findings/expensive_nodes/plan_change keys, plus the plan-history signature
capture, so the agent reads one shape either way.

Engine resolution: CAPABILITIES is keyed by FAMILY and Aurora PG / Aurora MySQL
are the same family (relational), so the family flag cannot pick the dialect.
The engine string is read from cluster_meta via cache.engine_of(), which is handler-side
data, never a caller-supplied parameter, so the agent cannot ask for a PG-shaped
answer about a MySQL cluster.
"""

import hashlib
import json
import re

from mcp_servers.shared.cache_client import CacheClient, is_mysql_engine
from mcp_servers.shared.sql_safety import is_read_only_safe, strip_sql_literals

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
# MySQL `filtered` is the % of scanned rows the predicate keeps. Below this, the
# access reads several times more rows than it returns.
_MYSQL_FILTERED_PCT_THRESHOLD = 50.0


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


def _normalize_query(sql: str) -> str:
    """Normalize a query so the SAME LOGICAL query matches across runs regardless
    of literal values: strip string literals / quoted idents / comments (shared
    scanner), collapse numeric literals to '?', lowercase + collapse whitespace.
    So `WHERE id = 1` and `WHERE id = 2` (and `name = 'a'` vs `'b'`) hash the same,
    which is what makes plan-flip detection survive parameter changes."""
    s = strip_sql_literals(sql)
    s = re.sub(r"\b\d+(?:\.\d+)?\b", "?", s)  # numeric literals → ?
    return " ".join(s.lower().split())


def _capture_plan_history(cache: CacheClient, cluster_id: str, inner_sql: str, nodes: list) -> dict:
    """Record this plan's structural signature and compare it to the most recent
    prior EXPLAIN of the same (literal-normalized) query on this cluster, so the
    agent can say whether a slowdown is a PLAN FLIP or just DATA GROWTH. Writes to
    the cache (query_plan_history, schema_v22).

    Engine-agnostic: the MySQL branch maps its access nodes onto the same four
    signature slots (see _mysql_history_nodes), so both engines share one hash
    space per (cluster, query)."""
    plan_hash = hashlib.md5(_plan_signature(nodes).encode()).hexdigest()
    query_sig = hashlib.md5(_normalize_query(inner_sql).encode()).hexdigest()
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
            "previous_seen": str(prev.get("captured_at")),
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


# --------------------------------------------------------------------------
# MySQL (Aurora MySQL): EXPLAIN FORMAT=JSON
#
# Document shape verified live against Aurora MySQL 8.0.39 over the Data API:
#
#   query_block:
#     cost_info: {query_cost: "602995.88"}          <- STRINGS, not numbers
#     ordering_operation:                            <- optional wrapper
#       using_filesort: true
#       grouping_operation: {using_temporary_table: true, table: {...}}
#       nested_loop: [{table: {...}}, {table: {...}}]   <- join order, left-deep
#     table:                                         <- single-table form
#       table_name / access_type / rows_examined_per_scan /
#       rows_produced_per_join / filtered ("33.33") / key / possible_keys /
#       cost_info / attached_condition
#
# So: any dict carrying "table_name" is an access node, the optimizer strategy
# flags hang off the CONTAINERS (not the tables), and every cost/percentage is a
# string. Recursion beats enumerating wrapper keys: MySQL has a dozen of them
# (duplicates_removal, materialized_from_subquery, union_result, windowing, ...)
# and a missed wrapper would silently drop that whole subtree from the analysis.
# --------------------------------------------------------------------------

def _num(value):
    """MySQL reports costs and `filtered` as STRINGS. None when not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _walk_mysql(node, tables: list, flags: dict) -> None:
    """Collect every table-access node plus the query-wide optimizer strategy
    flags. A table node's own values are recursed too, because a derived table
    nests another query_block inside `materialized_from_subquery`."""
    if isinstance(node, dict):
        if "table_name" in node:
            tables.append(node)
        for key in ("using_filesort", "using_temporary_table"):
            if node.get(key) is True:
                flags[key] = True
        for value in node.values():
            _walk_mysql(value, tables, flags)
    elif isinstance(node, list):
        for value in node:
            _walk_mysql(value, tables, flags)


def _mysql_history_nodes(tables: list) -> list:
    """Project MySQL access nodes onto the four slots _plan_signature hashes, so
    plan-flip detection is shared instead of duplicated. The mapping is exact in
    meaning, not a fudge: access_type is the node type, table_name is the
    relation, `key` is the chosen index. Costs and row counts stay excluded, so a
    cost-only change still reads as data growth rather than a plan flip."""
    return [
        {
            "Node Type": t.get("access_type"),
            "Relation Name": t.get("table_name"),
            "Index Name": t.get("key"),
        }
        for t in tables
    ]


def _analyze_mysql(tables: list, flags: dict, query_cost, findings: list) -> None:
    """MySQL plan smells. Deliberately NOT a translation of the PG heuristics:
    only signals MySQL's plan-only EXPLAIN actually carries are emitted, and the
    two PG heuristics that need real execution counters are reported as
    unavailable by the caller instead of being faked from estimates."""
    for t in tables:
        relation = t.get("table_name")
        examined = _num(t.get("rows_examined_per_scan"))
        produced = _num(t.get("rows_produced_per_join"))
        access = str(t.get("access_type") or "")

        # access_type ALL = full table scan. MySQL's analogue of a PG Seq Scan,
        # and the same "index probably missing" candidate.
        if access == "ALL" and examined is not None and examined >= _SEQ_SCAN_ROW_THRESHOLD:
            findings.append({
                "severity": "high",
                "issue": "Full table scan on large table",
                "detail": f"access_type=ALL scans {int(examined)} rows on "
                          f"{relation or 'a table'} (>= {_SEQ_SCAN_ROW_THRESHOLD}); "
                          f"consider an index on the WHERE/JOIN columns.",
                "node": access,
                "relation": relation,
            })

        # `filtered` is the % of scanned rows the predicate keeps. Low filtered on
        # a large scan means the access reads far more rows than it returns. This
        # is a SELECTIVITY signal, not a planner-vs-reality estimate miss: both
        # numbers here are estimates, so it says nothing about stats accuracy.
        filtered = _num(t.get("filtered"))
        if (filtered is not None and filtered < _MYSQL_FILTERED_PCT_THRESHOLD
                and examined is not None and examined >= _SEQ_SCAN_ROW_THRESHOLD):
            kept = int(produced) if produced is not None else "?"
            findings.append({
                "severity": "medium",
                "issue": "Low filter selectivity",
                "detail": f"{relation or 'a table'} reads {int(examined)} rows to keep "
                          f"an estimated {kept} (filtered={filtered}%); a more selective "
                          f"index on the filter columns would cut the rows read.",
                "node": access,
                "relation": relation,
            })

    # Strategy flags are query-block level in MySQL, not per-table.
    if flags.get("using_filesort"):
        findings.append({
            "severity": "medium",
            "issue": "Sort not served by an index (filesort)",
            "detail": "using_filesort=true: the optimizer sorts rows itself instead of "
                      "reading them in index order. An index matching the ORDER BY / "
                      "GROUP BY columns can remove the sort.",
            "node": "filesort",
            "relation": None,
        })
    if flags.get("using_temporary_table"):
        findings.append({
            "severity": "medium",
            "issue": "Internal temporary table",
            "detail": "using_temporary_table=true: the query materializes an internal "
                      "temporary table (typical for GROUP BY / DISTINCT / UNION that no "
                      "index can satisfy). Large ones spill to disk.",
            "node": "temporary table",
            "relation": None,
        })

    if query_cost is not None and query_cost >= _HIGH_COST_THRESHOLD:
        findings.append({
            "severity": "info",
            "issue": "High total plan cost",
            "detail": f"query_cost is {query_cost} (>= {_HIGH_COST_THRESHOLD}); "
                      f"this is an expensive plan overall.",
            "node": "query_block",
        })


# Analyses the PG path returns that MySQL's plan-only EXPLAIN cannot produce.
# Stated explicitly rather than defaulted, so the agent never presents a missing
# analysis as a clean result.
_MYSQL_UNAVAILABLE = {
    "planning_time_ms": (
        "MySQL의 EXPLAIN FORMAT=JSON에는 옵티마이저 소요 시간이 포함되지 않습니다. "
        "측정값이 없다는 뜻이며 0이라는 뜻이 아닙니다."
    ),
    "row_estimate_miss": (
        "추정 행수와 실제 행수의 괴리(통계 부정확 신호)는 실행 통계가 있어야 계산됩니다. "
        "MySQL에서 그것을 주는 EXPLAIN ANALYZE는 JSON 출력을 지원하지 않아 이 도구가 "
        "파싱할 수 없습니다. 이 플랜의 모든 행수는 추정값입니다."
    ),
    "disk_spill": (
        "정렬·해시가 실제로 디스크로 스필했는지는 실행 통계에만 나옵니다. "
        "using_filesort / using_temporary_table는 '그 연산을 한다'는 뜻이고 "
        "'디스크를 썼다'는 뜻은 아닙니다."
    ),
}


def _explain_mysql(cache: CacheClient, cluster_id: str, inner: str, analyze: bool) -> dict:
    """Aurora MySQL branch. Same output contract as the PG path."""
    if analyze:
        # MySQL's EXPLAIN ANALYZE both EXECUTES the statement and returns a
        # non-JSON tree (api/explain/handler.py:93-94 documents the same limit),
        # so there is nothing here to parse. Refuse before touching the target.
        return {
            "status": "rejected",
            "cluster_id": cluster_id,
            "engine": "mysql",
            "reason": (
                "MySQL은 EXPLAIN ANALYZE의 결과를 JSON으로 내주지 않아 이 도구가 구조화 "
                "분석을 만들 수 없습니다. analyze=false로 호출하면 실행 없이 플랜을 "
                "분석합니다."
            ),
        }

    # cache.execute_on_target prepends the required `/* source=dbops-agent */`
    # audit tag; a leading comment before EXPLAIN is accepted by MySQL (verified
    # live against the Data API).
    result = cache.execute_on_target(cluster_id, f"EXPLAIN FORMAT=JSON {inner}")
    if not result or not getattr(result, "rows", None):
        return {
            "status": "no_target",
            "reason": "cluster not registered or unreachable, register via /clusters",
            "cluster_id": cluster_id,
        }

    raw = _extract_plan_cell(result)
    if raw is None:
        return {"status": "error", "reason": "EXPLAIN returned no plan cell", "cluster_id": cluster_id}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {
            "status": "error",
            "reason": "MySQL EXPLAIN did not return parseable FORMAT=JSON output",
            "cluster_id": cluster_id,
        }

    block = parsed.get("query_block") if isinstance(parsed, dict) else None
    if not isinstance(block, dict):
        return {
            "status": "error",
            "reason": "MySQL plan has no top-level query_block",
            "cluster_id": cluster_id,
        }

    tables: list = []
    flags: dict = {}
    _walk_mysql(block, tables, flags)

    query_cost = _num((block.get("cost_info") or {}).get("query_cost"))
    findings: list = []
    _analyze_mysql(tables, flags, query_cost, findings)

    # MySQL has no single "plan output rows" field. The LAST access node in the
    # join order carries rows_produced_per_join for the whole join, which is the
    # closest real number; it is pre-LIMIT.
    estimated_rows = _num(tables[-1].get("rows_produced_per_join")) if tables else None

    expensive_nodes = []
    for t in sorted(tables, key=lambda t: _num((t.get("cost_info") or {}).get("prefix_cost")) or 0.0,
                    reverse=True)[:_MAX_EXPENSIVE_NODES]:
        expensive_nodes.append({
            "node_type": t.get("access_type"),
            "relation": t.get("table_name"),
            "plan_rows": _num(t.get("rows_examined_per_scan")),
            # prefix_cost is cumulative through this point of the join order,
            # MySQL's nearest equivalent to a PG node's Total Cost.
            "total_cost": _num((t.get("cost_info") or {}).get("prefix_cost")),
            "key": t.get("key"),
            "possible_keys": t.get("possible_keys"),
        })

    plan_change = None
    try:
        plan_change = _capture_plan_history(
            cache, cluster_id, inner, _mysql_history_nodes(tables)
        )
    except Exception:
        plan_change = None

    return {
        "status": "ok",
        "cluster_id": cluster_id,
        "analyzed": False,
        "engine": "mysql",
        "summary": {
            "total_cost": query_cost,
            "planning_time_ms": None,
            "estimated_rows": estimated_rows,
            "node_count": len(tables),
        },
        "findings": findings,
        "expensive_nodes": expensive_nodes,
        "plan_change": plan_change,
        "unavailable_analysis": _MYSQL_UNAVAILABLE,
    }


def explain_plan_impl(cache: CacheClient, cluster_id: str, sql: str, analyze: bool = False) -> dict:
    """Run EXPLAIN on a target Aurora cluster and parse the plan into structured analysis.

    analyze=False (default): EXPLAIN (FORMAT JSON, VERBOSE) — plans only, does NOT
        execute the query. Safe and instant; use this for "why might this be slow".
    analyze=True: EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) — ACTUALLY RUNS
        the SELECT to capture real timings and row counts. Use only when you need
        the planner-vs-reality comparison (row estimate misses, disk spills).

    Only SELECT / WITH...SELECT is accepted — EXPLAIN ANALYZE on a write statement
    would mutate the target, so non-SELECT input is rejected outright.

    On Aurora MySQL the statement becomes `EXPLAIN FORMAT=JSON` and the MySQL
    walker produces the same output contract; analyze=true is refused there
    because MySQL's EXPLAIN ANALYZE returns no JSON to parse.
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

    # Dialect split. The handler's relational gate has already run, but Aurora PG
    # and Aurora MySQL are the SAME family, so the family flag cannot pick the
    # statement: the engine string decides. Anything not MySQL keeps the PG path
    # (engine_of() returns "" on a lookup failure, so a failure degrades to the
    # behaviour this tool has always had rather than to a MySQL statement).
    if is_mysql_engine(cache.engine_of(cluster_id)):
        return _explain_mysql(cache, cluster_id, inner, analyze)

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
            "reason": "cluster not registered or unreachable, register via /clusters",
            "cluster_id": cluster_id,
        }

    raw = _extract_plan_cell(result)
    if raw is None:
        return {"status": "error", "reason": "EXPLAIN returned no plan cell", "cluster_id": cluster_id}

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Not valid JSON. MySQL no longer lands here (it branched above), so this
        # is a target that answered EXPLAIN with something other than JSON. Be
        # explicit rather than crash.
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

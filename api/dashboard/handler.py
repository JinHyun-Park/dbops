import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import boto3
import tenancy
from engine_family import CAPABILITIES, DOCUMENTDB, RDS_INSTANCE, engine_family
from metric_filters import CLUSTER_LEVEL_ONLY, EXCLUDE_PER_INSTANCE

# api/ cannot import mcp_servers, so schema_diff_util.py is a VERBATIM copy of
# mcp-servers/mcp_servers/operations/schema_diff_util.py (the engine_family /
# metric_filters convention). tests/unit/data_pipeline/test_schema_snapshot_parity.py
# asserts byte-identity across all four copies AND identical diff results.
from schema_diff_util import compute_diff, parse_tables


def _parse_int(value, default, min_v=1, max_v=168):
    try:
        return max(min_v, min(int(value), max_v))
    except (ValueError, TypeError):
        return default


def _parse_float(value, default):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


CLUSTER_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,63}$")


def _rds_data():
    return boto3.client("rds-data")


_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


def _norm_ts(s):
    """Normalize an RDS Data API timestamp string to unambiguous ISO 8601 UTC.

    The Data API returns TIMESTAMP / TIMESTAMPTZ as a space-separated, tz-less
    string in UTC (e.g. "2026-06-09 10:24:28.123"). The browser's `new Date()`
    parses that space form as LOCAL time, so every rendered timestamp came out
    shifted by the viewer's UTC offset (~9h in KST). Emit "...T...Z" so the
    client parses it as UTC and renders it in local time correctly. Strings
    that already carry a zone/offset are left untouched.
    """
    if not s or not isinstance(s, str):
        return s
    iso = s.replace(" ", "T", 1)
    if _TZ_SUFFIX_RE.search(iso):
        return iso
    return iso + "Z"


def _make_query(rds_data, cluster_arn, secret_arn, database):
    def query(sql, params=None):
        sql_params = []
        if params:
            for k, v in params.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    sql_params.append({"name": k, "value": {"longValue": v}})
                elif isinstance(v, float):
                    sql_params.append({"name": k, "value": {"doubleValue": v}})
                else:
                    sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=f"/* source=dbops-dashboard */ {sql}",
            parameters=sql_params,
            includeResultMetadata=True,
        )
        meta = resp.get("columnMetadata", [])
        cols = [c["name"] for c in meta]
        # typeName per column, so we normalize ONLY timestamp columns (leaving
        # text that happens to look date-ish untouched).
        col_is_ts = ["timestamp" in (c.get("typeName") or "").lower() for c in meta]
        rows = []
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                if f.get("isNull"):
                    row[col] = None
                    continue
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        val = f[typ]
                        if typ == "stringValue" and i < len(col_is_ts) and col_is_ts[i]:
                            val = _norm_ts(val)
                        row[col] = val
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows
    return query


_CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE", "")


def _lookup_cluster(cluster_id: str) -> dict:
    """Resolve cluster_arn / secret_arn / db_name from the DynamoDB clusters
    registry — needed when an endpoint queries the live target cluster
    (e.g. listing indexes) instead of the cache DB."""
    if not cluster_id or not _CLUSTERS_TABLE_NAME:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(_CLUSTERS_TABLE_NAME)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[dashboard] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _require_visible(event, cluster_id):
    """Return a 403 response if the caller may not see this cluster, else None.
    Admin / unassigned / member => None (allowed)."""
    if tenancy.cluster_visible(event, _lookup_cluster(cluster_id)):
        return None
    return {
        "statusCode": 403,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": "forbidden", "reason": "이 클러스터에 대한 접근 권한이 없습니다."}),
    }


def _registry_engine(cluster_id: str):
    """Return the `engine` string for a cluster from the registry.

    Returns:
      - str (possibly "")  when the registry row was read successfully
                           (including a legitimate missing Item → "").
      - None               when the registry lookup itself failed (DynamoDB
                           error, network, etc.) — callers must treat None as
                           FAIL CLOSED: do NOT create AWS clients or run live
                           queries against an unknown cluster type.
    """
    if not cluster_id or not _CLUSTERS_TABLE_NAME:
        return ""
    try:
        table = boto3.resource("dynamodb").Table(_CLUSTERS_TABLE_NAME)
        item = table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
        # Row found (or legitimately absent) — return engine string (possibly "").
        return item.get("engine", "")
    except Exception as e:
        print(f"[dashboard] _registry_engine lookup failed for {cluster_id}: {e}")
        # Lookup failure → fail closed (None signals "unknown, do not proceed").
        return None


def _session_for(region: str = "", role_arn: str = "") -> boto3.session.Session:
    """A boto3 Session for a cluster's account+region. With `role_arn`, assume
    the spoke role (hub-spoke chaining) so live RDS/CloudWatch/Logs reads hit
    the cluster's OWN account — not a same-named resource in the hub. With no
    role (single-account deploys) this is a transparent local session, so the
    behavior is unchanged for clusters without a spoke role."""
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName="dbops-dashboard",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _cluster_session(cluster_id: str = "", row: dict | None = None) -> boto3.session.Session:
    """Cross-account-aware session for a cluster's live reads. Pass a registry
    `row` if you already fetched it to avoid a second DynamoDB lookup."""
    row = row if row is not None else _lookup_cluster(cluster_id)
    return _session_for(row.get("region", ""), row.get("spoke_role_arn", ""))


def _schema_graph(cluster_id: str, schema: str) -> dict:
    """Return tables + foreign-key edges for one PG schema.

    Used by the Schema lineage page to render an FK graph. We deliberately
    keep this as a live Data API call (vs caching the snapshot in PG cache)
    because foreign-key topology is slow-moving but high-cardinality — a
    full snapshot per cluster would bloat the cache. Supports both
    engines: PostgreSQL via pg_class + pg_constraint, MySQL via
    information_schema.TABLES + KEY_COLUMN_USAGE."""
    eng = _registry_engine(cluster_id)
    if eng is None:
        # Registry lookup failed — fail closed; do not create rds-data clients.
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "tables": [], "edges": []}
    if engine_family(eng) != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": engine_family(eng),
                "tables": [], "edges": []}
    cluster = _lookup_cluster(cluster_id)
    if not cluster:
        return {"error": f"cluster {cluster_id!r} not registered", "tables": [], "edges": []}
    cluster_arn = cluster.get("cluster_arn")
    secret_arn = cluster.get("secret_arn")
    engine = (cluster.get("engine") or "").lower()
    is_mysql = "mysql" in engine
    db_name = (
        cluster.get("db_name") or ("mysql" if is_mysql else "postgres")
    )
    if not cluster_arn or not secret_arn:
        return {"error": "cluster registry missing cluster_arn/secret_arn", "tables": [], "edges": []}

    if is_mysql:
        # MySQL has no schema namespace inside a database — the "schema"
        # filter the user picks is actually a database name. Default to
        # the cluster's primary db when the caller hasn't specified one.
        schema = (schema or db_name).strip() or db_name
        tables_sql = (
            "SELECT "
            "  TABLE_NAME AS table_name, "
            "  COALESCE(TABLE_ROWS, 0) AS row_count, "
            "  COALESCE(DATA_LENGTH + INDEX_LENGTH, 0) AS size_bytes "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME"
        )
        # GROUP_CONCAT collapses multi-column FKs into a comma list to
        # match the PG `string_agg` output shape so the frontend renderer
        # treats both engines uniformly.
        edges_sql = (
            "SELECT "
            "  kcu.TABLE_NAME AS source_table, "
            "  kcu.REFERENCED_TABLE_NAME AS target_table, "
            "  kcu.REFERENCED_TABLE_SCHEMA AS target_schema, "
            "  kcu.CONSTRAINT_NAME AS constraint_name, "
            "  NULL AS definition, "
            "  GROUP_CONCAT(kcu.COLUMN_NAME ORDER BY kcu.ORDINAL_POSITION) AS source_columns, "
            "  GROUP_CONCAT(kcu.REFERENCED_COLUMN_NAME ORDER BY kcu.ORDINAL_POSITION) AS target_columns "
            "FROM information_schema.KEY_COLUMN_USAGE kcu "
            "WHERE kcu.TABLE_SCHEMA = :schema "
            "  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL "
            "GROUP BY kcu.TABLE_NAME, kcu.REFERENCED_TABLE_NAME, "
            "         kcu.REFERENCED_TABLE_SCHEMA, kcu.CONSTRAINT_NAME "
            "ORDER BY source_table, constraint_name"
        )
    else:
        # Sanitise schema name — pg_namespace.nspname is a regular identifier;
        # we pass it as a string param via Data API to avoid quoting concerns.
        schema = (schema or "public").strip() or "public"

        tables_sql = (
            "SELECT "
            "  c.relname AS table_name, "
            "  COALESCE(s.n_live_tup, 0)::bigint AS row_count, "
            "  pg_total_relation_size(c.oid)::bigint AS size_bytes "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_stat_user_tables s "
            "  ON s.schemaname = n.nspname AND s.relname = c.relname "
            "WHERE c.relkind = 'r' AND n.nspname = :schema "
            "ORDER BY c.relname"
        )

        edges_sql = (
            "SELECT "
            "  cls.relname AS source_table, "
            "  fcls.relname AS target_table, "
            "  fns.nspname AS target_schema, "
            "  con.conname AS constraint_name, "
            "  pg_get_constraintdef(con.oid) AS definition, "
            "  (SELECT string_agg(att.attname, ',' ORDER BY u.ord) "
            "   FROM unnest(con.conkey) WITH ORDINALITY u(att_num, ord) "
            "   JOIN pg_attribute att "
            "     ON att.attrelid = cls.oid AND att.attnum = u.att_num) AS source_columns, "
            "  (SELECT string_agg(att.attname, ',' ORDER BY u.ord) "
            "   FROM unnest(con.confkey) WITH ORDINALITY u(att_num, ord) "
            "   JOIN pg_attribute att "
            "     ON att.attrelid = fcls.oid AND att.attnum = u.att_num) AS target_columns "
            "FROM pg_constraint con "
            "JOIN pg_class cls ON cls.oid = con.conrelid "
            "JOIN pg_namespace ns ON ns.oid = cls.relnamespace "
            "JOIN pg_class fcls ON fcls.oid = con.confrelid "
            "JOIN pg_namespace fns ON fns.oid = fcls.relnamespace "
            "WHERE con.contype = 'f' AND ns.nspname = :schema "
            "ORDER BY cls.relname, con.conname"
        )

    rds_data = boto3.client("rds-data")

    def _run(sql: str) -> list[dict]:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db_name,
            sql=f"/* source=dbops-dashboard-schema-graph */ {sql}",
            parameters=[{"name": "schema", "value": {"stringValue": schema}}],
            includeResultMetadata=True,
        )
        cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
        out: list[dict] = []
        for rec in resp.get("records", []):
            row: dict = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
                if f.get("isNull"):
                    row[col] = None
                    continue
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
            out.append(row)
        return out

    try:
        tables = _run(tables_sql)
        edges = _run(edges_sql)
    except Exception as e:
        # Never surface the raw boto3 fault to the client — it can carry ARNs /
        # account ids. Log it server-side (CloudWatch) for debugging instead.
        print(f"[dashboard] schema-graph query failed for {cluster_id}: {e}")
        return {
            "error": "execution_failed",
            "message": "스키마 그래프 쿼리 실행에 실패했습니다. 잠시 후 다시 시도해주세요.",
            "tables": [],
            "edges": [],
        }

    # Per-table FK degree — useful for the UI to highlight hub tables.
    in_deg: dict[str, int] = {}
    out_deg: dict[str, int] = {}
    for e in edges:
        out_deg[e["source_table"]] = out_deg.get(e["source_table"], 0) + 1
        # Only count incoming edges from within-schema references — cross-
        # schema targets would skew "isolated" detection.
        if e.get("target_schema") == schema:
            in_deg[e["target_table"]] = in_deg.get(e["target_table"], 0) + 1

    for t in tables:
        name = t["table_name"]
        t["fk_out"] = out_deg.get(name, 0)
        t["fk_in"] = in_deg.get(name, 0)
        t["isolated"] = t["fk_in"] == 0 and t["fk_out"] == 0

    return {
        "cluster_id": cluster_id,
        "engine": engine,
        "schema": schema,
        "tables_count": len(tables),
        "edges_count": len(edges),
        "isolated_count": sum(1 for t in tables if t["isolated"]),
        "tables": tables,
        "edges": edges,
    }


def _redundant_indexes(cluster_id: str) -> dict:
    """Find PG indexes that can likely be dropped — prefix-covered, exact
    duplicates, or unused (idx_scan = 0 and not constraint-backing).

    pganalyze ships this as the "Index Advisor / Redundant Indexes" panel.
    Same idea here: catch the easy wasted disk + write amplification before
    a DBA goes through `pg_stat_user_indexes` by hand. PG-only for v1 —
    MySQL exposes a different index shape and the planner heuristics are
    different enough that we don't share logic."""
    eng = _registry_engine(cluster_id)
    if eng is None:
        # Registry lookup failed — fail closed; do not create rds-data clients.
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "candidates": []}
    if engine_family(eng) != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": engine_family(eng),
                "candidates": []}
    cluster = _lookup_cluster(cluster_id)
    if not cluster:
        return {"error": f"cluster {cluster_id!r} not registered", "candidates": []}
    cluster_arn = cluster.get("cluster_arn")
    secret_arn = cluster.get("secret_arn")
    engine = (cluster.get("engine") or "").lower()
    is_mysql = "mysql" in engine
    db_name = cluster.get("db_name") or ("mysql" if is_mysql else "postgres")
    if not cluster_arn or not secret_arn:
        return {"error": "cluster registry missing cluster_arn/secret_arn", "candidates": []}

    # Engine-specific introspection. Both shapes produce the same column
    # set (schema_name, table_name, index_name, columns CSV, bytes, idx_scan,
    # is_unique, is_primary, definition) so the downstream post-processing
    # is engine-agnostic.
    if is_mysql:
        # MySQL idx_scan via performance_schema.table_io_waits_summary_by_
        # index_usage — COUNT_FETCH is the closest analog to pg_stat_user_
        # indexes.idx_scan. Per-index byte size isn't cheaply available
        # from information_schema; report 0 and let the prefix/duplicate
        # heuristics still flag candidates by structure.
        sql = (
            "SELECT "
            "  s.TABLE_SCHEMA AS schema_name, "
            "  s.TABLE_NAME AS table_name, "
            "  s.INDEX_NAME AS index_name, "
            "  CONCAT('INDEX ', s.INDEX_NAME, ' ON ', s.TABLE_SCHEMA, '.', "
            "         s.TABLE_NAME, ' (', "
            "         GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.SEQ_IN_INDEX), "
            "         ')') AS definition, "
            "  0 AS bytes, "
            "  COALESCE(MAX(p.COUNT_FETCH), 0) AS idx_scan, "
            "  CAST(NOT MAX(s.NON_UNIQUE) AS UNSIGNED) AS is_unique, "
            "  CAST(s.INDEX_NAME = 'PRIMARY' AS UNSIGNED) AS is_primary, "
            "  GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.SEQ_IN_INDEX) AS columns "
            "FROM information_schema.STATISTICS s "
            "LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage p "
            "  ON p.OBJECT_SCHEMA = s.TABLE_SCHEMA "
            " AND p.OBJECT_NAME = s.TABLE_NAME "
            " AND p.INDEX_NAME = s.INDEX_NAME "
            "WHERE s.TABLE_SCHEMA NOT IN ('mysql','performance_schema','information_schema','sys') "
            "GROUP BY s.TABLE_SCHEMA, s.TABLE_NAME, s.INDEX_NAME "
            "ORDER BY s.TABLE_SCHEMA, s.TABLE_NAME, s.INDEX_NAME"
        )
    else:
        # PG pg_index — WITH ORDINALITY preserves column order so a (a,b)
        # prefix is distinguishable from (b,a).
        sql = (
            "SELECT "
            "  n.nspname AS schema_name, "
            "  c.relname AS table_name, "
            "  ic.relname AS index_name, "
            "  pg_get_indexdef(i.indexrelid) AS definition, "
            "  pg_relation_size(i.indexrelid)::bigint AS bytes, "
            "  COALESCE(s.idx_scan, 0) AS idx_scan, "
            "  i.indisunique AS is_unique, "
            "  i.indisprimary AS is_primary, "
            "  (SELECT string_agg(COALESCE(a.attname, '(expr)'), ',' ORDER BY arr.ord) "
            "   FROM unnest(i.indkey) WITH ORDINALITY AS arr(col, ord) "
            "   LEFT JOIN pg_attribute a "
            "     ON a.attrelid = i.indrelid AND a.attnum = arr.col) AS columns "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') "
            "  AND i.indisvalid "
            "ORDER BY n.nspname, c.relname, ic.relname"
        )

    rds_data = boto3.client("rds-data")
    try:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db_name,
            sql=f"/* source=dbops-dashboard-redundant */ {sql}",
            includeResultMetadata=True,
        )
    except Exception as e:
        print(f"[dashboard] redundant-indexes query failed for {cluster_id}: {e}")
        return {
            "error": "execution_failed",
            "message": "중복 인덱스 분석 쿼리 실행에 실패했습니다. 잠시 후 다시 시도해주세요.",
            "candidates": [],
        }

    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    indexes: list[dict] = []
    for rec in resp.get("records", []):
        row: dict = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        indexes.append(row)

    # Group by (schema, table) and compute redundancy candidates. We treat:
    #   - "prefix"   — this index's columns are a strict prefix of another's
    #   - "duplicate"— same columns as another index (keep the larger; the
    #                  smaller is usually a leftover migration artifact)
    #   - "unused"   — idx_scan = 0 and not backing a unique/PK constraint
    # An index can only show up once — we prefer prefix > duplicate > unused
    # so the DBA sees the most explainable reason first.
    findings: list[dict] = []
    by_table: dict[tuple[str, str], list[dict]] = {}
    for idx in indexes:
        key = (idx.get("schema_name") or "", idx.get("table_name") or "")
        by_table.setdefault(key, []).append(idx)

    for (schema, tbl), group in by_table.items():
        for a in group:
            if a.get("is_primary"):
                continue  # primary key is sacred even if unused
            a_cols = (a.get("columns") or "").split(",")
            a_name = a.get("index_name") or ""
            reason = None
            covered_by = None
            for b in group:
                if b is a:
                    continue
                b_cols = (b.get("columns") or "").split(",")
                b_name = b.get("index_name") or ""
                if a_cols == b_cols:
                    # Duplicate — keep whichever is larger / has more scans
                    a_size = int(a.get("bytes") or 0)
                    b_size = int(b.get("bytes") or 0)
                    if (a_size, int(a.get("idx_scan") or 0)) < (
                        b_size,
                        int(b.get("idx_scan") or 0),
                    ):
                        reason = "duplicate"
                        covered_by = b_name
                        break
                elif (
                    len(a_cols) < len(b_cols)
                    and a_cols == b_cols[: len(a_cols)]
                    and not a.get("is_unique")
                ):
                    # Strict prefix — b covers every query a covers, plus
                    # more. Unique-index prefixes are NOT redundant (they
                    # enforce a separate uniqueness constraint).
                    reason = "prefix"
                    covered_by = b_name
                    break

            if reason is None and int(a.get("idx_scan") or 0) == 0:
                # Unused — only flag if it's not enforcing a constraint.
                if not a.get("is_unique"):
                    reason = "unused"

            if reason:
                findings.append(
                    {
                        "schema": schema,
                        "table": tbl,
                        "index_name": a_name,
                        "kind": reason,
                        "bytes": int(a.get("bytes") or 0),
                        "idx_scan": int(a.get("idx_scan") or 0),
                        "is_unique": bool(a.get("is_unique")),
                        "columns": a.get("columns") or "",
                        "definition": a.get("definition") or "",
                        "covered_by": covered_by,
                    }
                )

    findings.sort(key=lambda f: f["bytes"], reverse=True)
    total_bytes = sum(f["bytes"] for f in findings)
    return {
        "cluster_id": cluster_id,
        "engine": engine,
        "indexes_scanned": len(indexes),
        "candidates_count": len(findings),
        "total_bytes_reclaimable": total_bytes,
        "candidates": findings,
    }


def _table_indexes(cluster_id: str, schema: str, table_name: str) -> dict:
    """List every index on a given table (definition, size, scan count,
    uniqueness, primary-key flag). Engine-aware: PG queries pg_stat_user_indexes,
    MySQL aggregates from information_schema.statistics + table_io_waits_summary."""
    if not schema or not table_name:
        return {"error": "schema and table required"}
    eng = _registry_engine(cluster_id)
    if eng is None:
        # Registry lookup failed — fail closed; do not create rds-data clients.
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "indexes": []}
    if engine_family(eng) != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": engine_family(eng),
                "indexes": []}
    cluster = _lookup_cluster(cluster_id)
    if not cluster:
        return {"error": f"cluster {cluster_id!r} not registered"}
    cluster_arn = cluster.get("cluster_arn")
    secret_arn = cluster.get("secret_arn")
    db_name = cluster.get("db_name") or "postgres"
    engine = (cluster.get("engine") or "").lower()
    if not cluster_arn or not secret_arn:
        return {"error": "cluster registry missing cluster_arn/secret_arn"}

    if "mysql" in engine:
        # MySQL: information_schema.statistics holds per-column index info;
        # we collapse to one row per index (GROUP_CONCAT columns into the
        # definition column) and join performance_schema.table_io_waits_summary_by_index_usage
        # for usage counts.
        sql = (
            "SELECT "
            "  s.INDEX_NAME AS index_name, "
            "  CONCAT('USING ', MAX(s.INDEX_TYPE), ' (', GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.SEQ_IN_INDEX), ')') AS definition, "
            "  COALESCE(MAX(stat.STAT_VALUE * stat.STAT_VALUE), 0) AS bytes, "  # rough estimate
            "  COALESCE(MAX(ios.COUNT_FETCH), 0) AS idx_scan, "
            "  COALESCE(MAX(ios.COUNT_READ), 0) AS idx_tup_read, "
            "  (MAX(s.NON_UNIQUE) = 0) AS is_unique, "
            "  (MAX(s.INDEX_NAME) = 'PRIMARY') AS is_primary, "
            "  TRUE AS is_valid "
            "FROM information_schema.statistics s "
            "LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage ios "
            "  ON ios.OBJECT_SCHEMA = s.TABLE_SCHEMA AND ios.OBJECT_NAME = s.TABLE_NAME AND ios.INDEX_NAME = s.INDEX_NAME "
            "LEFT JOIN mysql.innodb_index_stats stat "
            "  ON stat.database_name = s.TABLE_SCHEMA AND stat.table_name = s.TABLE_NAME AND stat.index_name = s.INDEX_NAME "
            "  AND stat.stat_name = 'size' "
            "WHERE s.TABLE_SCHEMA = :s AND s.TABLE_NAME = :t "
            "GROUP BY s.INDEX_NAME "
            "ORDER BY is_primary DESC, index_name"
        )
    else:
        sql = (
            "SELECT "
            "  i.indexrelname AS index_name, "
            "  pg_get_indexdef(i.indexrelid) AS definition, "
            "  pg_relation_size(i.indexrelid)::bigint AS bytes, "
            "  i.idx_scan, "
            "  i.idx_tup_read, "
            "  ix.indisunique AS is_unique, "
            "  ix.indisprimary AS is_primary, "
            "  ix.indisvalid AS is_valid "
            "FROM pg_stat_user_indexes i "
            "JOIN pg_index ix ON ix.indexrelid = i.indexrelid "
            "WHERE i.schemaname = :s AND i.relname = :t "
            "ORDER BY pg_relation_size(i.indexrelid) DESC"
        )
    rds_data = boto3.client("rds-data")
    try:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db_name,
            sql=f"/* source=dbops-dashboard-indexes */ {sql}",
            parameters=[
                {"name": "s", "value": {"stringValue": schema}},
                {"name": "t", "value": {"stringValue": table_name}},
            ],
            includeResultMetadata=True,
        )
    except Exception as e:
        print(f"[dashboard] table-indexes query failed for {cluster_id}: {e}")
        return {
            "error": "execution_failed",
            "message": "인덱스 조회 쿼리 실행에 실패했습니다. 잠시 후 다시 시도해주세요.",
        }

    # MySQL Data API leaves `name` blank for computed/aliased columns; the
    # alias ends up in `label`. Prefer whichever is non-empty.
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        rows.append(row)
    return {"schema": schema, "table": table_name, "indexes": rows}


# On-demand LIVE top (P2-⑧). Unlike every other dashboard read, this does NOT
# hit the pre-collected cache and is NOT a background collector — it queries the
# TARGET cluster directly via RDS Data API, and ONLY while a DBA has the live
# view open (the browser polls ~2s and clears the interval on close/unmount).
# So the target sees load only while someone is actively watching. PostgreSQL
# only: pg_stat_activity / pg_blocking_pids / pg_buffercache are PG surfaces.
# MySQL SHOW PROCESSLIST is a different mechanism — out of v1 scope.
_LIVE_SESSIONS_SQL = (
    "SELECT pid, usename, state, "
    "  COALESCE(wait_event_type || ':' || wait_event, 'CPU') AS wait, "
    "  EXTRACT(EPOCH FROM (now() - query_start)) AS age_sec, "
    "  left(query, 120) AS query, backend_type "
    "FROM pg_stat_activity "
    "WHERE state IS NOT NULL AND pid <> pg_backend_pid() "
    "ORDER BY age_sec DESC NULLS LAST LIMIT 100"
)
# array_to_string keeps the pid[] out of Data API's arrayValue path (which the
# generic scalar parser below ignores) — we split the CSV back into ints here.
_LIVE_BLOCKING_SQL = (
    "SELECT pid, array_to_string(pg_blocking_pids(pid), ',') AS blockers "
    "FROM pg_stat_activity "
    "WHERE cardinality(pg_blocking_pids(pid)) > 0"
)
_LIVE_COUNTERS_SQL = (
    "SELECT xact_commit, xact_rollback, tup_returned, tup_fetched, "
    "  tup_inserted, tup_updated, tup_deleted, blks_read, blks_hit "
    "FROM pg_stat_database WHERE datname = current_database()"
)
# HEAVY — pg_buffercache scans the whole shared-buffer pool. Never in the poll;
# only on the manual "버퍼풀" button (?buffers=true).
_LIVE_BUFFERCACHE_SQL = (
    "SELECT count(*) FILTER (WHERE relfilenode IS NOT NULL) AS used, "
    "  count(*) AS total FROM pg_buffercache"
)
_LIVE_BUFFERCACHE_TOP_SQL = (
    "SELECT c.relname AS relation, count(*) AS buffers "
    "FROM pg_buffercache b "
    "JOIN pg_class c ON b.relfilenode = pg_relation_filenode(c.oid) "
    "GROUP BY c.relname ORDER BY buffers DESC LIMIT 10"
)


def _live_activity(cluster_id: str, buffers: bool = False) -> dict:
    """One live snapshot of the target PG cluster's active sessions, blocking
    chains and cumulative DB counters (the client computes per-second rates from
    consecutive snapshots — no server-side state). buffers=True additionally runs
    the heavy pg_buffercache summary. PG-only; graceful when the cluster isn't PG
    or has no Data API. Never leaks str(e)."""
    eng = _registry_engine(cluster_id)
    if eng is None:
        # Registry lookup failed — fail closed; do not create rds-data clients.
        return {"cluster_id": cluster_id, "available": False, "registry_unavailable": True}
    fam = engine_family(eng)
    if fam != "relational" or "mysql" in (eng or "").lower():
        # Non-relational, or MySQL (SHOW PROCESSLIST is a different mechanism,
        # out of v1 scope) → friendly not_applicable, no Data API call.
        return {
            "cluster_id": cluster_id, "available": False, "not_applicable": True,
            "engine_family": fam,
            "reason": "라이브 top은 Aurora PostgreSQL 전용입니다",
        }

    cluster = _lookup_cluster(cluster_id)
    cluster_arn = (cluster or {}).get("cluster_arn")
    secret_arn = (cluster or {}).get("secret_arn")
    db_name = (cluster or {}).get("db_name") or "postgres"
    if not cluster_arn or not secret_arn:
        return {
            "cluster_id": cluster_id, "available": False,
            "reason": "대상 클러스터에 RDS Data API가 없어 라이브 조회가 불가합니다 (활성화 필요)",
        }

    rds_data = boto3.client("rds-data")

    def _run(sql: str) -> list[dict]:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=db_name,
            sql=f"/* source=dbops-live */ {sql}",
            includeResultMetadata=True,
        )
        cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
        out: list[dict] = []
        for rec in resp.get("records", []):
            row: dict = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
                if f.get("isNull"):
                    row[col] = None
                    continue
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
                else:
                    row[col] = None
            out.append(row)
        return out

    try:
        sessions = _run(_LIVE_SESSIONS_SQL)
        blocking_rows = _run(_LIVE_BLOCKING_SQL)
        counter_rows = _run(_LIVE_COUNTERS_SQL)
    except Exception as e:
        # Data API not enabled / cluster paused / connect fault — never surface
        # the raw boto3 fault (it can carry ARNs / account ids). Log server-side.
        print(f"[dashboard] live-activity query failed for {cluster_id}: {type(e).__name__}: {e}")
        return {
            "cluster_id": cluster_id, "available": False,
            "reason": "대상 클러스터에 RDS Data API가 없어 라이브 조회가 불가합니다 (활성화 필요)",
        }

    blocking = []
    for r in blocking_rows:
        pid = r.get("pid")
        raw = r.get("blockers") or ""
        blockers = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
        if pid is not None:
            blocking.append({"pid": pid, "blockers": blockers})

    buffercache = None
    if buffers:
        try:
            summary = _run(_LIVE_BUFFERCACHE_SQL)
            top = _run(_LIVE_BUFFERCACHE_TOP_SQL)
            s0 = summary[0] if summary else {}
            buffercache = {
                "used": int(s0.get("used") or 0),
                "total": int(s0.get("total") or 0),
                "top_relations": top,
            }
        except Exception as e:
            # pg_buffercache extension missing / no privilege — degrade only the
            # buffer section, keep the rest of the snapshot usable.
            print(f"[dashboard] live buffercache failed for {cluster_id}: {type(e).__name__}: {e}")
            buffercache = {
                "available": False,
                "reason": "pg_buffercache 확장을 사용할 수 없습니다",
            }

    return {
        "cluster_id": cluster_id,
        "available": True,
        "captured_at": int(time.time() * 1000),
        "sessions": sessions,
        "blocking": blocking,
        "db_counters": counter_rows[0] if counter_rows else {},
        "buffercache": buffercache,
    }


_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}

_CURRENT_ORIGIN = {"value": ""}


def _set_origin(event):
    headers = (event or {}).get("headers") or {}
    _CURRENT_ORIGIN["value"] = headers.get("origin") or headers.get("Origin") or ""


def _response(status, body, max_age: int = 0):
    """Build the API Gateway response envelope.

    `max_age` adds a Cache-Control header so the browser caches
    identical GETs for that many seconds. Use sparingly — only for
    endpoints whose payload genuinely is stable for that window
    (overview, timeseries, timeline). Default 0 = no cache (safe for
    mutations + per-call-fresh reads). We use `private` so a shared
    proxy can't cache one user's response for another.
    """
    origin = _CURRENT_ORIGIN["value"]
    if _ALLOWED_ORIGINS:
        allow = origin if origin in _ALLOWED_ORIGINS else ""
    else:
        allow = origin or "*"
    cors = {}
    if allow:
        cors = {"Access-Control-Allow-Origin": allow}
        if allow != "*":
            cors["Vary"] = "Origin"
    cache_hdr = {}
    if max_age > 0 and 200 <= status < 300:
        swr = int(max_age) * 4
        cache_hdr["Cache-Control"] = f"private, max-age={int(max_age)}, stale-while-revalidate={swr}"
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            **cors,
            **cache_hdr,
        },
        "body": json.dumps(body, default=str),
    }


# Warm-container TTL cache for the expensive cross-account live-describe reads
# (topology / backups / engine-config). Each of those endpoints fires
# sts:AssumeRole + multiple rds:Describe* + (for topology) N× cloudwatch:
# GetMetricStatistics PER REQUEST against the cluster's own account. The
# region-level RDS-describe and CloudWatch quotas (tens of req/s) throttle long
# before the Data API does, so under many concurrent dashboard pollers these
# are the first thing to fall over. HTTP Cache-Control only dedups within a
# single browser; this dedups ACROSS users sharing a warm Lambda container.
#
# Lives in module memory → scoped to one warm container. Lambda scales out
# horizontally, so the global ceiling is (concurrent containers × 1/ttl)
# describe-bursts — still far below the per-request rate without it, and it
# degrades gracefully (a cold container just does one live call). Kept at the
# routing layer (not inside _topology/_backups/_engine_config) so those stay
# pure + directly unit-testable.
_LIVE_CACHE: dict[str, tuple[float, object, float]] = {}

# Successful describe results are stable for the TTL; failures (error-shaped
# dicts AND raised exceptions) are cached only briefly so a transient fault
# doesn't pin a panel for a full minute, while still throttling a hard-failing
# cluster down from every-poll to once per negative-TTL window.
_LIVE_NEG_TTL = 5.0

# Defensive ceiling on the cache size. The real key space is 3 endpoints ×
# registered clusters (bounded by the DynamoDB registry, ~hundreds), so this is
# never reached in normal operation — it only guards a warm container against an
# unforeseen key explosion. On overflow we drop the whole cache (cheap; it
# refills on the next polls) rather than maintain a per-entry LRU.
_LIVE_CACHE_MAX = 1024


class _CachedError:
    """Wraps an exception raised by a live-describe producer so the FAILURE is
    cached for the negative TTL and re-raised on subsequent hits — otherwise a
    producer that throws (e.g. sts:AssumeRole / rds:Describe* throttle or
    outage) would be retried live on every poll, the exact thundering-herd this
    cache exists to prevent, and precisely when AWS is already rate-limiting."""

    __slots__ = ("exc",)

    def __init__(self, exc):
        self.exc = exc


def _store_live(key: str, val, ttl: float):
    # Stamp the entry with the time AFTER producer() finished (not before it
    # started) so a slow live call doesn't eat into the entry's effective TTL —
    # matters most for the 5s negative TTL, where a multi-second failure could
    # otherwise land already-expired.
    if len(_LIVE_CACHE) >= _LIVE_CACHE_MAX and key not in _LIVE_CACHE:
        _LIVE_CACHE.clear()
    _LIVE_CACHE[key] = (time.monotonic(), val, ttl)


def _cached_live(key: str, ttl: float, producer):
    """Return producer()'s result, served from a warm-container TTL cache.

    `producer` is invoked only on a miss (or after the entry's TTL lapses),
    bounding the live-AWS call rate behind this key. Both failure modes — an
    error-shaped dict (truthy "error") and a raised exception — are cached for
    the shorter negative TTL so a transient fault is still throttled from
    every-poll down to once per window, without pinning a panel for a full
    minute."""
    hit = _LIVE_CACHE.get(key)
    if hit is not None and (time.monotonic() - hit[0]) < hit[2]:
        cached = hit[1]
        if isinstance(cached, _CachedError):
            raise cached.exc
        return cached

    try:
        val = producer()
    except Exception as exc:
        # Cache the failure (negative TTL) BEFORE re-raising so the next poll in
        # this window is served from cache instead of re-hitting the live API.
        # Re-raising preserves the existing 500 behaviour at the routing layer.
        _store_live(key, _CachedError(exc), _LIVE_NEG_TTL)
        raise

    entry_ttl = _LIVE_NEG_TTL if (isinstance(val, dict) and val.get("error")) else ttl
    _store_live(key, val, entry_ttl)
    return val


def _overview(query, cluster_id):
    # The four reads below are independent (different tables, no shared
    # state) and each is a separate RDS Data API round-trip (~100-300ms of
    # HTTP RTT). Run sequentially this is the dominant click→render latency,
    # since the whole dashboard body waits on /overview. Fan them out across
    # a small thread pool so the wall-clock cost collapses from 4 serial RTTs
    # to ~1 (the slowest single query). botocore low-level clients are
    # thread-safe for operation calls, so the shared rds-data client behind
    # `query` is safe to invoke concurrently. Throttle posture is unchanged:
    # still 4 Data API calls, just not serialized (Data API's region quota,
    # ~1000 req/s, is not the binding constraint here — the live-describe
    # endpoints are; see _cached_live).
    reads = (
        (
            "meta",
            "SELECT * FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        ),
        (
            "metrics",
            "SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val "
            "FROM metric_snapshots WHERE cluster_id = :cid AND ts > NOW() - INTERVAL '1 hour' "
            f"{CLUSTER_LEVEL_ONLY} "
            "GROUP BY metric_type",
            {"cid": cluster_id},
        ),
        (
            "top_queries",
            "SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms "
            "FROM query_stats WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
            "ORDER BY total_time_ms DESC LIMIT 10",
            {"cid": cluster_id},
        ),
        (
            "events",
            "SELECT id, event_time as ts, event_type, severity, source, message, raw_event "
            "FROM event_log WHERE cluster_id = :cid "
            "ORDER BY event_time DESC LIMIT 10",
            {"cid": cluster_id},
        ),
    )
    with ThreadPoolExecutor(max_workers=len(reads)) as ex:
        futures = {
            name: ex.submit(query, sql, params) for name, sql, params in reads
        }
        results = {name: fut.result() for name, fut in futures.items()}

    meta = results["meta"]
    recent_metrics = results["metrics"]
    top_queries = results["top_queries"]
    recent_events = results["events"]

    cluster_row = meta[0] if meta else None

    # Cold-resource fallback: if cluster_meta has no row yet (first ETL not run),
    # synthesise a minimal cluster stub from the registry so the frontend can gate
    # on engine_family correctly instead of defaulting to "relational".
    if not cluster_row:
        reg = _lookup_cluster(cluster_id)
        if reg:
            eng = reg.get("engine") or ""
            cluster_row = {
                "cluster_id": cluster_id,
                "engine": eng,
                "engine_family": engine_family(eng),
            }

    return {
        "cluster": cluster_row,
        "metrics": recent_metrics,
        "top_queries": top_queries,
        "events": recent_events,
    }


def _timeseries(query, cluster_id, metric_type, hours, from_iso=None, to_iso=None):
    """Single-metric timeseries. Same window precedence as _batch_timeseries —
    absolute (from/to) overrides relative (hours). Server-side bucketed to a
    bounded point count (see _bucket_seconds) so a wide window doesn't return
    thousands of sub-pixel raw points."""
    bucket = _bucket_seconds(hours, from_iso, to_iso)
    # INTENTIONALLY dimensioned: the row's `dimensions` text is returned and the
    # GROUP BY keeps it, so the AAS per-wait-event / SQL-Server per-wait_type /
    # per-GSI breakdowns survive for the stacked charts. EXCLUDE_PER_INSTANCE (not
    # CLUSTER_LEVEL_ONLY) is the correct filter here: drop the per-instance
    # duplicates, keep the detail rows the chart splits on.
    head = (
        f"SELECT {_BUCKET_TS_EXPR} AS ts, AVG(value)::double precision AS value, "
        f"dimensions::text as dimensions "
        f"FROM metric_snapshots "
        f"WHERE cluster_id = :cid AND metric_type = :mt "
        f"{EXCLUDE_PER_INSTANCE} "
    )
    tail = (
        "GROUP BY 1, dimensions::text "
        "ORDER BY 1 ASC"
    )
    if from_iso and to_iso:
        rows = query(
            head + "AND ts >= :from_ts::timestamptz AND ts <= :to_ts::timestamptz " + tail,
            {"cid": cluster_id, "mt": metric_type, "from_ts": from_iso, "to_ts": to_iso, "bucket": bucket},
        )
    else:
        rows = query(
            head + "AND ts > NOW() - (:hours || ' hours')::interval " + tail,
            {"cid": cluster_id, "mt": metric_type, "hours": str(hours), "bucket": bucket},
        )
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "hours": hours,
        "from": from_iso,
        "to": to_iso,
        "bucket_seconds": bucket,
        "points": rows,
    }


def _slow_queries(query, cluster_id, hours, threshold_ms):
    rows = query(
        "SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms, rows_returned "
        "FROM query_stats "
        "WHERE cluster_id = :cid "
        "AND snapshot_time > NOW() - (:hours || ' hours')::interval "
        "AND mean_time_ms >= :threshold "
        "ORDER BY mean_time_ms DESC "
        "LIMIT 20",
        {"cid": cluster_id, "hours": str(hours), "threshold": float(threshold_ms)},
    )
    return {"cluster_id": cluster_id, "hours": hours, "threshold_ms": threshold_ms, "slow_queries": rows}


def _query_detail(query, cluster_id, query_hash):
    rows = query(
        "SELECT snapshot_time, calls, total_time_ms, mean_time_ms, rows_returned, "
        "shared_blks_hit, shared_blks_read, query_text "
        "FROM query_stats "
        "WHERE cluster_id = :cid AND query_hash = :qh "
        "ORDER BY snapshot_time DESC LIMIT 100",
        {"cid": cluster_id, "qh": query_hash},
    )
    return {"cluster_id": cluster_id, "query_hash": query_hash, "snapshots": rows}


def _workload_diff(query, cluster_id, before_iso, after_iso, regression_pct, match_window_min):
    """Diff the workload (pg_stat_statements snapshot) between two points
    in time. Answers "what changed in the workload around this deploy?".

    For each side we take, per query_hash, the single most-recent
    snapshot at-or-before the target timestamp but no older than
    match_window_min (so a hash last seen days ago isn't treated as
    "present" at the target). This gives a "what the workload looked
    like around time T" picture.

    Buckets (by query_hash):
      new          — present at `after`, absent at `before`
      disappeared  — present at `before`, absent at `after`
      regressed    — present both sides, mean_time_ms worsened by
                     ≥ regression_pct
      improved     — present both sides, mean_time_ms improved by
                     ≥ regression_pct (informational; helps confirm a
                     fix landed)

    Caveat surfaced in the response: mean_time_ms from pg_stat_statements
    is cumulative-since-reset, so the comparison is "average over the
    query's lifetime at snapshot A vs B". For a freshly-reset counter
    this tracks recent behavior closely; for a long-lived counter it's
    dampened. We note this in `methodology` so the DBA reads the numbers
    correctly.
    """
    # Resolve each side: latest snapshot per query_hash <= target ts and
    # within the match window. DISTINCT ON gives one row per hash.
    side_sql = (
        "SELECT DISTINCT ON (query_hash) "
        "  query_hash, LEFT(query_text, 300) AS query_excerpt, "
        "  calls, total_time_ms, mean_time_ms, rows_returned, snapshot_time "
        "FROM query_stats "
        "WHERE cluster_id = :cid "
        "  AND snapshot_time <= (:ts)::timestamptz "
        "  AND snapshot_time >= (:ts)::timestamptz - (:win || ' minutes')::interval "
        "ORDER BY query_hash, snapshot_time DESC"
    )

    before_rows = query(side_sql, {"cid": cluster_id, "ts": before_iso, "win": str(match_window_min)})
    after_rows = query(side_sql, {"cid": cluster_id, "ts": after_iso, "win": str(match_window_min)})

    before = {r["query_hash"]: r for r in before_rows}
    after = {r["query_hash"]: r for r in after_rows}
    before_hashes = set(before)
    after_hashes = set(after)

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    new = []
    for h in after_hashes - before_hashes:
        r = after[h]
        new.append({
            "query_hash": h,
            "query_excerpt": r.get("query_excerpt"),
            "mean_time_ms": _f(r.get("mean_time_ms")),
            "calls": r.get("calls"),
        })

    disappeared = []
    for h in before_hashes - after_hashes:
        r = before[h]
        disappeared.append({
            "query_hash": h,
            "query_excerpt": r.get("query_excerpt"),
            "mean_time_ms": _f(r.get("mean_time_ms")),
        })

    regressed = []
    improved = []
    factor = 1.0 + (regression_pct / 100.0)
    for h in before_hashes & after_hashes:
        b_mean = _f(before[h].get("mean_time_ms"))
        a_mean = _f(after[h].get("mean_time_ms"))
        if b_mean <= 0:
            continue  # can't compute a ratio off a zero baseline
        delta_pct = round((a_mean - b_mean) / b_mean * 100.0, 1)
        entry = {
            "query_hash": h,
            "query_excerpt": after[h].get("query_excerpt"),
            "before_mean_ms": round(b_mean, 2),
            "after_mean_ms": round(a_mean, 2),
            "delta_pct": delta_pct,
        }
        if a_mean >= b_mean * factor:
            regressed.append(entry)
        elif b_mean >= a_mean * factor:
            improved.append(entry)

    # Most-impactful first.
    new.sort(key=lambda x: x["mean_time_ms"], reverse=True)
    regressed.sort(key=lambda x: x["delta_pct"], reverse=True)
    improved.sort(key=lambda x: x["delta_pct"])

    return {
        "cluster_id": cluster_id,
        "before": before_iso,
        "after": after_iso,
        "regression_pct": regression_pct,
        "match_window_min": match_window_min,
        "totals": {
            "before_distinct_queries": len(before_hashes),
            "after_distinct_queries": len(after_hashes),
            "new": len(new),
            "disappeared": len(disappeared),
            "regressed": len(regressed),
            "improved": len(improved),
        },
        "new": new[:50],
        "regressed": regressed[:50],
        "improved": improved[:50],
        "disappeared": disappeared[:50],
        "methodology": (
            "mean_time_ms is pg_stat_statements cumulative-since-reset; "
            "comparison reflects lifetime average at each snapshot. "
            "Per query_hash we use the latest snapshot at-or-before each "
            f"target within a {match_window_min}-minute window."
        ),
    }


METRIC_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,49}$")


# 변경 영향 회고에서 전후 델타가 의미 있는 핵심 메트릭. direction은 UI가
# 개선/악화 색을 칠하는 기준 — 대부분 lower=좋음, 캐시 히트는 higher=좋음,
# 커넥션·IOPS는 워크로드 자체라 중립(증감을 가치판단하지 않음).
_IMPACT_METRICS = [
    ("cpu", "CPU", "lower"),
    ("aas", "Active Sessions (AAS)", "lower"),
    ("db_connections", "Connections", "neutral"),
    ("read_iops", "Read IOPS", "neutral"),
    ("write_iops", "Write IOPS", "neutral"),
    ("deadlocks", "Deadlocks", "lower"),
    ("buffer_cache_hit", "Buffer Cache Hit %", "higher"),
]


def _change_impact(query, cluster_id, window_hours, days):
    """변경 영향 자동 회고 — event_log의 RDS 변경 이벤트를 앵커로, 전후 동일
    윈도우의 핵심 메트릭을 비교해 '이 변경 후 무엇이 좋아졌/나빠졌나'를 수치로
    보여준다. DBA가 compare 페이지에서 수동으로 기간을 맞춰 비교하던 일을
    변경 이벤트마다 자동으로 해준다.

    앵커는 RDS 컨트롤플레인 이벤트(source=aws.rds, configuration change /
    maintenance / parameter / reboot / scaling / upgrade 류)다 — DBOps 경유
    여부와 무관하게 콘솔·CLI 직접 변경까지 포착한다. dbops-monitor가 만든
    anomaly_* 이벤트는 변경이 아니므로 source 필터로 제외한다."""
    events = query(
        "SELECT id, event_time, event_type, message FROM event_log "
        "WHERE cluster_id = :cid AND source = 'aws.rds' "
        "  AND event_time > NOW() - (:days || ' days')::interval "
        "  AND ("
        "    event_type ILIKE '%config%' OR event_type ILIKE '%maintenance%' "
        "    OR message ILIKE '%modif%' OR message ILIKE '%parameter%' "
        "    OR message ILIKE '%reboot%' OR message ILIKE '%reset%' "
        "    OR message ILIKE '%scal%' OR message ILIKE '%upgrad%'"
        "  ) "
        "ORDER BY event_time DESC LIMIT 20",
        {"cid": cluster_id, "days": str(days)},
    )

    metric_list = [m[0] for m in _IMPACT_METRICS]
    placeholders = ", ".join(f":m{i}" for i in range(len(metric_list)))

    changes = []
    for ev in events:
        anchor = ev["event_time"]
        params = {"cid": cluster_id, "anchor": anchor, "win": str(window_hours)}
        for i, m in enumerate(metric_list):
            params[f"m{i}"] = m
        # 앵커 ±윈도우 범위만 읽고, FILTER로 앵커 기준 전/후를 한 번에 집계.
        # 후 윈도우가 NOW를 넘으면(아주 최근 변경) after 표본이 적을 수 있어
        # before_n/after_n을 같이 돌려 UI가 신뢰도를 판단하게 한다.
        rows = query(
            "SELECT metric_type, "
            "  AVG(value) FILTER (WHERE ts <= :anchor::timestamptz) AS before_avg, "
            "  AVG(value) FILTER (WHERE ts >  :anchor::timestamptz) AS after_avg, "
            "  COUNT(*)   FILTER (WHERE ts <= :anchor::timestamptz) AS before_n, "
            "  COUNT(*)   FILTER (WHERE ts >  :anchor::timestamptz) AS after_n "
            "FROM metric_snapshots "
            f"WHERE cluster_id = :cid AND metric_type IN ({placeholders}) "
            "  AND ts > :anchor::timestamptz - (:win || ' hours')::interval "
            "  AND ts < :anchor::timestamptz + (:win || ' hours')::interval "
            "  AND (dimensions IS NULL OR dimensions::text = '{}') "
            "GROUP BY metric_type",
            params,
        )
        by_metric = {r["metric_type"]: r for r in rows}
        deltas = []
        for key, label, direction in _IMPACT_METRICS:
            r = by_metric.get(key)
            if not r or r.get("before_avg") is None or r.get("after_avg") is None:
                continue
            if int(r.get("before_n") or 0) < 3 or int(r.get("after_n") or 0) < 3:
                continue  # 표본 부족 — 노이즈 방지로 생략
            before = float(r["before_avg"])
            after = float(r["after_avg"])
            delta = after - before
            pct = (delta / before * 100) if before else None
            deltas.append({
                "metric": key, "label": label, "direction": direction,
                "before": round(before, 3), "after": round(after, 3),
                "delta": round(delta, 3),
                "delta_pct": round(pct, 1) if pct is not None else None,
            })
        changes.append({
            "event_id": ev["id"],
            "event_time": ev["event_time"],
            "event_type": ev["event_type"],
            "message": (ev.get("message") or "")[:200],
            "window_hours": window_hours,
            "deltas": deltas,
        })

    return {"cluster_id": cluster_id, "window_hours": window_hours, "days": days, "changes": changes}


# Charts are ~600px wide; beyond a few hundred points per series the extra
# resolution is sub-pixel. metric_snapshots is 1-minute granularity, so a 24h
# window returns ~1440 raw points/metric (×8 metrics, ×AAS wait-event
# dimensions) → a ~1.4MB payload and ~2s of RDS Data API marshaling + JSON
# serialize, and at the wide end brushes the Data API's 1MB result cap. We
# instead bucket server-side to a bounded point count regardless of window —
# bounding payload, query time, transfer, AND Data-API result size at once.
TS_TARGET_POINTS = 240

# Bucket boundary, computed from the UTC epoch so it's timezone-independent
# (metric_snapshots.ts is timestamptz/UTC). to_char emits an explicit-Z ISO
# string directly — sidestepping the _norm_ts path and any naive-datetime
# ambiguity. The bucket string is zero-padded fixed-width, so its lexical order
# IS chronological order — which lets the queries GROUP BY 1 / ORDER BY 1 on
# this column (PG won't reliably match the nested floor() expression between
# SELECT and GROUP BY when a parameter is involved; the ordinal sidesteps that).
_BUCKET_TS_EXPR = (
    "to_char("
    "to_timestamp(floor(extract(epoch from ts) / :bucket) * :bucket) AT TIME ZONE 'UTC', "
    "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
)


def _bucket_seconds(hours, from_iso=None, to_iso=None):
    """Bucket width (≥60s, never finer than the 1-min source granularity) that
    keeps a window to ~TS_TARGET_POINTS points. For a 1h window this resolves
    to 60s — i.e. a no-op that preserves the existing default-load behaviour —
    and only downsamples once the window is wide enough to need it."""
    span = None
    if from_iso and to_iso:
        try:
            from datetime import datetime

            f = datetime.fromisoformat(str(from_iso).replace("Z", "+00:00"))
            t = datetime.fromisoformat(str(to_iso).replace("Z", "+00:00"))
            s = (t - f).total_seconds()
            if s > 0:
                span = s
        except Exception:
            span = None
    if span is None:
        span = max(1, int(hours)) * 3600
    return max(60, int(span // TS_TARGET_POINTS))


def _batch_timeseries(
    query,
    cluster_id,
    metric_names,
    hours,
    offset_hours=0,
    from_iso=None,
    to_iso=None,
    instance=None,
):
    """Returns metric series within the requested time window.

    Window selection precedence:
      1. If both `from_iso` and `to_iso` are valid TIMESTAMPTZ strings →
         use [from_iso, to_iso] as an *absolute* window. This is what the
         Dashboard custom time picker emits.
      2. Otherwise → use the legacy relative window
         (NOW - hours, NOW - offset_hours].

    The absolute path makes the result deterministic across requests (a
    URL with from/to can be shared), while the relative path keeps every
    legacy caller working unchanged."""
    metric_names = [m for m in metric_names if METRIC_NAME_RE.match(m)][:20]
    base_meta = {
        "cluster_id": cluster_id,
        "hours": hours,
        "offset_hours": offset_hours,
        "from": from_iso,
        "to": to_iso,
        "instance": instance,
    }
    if not metric_names:
        return {**base_meta, "series": {}}

    placeholders = ", ".join(f":m{i}" for i in range(len(metric_names)))
    bucket = _bucket_seconds(hours, from_iso, to_iso)
    params = {"cid": cluster_id, "bucket": bucket}
    base_meta["bucket_seconds"] = bucket
    for i, m in enumerate(metric_names):
        params[f"m{i}"] = m

    # Bucketed read: AVG(value) per (time bucket, metric_type, dimensions).
    # Grouping by dimensions::text preserves the AAS per-wait-event breakdown
    # the stacked chart needs; non-dimensional metrics collapse to one series.
    # AVG is a visual downsample — at the default 1h window bucket=60s is a
    # no-op (exact 1-min points); at wide windows it smooths sparse spikes
    # (e.g. a one-minute deadlock burst). That's acceptable here because spike
    # DETECTION lives in the raw-data endpoints (/anomalies, /events,
    # /health-findings), not in these chart series.
    #
    # Dimensions filter: per-instance rows (dimensions->>'instance' IS NOT NULL)
    # are excluded from cluster-level queries so existing charts are unaffected.
    # When `instance` is given, we narrow to that instance's rows only.
    # `jsonb_exists(col, key)` is used instead of the `?` operator because the
    # RDS Data API rejects `?` as a positional-parameter character.
    inst_clause = (
        " AND dimensions->>'instance' = :inst"
        if instance
        else f" {EXCLUDE_PER_INSTANCE}"
    )
    select_head = (
        f"SELECT {_BUCKET_TS_EXPR} AS ts, metric_type, "
        f"AVG(value)::double precision AS value, dimensions::text AS dimensions "
        f"FROM metric_snapshots "
        f"WHERE cluster_id = :cid AND metric_type IN ({placeholders})"
        f"{inst_clause} "
    )
    if instance:
        params["inst"] = instance
    group_tail = (
        "GROUP BY 1, metric_type, dimensions::text "
        "ORDER BY 1 ASC"
    )

    use_absolute = bool(from_iso) and bool(to_iso)
    if use_absolute:
        params["from_ts"] = from_iso
        params["to_ts"] = to_iso
        sql = select_head + (
            "AND ts >= :from_ts::timestamptz AND ts <= :to_ts::timestamptz "
        ) + group_tail
    else:
        params["hours"] = str(hours)
        params["offset"] = str(offset_hours)
        # Window: (NOW - hours, NOW - offset_hours]. When offset_hours=0 the
        # upper bound collapses to NOW, matching the original "last N hours"
        # semantics.
        sql = select_head + (
            "AND ts > NOW() - (:hours || ' hours')::interval "
            "AND ts <= NOW() - (:offset || ' hours')::interval "
        ) + group_tail

    rows = query(sql, params)

    series = {m: [] for m in metric_names}
    for r in rows:
        mt = r.get("metric_type")
        if mt in series:
            series[mt].append({"ts": r["ts"], "value": r["value"], "dimensions": r.get("dimensions")})
    return {**base_meta, "series": series}


def _registered_clusters() -> dict[str, dict] | None:
    """Return {cluster_id: {"engine": ...}} for the DDB registry, or None if
    the registry is unreachable / not configured.

    The registry is the source of truth in BOTH directions:
      - cache rows whose registration was deleted are ghosts → filtered out;
      - registered clusters with NO cache row yet (new registration, broken
        ETL) must still APPEAR in Fleet — those are exactly the ones an
        operator needs to notice, so the overview synthesizes a metric-less
        row for them instead of letting them vanish."""
    if not _CLUSTERS_TABLE_NAME:
        return None
    try:
        tbl = boto3.resource("dynamodb").Table(_CLUSTERS_TABLE_NAME)
        out: dict[str, dict] = {}
        kwargs: dict = {"ProjectionExpression": "cluster_id, engine, team_id"}
        while True:
            resp = tbl.scan(**kwargs)
            for item in resp.get("Items", []):
                cid = item.get("cluster_id")
                if cid:
                    out[cid] = {"engine": item.get("engine") or "", "team_id": item.get("team_id") or ""}
            last = resp.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last
    except Exception as e:
        print(f"[dashboard] registered cluster scan failed: {e}")
        return None


def _learning_overview(query, event=None):
    rows = query(
        "SELECT cluster_id, symptom_class, action_class, successes, attempts, last_outcome "
        "FROM remediation_outcomes_agg ORDER BY attempts DESC LIMIT 500"
    ) or []
    # Compute visible cluster_id set (mirrors _multi_cluster_overview pattern).
    # fleet rows (cluster_id == '*') are an anonymized aggregate with no real
    # cluster identity — always shown regardless of team membership.
    visible = None  # None => admin => unfiltered
    if event is not None and not tenancy.is_admin(event):
        registered = _registered_clusters()
        if registered is None:
            visible = set()  # fail-closed: registry unavailable + non-admin => no clusters visible
        else:
            items = [
                {"cluster_id": cid, "team_id": (meta or {}).get("team_id") or ""}
                for cid, meta in registered.items()
            ]
            visible = tenancy.visible_cluster_ids(event, items)
            # None returned by visible_cluster_ids => admin => unfiltered
    fleet, clusters = [], {}
    for r in rows:
        if r["cluster_id"] == "*":
            fleet.append(r)  # ponytail: fleet '*' has no real cluster_id, always visible
        elif visible is None or r["cluster_id"] in visible:
            clusters.setdefault(r["cluster_id"], []).append(r)
    recent = query(
        "SELECT cluster_id, symptom_class, action_class, status, evaluated_at "
        "FROM remediation_cases WHERE status IN ('resolved','persisted') "
        "ORDER BY evaluated_at DESC LIMIT 50"
    ) or []
    if visible is not None:
        recent = [c for c in recent if c.get("cluster_id") in visible]
    return {"fleet": fleet, "clusters": clusters, "recent": recent}


def _multi_cluster_overview(query, event=None):
    rows = query(
        "WITH latest_metrics AS ("
        "  SELECT cluster_id, metric_type, "
        "    (array_agg(value ORDER BY ts DESC))[1] AS latest_value "
        "  FROM metric_snapshots "
        # Bound BOTH ends of the window. Without the `ts <= NOW()` upper bound a
        # future-dated snapshot (clock skew, back-test injection) would sort
        # first under `ORDER BY ts DESC` and masquerade as the "latest" value —
        # which made Fleet flip a cluster CRITICAL while the Dashboard health
        # score (computed via _batch_timeseries, already bounded at NOW()) still
        # read HEALTHY. Same now-boundary on both = the two surfaces agree.
        "  WHERE ts > NOW() - INTERVAL '15 minutes' AND ts <= NOW() "
        "  AND metric_type IN ('cpu', 'aas', 'conn_active', 'conn_idle', 'storage_bytes', 'deadlocks') "
        # Cluster-level only: `aas` also lands as one row per PI wait event, so the
        # weak per-instance-only filter used to let a single wait event become the
        # fleet card's "latest AAS" and flip a cluster CRITICAL.
        f"  {CLUSTER_LEVEL_ONLY} "
        "  GROUP BY cluster_id, metric_type"
        "), "
        "agg AS ("
        "  SELECT "
        "    cluster_id, "
        "    MAX(CASE WHEN metric_type='cpu' THEN latest_value END) AS cpu, "
        "    MAX(CASE WHEN metric_type='aas' THEN latest_value END) AS aas, "
        "    MAX(CASE WHEN metric_type='conn_active' THEN latest_value END) AS conn_active, "
        "    MAX(CASE WHEN metric_type='conn_idle' THEN latest_value END) AS conn_idle, "
        "    MAX(CASE WHEN metric_type='storage_bytes' THEN latest_value END) AS storage_bytes, "
        "    MAX(CASE WHEN metric_type='deadlocks' THEN latest_value END) AS deadlocks "
        "  FROM latest_metrics "
        "  GROUP BY cluster_id"
        "), "
        "lock_count AS ("
        "  SELECT cluster_id, COUNT(*) AS blocking_count "
        "  FROM blocking_locks "
        "  WHERE snapshot_time > NOW() - INTERVAL '15 minutes' AND snapshot_time <= NOW() "
        "  GROUP BY cluster_id"
        ") "
        "SELECT "
        "  m.cluster_id, m.engine, m.engine_version, m.status, m.storage_size_gb, "
        "  a.cpu, a.aas, a.conn_active, a.conn_idle, a.storage_bytes, a.deadlocks, "
        "  COALESCE(l.blocking_count, 0) AS blocking_count "
        "FROM cluster_meta m "
        "LEFT JOIN agg a USING (cluster_id) "
        "LEFT JOIN lock_count l USING (cluster_id) "
        "ORDER BY m.cluster_id"
    )
    # Filter out rows whose cluster_id isn't in the DDB registry. The PG
    # cache can carry orphans from old ETL runs or deleted registrations;
    # the DDB table is the source of truth for "what the operator considers
    # an active cluster." On registry-fetch failure we leave the list
    # unfiltered so a transient DDB outage doesn't blank out Fleet.
    registered = _registered_clusters()
    if registered is not None:
        rows = [r for r in rows if r.get("cluster_id") in registered]
        # Registered but never collected (new registration / broken ETL):
        # synthesize a metric-less row so the cluster is VISIBLE in Fleet —
        # the frontend renders null metrics as "-" and treats missing status
        # as neutral, so it surfaces without false-alarming.
        present = {r.get("cluster_id") for r in rows}
        for cid, meta in sorted(registered.items()):
            if cid in present:
                continue
            rows.append({
                "cluster_id": cid,
                "engine": meta.get("engine") or "",
                "engine_version": None,
                "status": None,
                "storage_size_gb": None,
                "cpu": None,
                "aas": None,
                "conn_active": None,
                "conn_idle": None,
                "storage_bytes": None,
                "deadlocks": None,
                "blocking_count": 0,
            })
    # Team-visibility filter. Admin sees all (visible stays None). Non-admin:
    # if the registry is unavailable (None), fail-closed => empty set. Otherwise
    # filter to the caller's visible set.
    visible = None  # None => admin => unfiltered
    if event is not None and not tenancy.is_admin(event):
        if registered is None:
            visible = set()  # fail-closed: registry unavailable + non-admin => no clusters visible
        else:
            items = [
                {"cluster_id": cid, "team_id": (meta or {}).get("team_id") or ""}
                for cid, meta in registered.items()
            ]
            visible = tenancy.visible_cluster_ids(event, items)
    if visible is not None:  # None => admin => unfiltered
        rows = [r for r in rows if r.get("cluster_id") in visible]
    return {"clusters": rows}


def _timeline_category(raw_type: str) -> str:
    """Collapse the varied event_log event_type strings into the small
    set of categories the timeline frontend colors:
      alert / ack / proactive / rds_event  (+ schema_change, audit are
      stamped by their own source queries below).
    Unknown families fall through to 'rds_event' — they all originate
    from the RDS/CloudWatch event pipeline."""
    t = (raw_type or "").lower()
    if t == "alert":
        return "alert"
    if t in ("alert_ack", "ack"):
        return "ack"
    if t == "proactive":
        return "proactive"
    # event_processor writes 'alarm_ok' / 'alarm_alarm' for CW alarm
    # state changes, plus passthrough RDS detail_types. Bucket them all
    # as rds_event so they share the amber chip.
    return "rds_event"


def _timeline(query, cluster_id: str, hours: int, categories: list[str] | None) -> dict:
    """Unified chronological timeline for one cluster.

    Merges the four signal streams a DBA needs at 3am during incident
    triage into a single sorted list:

      - alerts        — alert rule fires (event_log event_type='alert')
      - rds_event     — RDS/CloudWatch events (event_log event_type='rds_event'
                        or whatever event_processor wrote)
      - proactive     — proactive_monitor findings
      - ack           — Slack acks of alerts
      - schema_change — schema_changes table (DDL detected by schema_tracker)
      - audit         — audit_log (executed write operations)
      - slow_peak     — query_stats rows whose total_time_ms jumped past
                        the per-cluster p95 in this window (the "what got
                        slow during the incident" signal)

    Output is a flat list of {ts, category, severity, title, detail, source_id}.
    Frontend renders that as a vertical timeline + category filter chips.

    Window is `hours` back from now. Caller can pass `categories=...` to
    restrict — useful during retro where you only want changes + alerts."""
    cats = set(categories or [])
    items: list[dict] = []

    # event_log already aggregates alerts + RDS events + proactive findings
    # + acks. Pull everything in the window and stamp category from
    # event_type for chip filtering.
    eventlog = query(
        "SELECT id, event_time, event_type, severity, source, "
        "       LEFT(message, 500) AS message, raw_event "
        "FROM event_log "
        "WHERE cluster_id = :cid "
        "  AND event_time > NOW() - (:hours || ' hours')::interval "
        "ORDER BY event_time DESC LIMIT 500",
        {"cid": cluster_id, "hours": str(hours)},
    )
    for r in eventlog:
        raw_type = r.get("event_type") or "event"
        # Normalise the long tail of event_types into the buckets the
        # frontend knows how to color. Writers use varied strings:
        #   alert_evaluator      → 'alert'
        #   slack_interactive    → 'alert_ack'
        #   proactive_monitor    → 'proactive'
        #   event_processor      → 'alarm_<state>' / RDS detail_type /
        #                          arbitrary 'event'
        # so we match by prefix/family rather than exact string.
        cat = _timeline_category(raw_type)
        items.append({
            "ts": r.get("event_time"),
            "category": cat,
            # Keep the original event_type as the title so a normalised
            # 'rds_event' row still tells the DBA it was e.g. a failover.
            "severity": (r.get("severity") or "info"),
            "title": raw_type,
            "detail": r.get("message") or "",
            "source": r.get("source") or "",
            "source_id": f"event_log:{r.get('id')}",
        })

    # schema_changes table — DDL detected by the schema_tracker pipeline.
    try:
        schema_rows = query(
            "SELECT detected_at, change_type, object_name, "
            "       LEFT(old_definition, 200) AS old_def, "
            "       LEFT(new_definition, 200) AS new_def "
            "FROM schema_changes "
            "WHERE cluster_id = :cid "
            "  AND detected_at > NOW() - (:hours || ' hours')::interval "
            "ORDER BY detected_at DESC LIMIT 100",
            {"cid": cluster_id, "hours": str(hours)},
        )
        for r in schema_rows:
            items.append({
                "ts": r.get("detected_at"),
                "category": "schema_change",
                "severity": "info",
                "title": f"{r.get('change_type')} · {r.get('object_name')}",
                "detail": (r.get("new_def") or r.get("old_def") or "")[:200],
                "source": "schema_tracker",
                "source_id": "",
            })
    except Exception as e:
        # schema_changes may not exist on partial deploys; skip silently.
        print(f"[timeline] schema_changes skipped: {e}")

    # audit_log — executed write operations (DDL via execute_sql,
    # parameter changes, scaling). Empty in most deployments today;
    # included so it lights up automatically when the agent starts
    # writing here.
    try:
        audit_rows = query(
            "SELECT id, created_at, action_type, tool_name, requested_by, "
            "       approved_by, LEFT(sql_text, 240) AS sql_text, status "
            "FROM audit_log "
            "WHERE cluster_id = :cid "
            "  AND created_at > NOW() - (:hours || ' hours')::interval "
            "ORDER BY created_at DESC LIMIT 100",
            {"cid": cluster_id, "hours": str(hours)},
        )
        for r in audit_rows:
            items.append({
                "ts": r.get("created_at"),
                "category": "audit",
                "severity": "warning" if r.get("status") == "failed" else "info",
                "title": f"{r.get('tool_name') or r.get('action_type')} · {r.get('status')}",
                "detail": (r.get("sql_text") or "")[:240],
                "source": (
                    f"{r.get('requested_by') or 'agent'} → {r.get('approved_by') or '—'}"
                ),
                "source_id": f"audit_log:{r.get('id')}",
            })
    except Exception as e:
        print(f"[timeline] audit_log skipped: {e}")

    # Sort by ts DESC — most recent first.
    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)

    # Optional category filter post-sort so chip toggling is O(N) not N×SQL.
    if cats:
        items = [i for i in items if i["category"] in cats]

    return {
        "cluster_id": cluster_id,
        "hours": hours,
        "categories": sorted({i["category"] for i in items}),
        "count": len(items),
        "items": items[:500],
    }


def _audit_log(query, cluster_id, days, action_type):
    conditions = ["cluster_id = :cid", "created_at > NOW() - (:days || ' days')::interval"]
    params = {"cid": cluster_id, "days": str(days)}
    if action_type:
        conditions.append("action_type = :at")
        params["at"] = action_type
    rows = query(
        "SELECT id, action_type, tool_name, requested_by, approved_by, "
        "       LEFT(sql_text, 500) AS sql_text, status, created_at, resolved_at "
        "FROM audit_log WHERE " + " AND ".join(conditions) +
        " ORDER BY created_at DESC LIMIT 100",
        params,
    )
    return {"cluster_id": cluster_id, "days": days, "audit_entries": rows}


def _active_sessions(query, cluster_id, hours):
    """High-resolution (~5s) active-session samples from active_session_samples
    (written by the ASH sampler). Most-recent-first, capped to bound the payload;
    returned chronological for charting."""
    rows = query(
        "SELECT ts, active_sessions, top_wait, top_wait_count "
        "FROM active_session_samples "
        "WHERE cluster_id = :cid AND ts > NOW() - (:hours || ' hours')::interval "
        "ORDER BY ts DESC LIMIT 2000",
        {"cid": cluster_id, "hours": str(hours)},
    )
    samples = [
        {"ts": r.get("ts"), "active": r.get("active_sessions"),
         "top_wait": r.get("top_wait"), "top_wait_count": r.get("top_wait_count")}
        for r in reversed(rows)
    ]
    return {"cluster_id": cluster_id, "hours": hours, "samples": samples}


# VERBATIM COPY of _ANOMALY_SQL in
# mcp-servers/mcp_servers/performance/tools/detect_anomalies.py. api/ cannot
# import mcp_servers (no shared Lambda layer), so this is the same
# verbatim-copy + parity-test contract engine_family.py has; byte identity is
# asserted by tests/unit/api/test_dashboard_anomalies.py. It matters here
# because the panel now reports "no baseline trained yet" as a distinct state:
# if the two surfaces classified seasonal/flat differently, the dashboard and
# the chat agent would disagree about whether the cluster is judgeable at all.
# Scoring returns EVERY checked metric (threshold filtering happens in Python)
# so that "0 anomalies" and "0 baselines" stay distinguishable.
#
# Deliberately UNLIMITED. One row per cluster-level metric_type, verified on
# PostgreSQL 14.18: 7 metric_types with 168 trained hour_of_week buckets each, a
# flat baseline, and 30 dimensioned rows each, returns exactly 7 rows (the
# seasonal CTE pins hour_of_week to the current bucket, and the strict dimension
# filter keeps the dimensioned rows out of `recent`).
#
# LIMIT 50 was therefore never reachable on today's collectors. Counted off the
# shipped collector tables, the deepest family is about 30 cluster-level
# metric_types (Aurora PG: 9 cluster CloudWatch + 12 Performance Insights + 5
# pg_activity connection states + 4 pg_stat_database/bgwriter); the others run 6
# (DynamoDB on-demand) to 23 (DocumentDB). 81 distinct CLUSTER-LEVEL metric_type
# literals exist across the whole data-pipeline tree (86 counting the five that
# are only ever written dimensioned), but a cluster only ever runs ONE family
# branch, so neither number is a per-cluster ceiling.
#
# The LIMIT is gone anyway because `total_checked` and the seasonal/flat
# classification have to come from the FULL scored set, not from whatever slice
# the query happened to keep: one new per-object collector would push a cluster
# past 50 and the count would silently cap and the only seasonal baseline outside
# the top-N by |z| would disappear. The display cap is applied in Python, to the
# ROWS RETURNED to the caller.
_ANOMALY_SQL = """
SELECT * FROM (
    WITH current_hour AS (
        SELECT (EXTRACT(DOW FROM NOW())::int * 24 + EXTRACT(HOUR FROM NOW())::int) AS how
    ),
    recent AS (
        SELECT metric_type, MAX(value) AS recent_max, AVG(value) AS recent_avg
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts > NOW() - (:hours || ' hours')::interval
          AND (dimensions IS NULL OR dimensions::text = '{}')
        GROUP BY metric_type
    ),
    seasonal AS (
        SELECT b.metric_type, b.median, b.iqr, b.sample_count
        FROM metric_baselines b, current_hour c
        WHERE b.cluster_id = :cluster_id AND b.hour_of_week = c.how
    ),
    flat AS (
        SELECT metric_type, AVG(value) AS mean, STDDEV(value) AS stddev
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts BETWEEN NOW() - INTERVAL '7 days' AND NOW() - (:hours || ' hours')::interval
          AND (dimensions IS NULL OR dimensions::text = '{}')
        GROUP BY metric_type
        HAVING STDDEV(value) > 0 AND COUNT(*) > 50
    )
    SELECT
        r.metric_type,
        r.recent_max,
        r.recent_avg,
        -- A seasonal row with iqr <= 0 (a metric that's constant at this hour)
        -- can't yield a robust z, so treat it like "no seasonal" and fall back
        -- to the flat baseline instead of dropping the metric entirely.
        CASE WHEN s.iqr > 0 THEN s.median ELSE f.mean END AS baseline_mean,
        CASE WHEN s.iqr > 0 THEN s.iqr ELSE f.stddev END AS baseline_stddev,
        CASE WHEN s.iqr > 0
            THEN (r.recent_max - s.median) / s.iqr
            ELSE (r.recent_max - f.mean) / NULLIF(f.stddev, 0)
        END AS z_score,
        CASE WHEN s.iqr > 0 THEN 'seasonal' ELSE 'flat' END AS mode,
        CASE WHEN s.iqr > 0 THEN s.sample_count ELSE NULL END AS sample_count
    FROM recent r
    LEFT JOIN seasonal s ON s.metric_type = r.metric_type
    LEFT JOIN flat     f ON f.metric_type = r.metric_type
    WHERE (s.iqr > 0 OR f.stddev IS NOT NULL)
) t
WHERE z_score IS NOT NULL
ORDER BY ABS(z_score) DESC
"""

# Existence probe, run ONLY when _ANOMALY_SQL scored nothing, so the normal path
# costs no extra round trip. _ANOMALY_SQL is DRIVEN from its `recent` CTE, so an
# empty result collapses two states whose operator actions are opposite: "no
# recent samples at all" (collection stopped / cluster just registered / every
# recent row is dimensioned) and "samples arrived but no baseline matched".
#
# Same :hours window as the scoring query, or the two answers could disagree.
# CLUSTER_LEVEL_ONLY, never hand-written: this reads metric_snapshots at cluster
# level, and per-instance / per-wait-event / per-GSI rows must NOT count as
# "samples exist" (they are invisible to the scoring query).
#
# VERBATIM COPY of _RECENT_SAMPLES_SQL in the detect_anomalies MCP tool, same
# contract as _ANOMALY_SQL above.
_RECENT_SAMPLES_SQL = f"""
SELECT 1
FROM metric_snapshots
WHERE cluster_id = :cluster_id
  AND ts > NOW() - (:hours || ' hours')::interval
  {CLUSTER_LEVEL_ONLY}
LIMIT 1
"""

# How many anomalies to hand back. What makes these the STRONGEST ones is the
# ORDERING, not where the cap sits: the scoring SQL already emits |z| descending,
# so the rows at or above threshold are a prefix of a sorted list and the cap can
# only ever drop the weakest. Swapping the cap and the threshold filter is an
# equivalent mutation on that input (checked: 0 tests fail when swapped), so do
# not read the placement as load-bearing. Keep in sync with the detect_anomalies
# copy (parity test compares the derived output, not just the SQL).
_MAX_REPORTED = 50


def _anomalies(query, cluster_id, hours, threshold):
    """Seasonal anomaly detection.

    For each metric we have a per-hour-of-week baseline (median + IQR) in
    `metric_baselines`. Robust z-score = (recent_max - median) / IQR
    (on a normal distribution the IQR is ≈ 1.349 stddev, NOT the other way
    round: q(0.75) - q(0.25) = 1.3489795..., analytic and not sampled, so a
    robust z of 2.0 is about
    2.7 sigma and 2.5 is about 3.4. The IQR doesn't blow up on outliers, so the
    score is stable on a cluster that has a handful of legitimate spikes per
    day).

    Falls back to the legacy flat-mean+stddev baseline when no seasonal
    baseline exists for the current bucket (cold-start: less than ~14 days
    of history). The fallback rows are tagged `mode='flat'` so the UI can
    explain why a finding's confidence is lower.

    `baseline_mode` + `total_checked` are the honesty signals, derived exactly
    as detect_anomalies_impl derives them, off the FULL scored set:

      `anomalies`      rows at or above `threshold`, |z| descending, capped at
                       `_MAX_REPORTED`
      `total_checked`  EVERY scored metric, including the ones below threshold
                       and the ones past the display cap
      `baseline_mode`  why the list can be empty, four answers:
                       seasonal / flat  -> scored against that baseline kind
                       none       -> recent cluster-level samples EXIST but no
                                     baseline of either kind matched, nothing
                                     could be scored. Waiting fixes it.
                       no_samples -> no recent cluster-level samples at all
                                     (collection stopped, cluster just
                                     registered, or every recent row is
                                     dimensioned). Waiting does NOT fix it.

    `none` and `no_samples` used to be one state, so a dead collector was
    rendered as "wait about two weeks for a baseline"."""

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    rows = query(_ANOMALY_SQL, {"cluster_id": cluster_id, "hours": str(hours)}) or []
    anomalies = [r for r in rows if abs(_f(r.get("z_score"))) >= _f(threshold)][:_MAX_REPORTED]
    has_seasonal = any(r.get("mode") == "seasonal" for r in rows)
    if rows:
        baseline_mode = "seasonal" if has_seasonal else "flat"
    else:
        probe = query(_RECENT_SAMPLES_SQL, {"cluster_id": cluster_id, "hours": str(hours)})
        baseline_mode = "none" if probe else "no_samples"
    return {
        "cluster_id": cluster_id,
        "hours": hours,
        "threshold": threshold,
        "anomalies": anomalies,
        "total_checked": len(rows),
        "baseline_mode": baseline_mode,
    }


# ---------------------------------------------------------------------------
# GET /api/dashboard/{id}/schema-changes
# ---------------------------------------------------------------------------
# TWO producers feed this panel and only ONE of them can answer a DDL question.
#
# created / dropped come from schema_snapshots (schema_v26) ONLY. Both
# table_stats producers cap their catalog read at the 100 largest tables
# (data-pipeline/etl_collector/collectors/pg_table_stats.py TABLE_STATS_SQL and
# mysql_table_stats.py TABLE_STATS_SQL), so ABSENCE from table_stats means "not
# in the top 100 of that run", which is indistinguishable from "does not exist".
# Deriving DDL from a size-capped producer gave this panel two defects, both
# reproduced against a real PostgreSQL in
# tests/unit/api/test_dashboard_schema_changes_real_pg.py:
#   * `dropped` was UNREACHABLE. `baseline` (newest row older than :days) is a
#     subset of an UNBOUNDED `latest`, so `l.table_name IS NULL` could never be
#     true and a genuinely dropped table was reported as nothing at all.
#   * `created` fired on TOP-100 ENTRANTS. A table that grew into the largest
#     100 has no row older than :days, and the CASE called that 'created'.
# schema_snapshots stores the COMPLETE table map per schema with no LIMIT
# anywhere in its catalog read (deliberately: see schema_snapshot.py), so
# absence there is a real DROP.
#
# Row-count deltas stay on table_stats: n_live_tup exists nowhere else. The cap
# is harmless for a DELTA because a delta needs BOTH endpoints, so a table
# crossing the top-100 boundary yields no row instead of a false claim.
#
# The diff itself is compute_diff from the verbatim-copied schema_diff_util, the
# same function get_schema_diff and the collector use, so the panel and the
# agent cannot describe one DDL event two different ways (a rename is a
# rename_candidate in both, not a DROP + a CREATE).

_SCHEMA_SNAPSHOT_PAIRS_SQL = (
    "WITH snaps AS ("
    "  SELECT schema_name, snapshot_time, tables_json "
    "  FROM schema_snapshots WHERE cluster_id = :cid"
    "), latest AS ("
    "  SELECT DISTINCT ON (schema_name) schema_name, snapshot_time, tables_json "
    "  FROM snaps ORDER BY schema_name, snapshot_time DESC"
    "), oldest AS ("
    "  SELECT DISTINCT ON (schema_name) schema_name, snapshot_time, tables_json "
    "  FROM snaps ORDER BY schema_name, snapshot_time ASC"
    "), base AS ("
    "  SELECT DISTINCT ON (schema_name) schema_name, snapshot_time, tables_json "
    "  FROM snaps WHERE snapshot_time <= NOW() - (:days || ' days')::interval "
    "  ORDER BY schema_name, snapshot_time DESC"
    "), per_schema AS ("
    "  SELECT schema_name, COUNT(*) AS n FROM snaps GROUP BY schema_name"
    ") "
    "SELECT l.schema_name, "
    "  p.n AS snapshots_for_schema, "
    # No snapshot at or before the window start means the history STARTS inside
    # the window: fall back to the oldest snapshot and report the shorter span
    # instead of silently answering for :days we never observed.
    "  COALESCE(b.tables_json, o.tables_json) AS tables_before, "
    "  COALESCE(b.snapshot_time, o.snapshot_time) AS baseline_time, "
    "  (b.schema_name IS NULL) AS baseline_outside_window, "
    "  l.tables_json AS tables_after, "
    "  l.snapshot_time AS current_time, "
    # Uncorrelated scalar subqueries: PostgreSQL evaluates each once as an
    # InitPlan, so cluster-wide coverage costs one extra index scan, not one per
    # schema.
    "  (SELECT COUNT(*) FROM snaps) AS snapshots_stored, "
    "  (SELECT MIN(snapshot_time) FROM snaps) AS first_snapshot, "
    "  (SELECT MAX(snapshot_time) FROM snaps) AS last_snapshot "
    "FROM latest l "
    "JOIN per_schema p ON p.schema_name = l.schema_name "
    "JOIN oldest o ON o.schema_name = l.schema_name "
    "LEFT JOIN base b ON b.schema_name = l.schema_name "
    "ORDER BY l.schema_name"
)

# `latest` is deliberately UNBOUNDED in time: a table the snapshot diff reports
# as dropped needs its LAST OBSERVED row count, which is older than the window
# by definition. `current_in_window` is what decides whether that count may be
# called current, so a stale value can never be presented as a fresh one.
#   ponytail: LIMIT 500 is a bound, not a cap on truth. The DDL list comes from
#   schema_snapshots and is unaffected; above 500 distinct (schema, table) keys
#   the tail loses its row COUNTS only. Raise it or page by schema if a real
#   fleet exceeds it.
_TABLE_STATS_WINDOW_SQL = (
    "WITH latest AS ("
    "  SELECT DISTINCT ON (schema_name, table_name) "
    "    schema_name, table_name, n_live_tup, snapshot_time "
    "  FROM table_stats WHERE cluster_id = :cid "
    "  ORDER BY schema_name, table_name, snapshot_time DESC"
    "), baseline AS ("
    "  SELECT DISTINCT ON (schema_name, table_name) "
    "    schema_name, table_name, n_live_tup, snapshot_time "
    "  FROM table_stats WHERE cluster_id = :cid "
    "    AND snapshot_time <= NOW() - (:days || ' days')::interval "
    "  ORDER BY schema_name, table_name, snapshot_time DESC"
    ") "
    "SELECT l.schema_name, l.table_name, "
    "  b.n_live_tup AS baseline_rows, l.n_live_tup AS current_rows, "
    "  b.snapshot_time AS baseline_time, l.snapshot_time AS current_time, "
    "  (l.snapshot_time > NOW() - (:days || ' days')::interval) AS current_in_window "
    "FROM latest l "
    "LEFT JOIN baseline b "
    "  ON b.schema_name = l.schema_name AND b.table_name = l.table_name "
    "ORDER BY l.snapshot_time DESC, l.n_live_tup DESC NULLS LAST "
    "LIMIT 500"
)

# table_stats is written EVERY ETL run, so its high-water mark is the honest
# answer to "is collection still running for this cluster". schema_snapshots is
# store-on-change and normally writes 0 rows/day, so it cannot carry freshness.
# Age is computed IN SQL: parsing the timestamp back in Python is how this repo
# has produced naive-datetime bugs four times.
_TABLE_STATS_FRESHNESS_SQL = (
    "SELECT MAX(snapshot_time) AS last_collected, "
    "  ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(snapshot_time))))::bigint AS age_sec "
    "FROM table_stats WHERE cluster_id = :cid"
)

# Same threshold api/clusters/handler.py uses for etl_status: 15 minutes is 3
# missed runs at the default STATS_COLLECTION_INTERVAL_MIN of 5.
_FRESH_MAX_AGE_SEC = 15 * 60

_SC_NO_HISTORY = (
    "이 클러스터의 스키마 이력이 아직 없습니다 (schema_snapshots와 table_stats 모두 "
    "이 클러스터 행이 없음). 변경이 없다는 뜻이 아닙니다."
)
_SC_INSUFFICIENT = (
    "수집된 이력이 요청 구간을 걸치지 않아 비교할 기준점이 없습니다. 변경이 없다는 "
    "뜻이 아닙니다. 구간을 늘리거나 수집이 쌓일 때까지 기다려야 합니다."
)
_SC_DDL_NOT_COLLECTED = (
    "테이블 생성·삭제(DDL)는 schema_snapshots로만 판정하는데 이 클러스터의 스냅샷이 "
    "아직 없습니다. DDL 변경이 없다는 뜻이 아닙니다. 다음 ETL 주기에 최초 baseline "
    "스냅샷이 기록되고, 그 다음 변경 시점부터 생성·삭제가 표시됩니다."
)
_SC_DDL_BASELINE_ONLY = (
    "baseline 스냅샷만 있어 DDL 비교 대상이 없습니다 (판정에는 스냅샷 2개가 "
    "필요합니다). 다음 스키마 변경이 감지되면 그 시점의 스냅샷과 비교됩니다."
)
_SC_DDL_UNAVAILABLE = (
    "schema_snapshots를 조회할 수 없어 이번 응답에서는 테이블 생성·삭제(DDL)를 "
    "판정하지 못했습니다 (schema_v26 마이그레이션 적용 여부를 확인하세요). DDL "
    "변경이 없다는 뜻이 아닙니다."
)
_SC_NO_FRESHNESS = (
    "table_stats에 이 클러스터의 수집 기록이 없어 위 판정이 언제 기준인지 확인할 수 "
    "없습니다. 표시된 내용은 마지막으로 기록된 스냅샷 기준입니다."
)
_SC_ROW_DELTA_CAP = (
    "행 수 증감은 table_stats 기준이며, 이 수집기는 매 주기 상위 100개 테이블만 "
    "기록합니다. 상위 100개 밖의 테이블은 증감 판정 대상이 아닙니다."
)


def _schema_changes(query, cluster_id, days):
    """Schema changes over the last `days`, with each claim tied to the producer
    that can actually support it.

    `changes` keeps the exact six fields the panel renders, plus `source`.
    Everything an empty list needs in order NOT to read as "nothing changed"
    lives beside it: `status`, `note`, `ddl_detection`, `row_deltas`,
    `collection`.
    """
    days_s = str(days)

    # --- is collection even running? ---------------------------------------
    fresh = (query(_TABLE_STATS_FRESHNESS_SQL, {"cid": cluster_id}) or [{}])[0]
    last_collected = fresh.get("last_collected")
    age_sec = fresh.get("age_sec")
    age_sec = int(age_sec) if age_sec is not None else None
    if last_collected is None:
        collection = "no_data"
    elif age_sec is not None and age_sec > _FRESH_MAX_AGE_SEC:
        collection = "stale"
    else:
        collection = "fresh"

    # --- row counts, one query serving deltas AND the DDL rows' counts -----
    stat_rows = query(_TABLE_STATS_WINDOW_SQL, {"cid": cluster_id, "days": days_s})
    by_key = {(r.get("schema_name"), r.get("table_name")): r for r in stat_rows}

    # --- DDL from the complete table map ----------------------------------
    ddl_available = True
    try:
        snap_rows = query(_SCHEMA_SNAPSHOT_PAIRS_SQL, {"cid": cluster_id, "days": days_s})
    except Exception as e:
        # schema_snapshots arrives in schema_v26. A cache DB that has not run the
        # migrator yet must degrade to "DDL unknown", not 500 the whole panel.
        # Detail to CloudWatch only: never into the payload.
        print(f"[schema-changes] schema_snapshots unavailable: {type(e).__name__}: {e}")
        snap_rows, ddl_available = [], False

    created, dropped, renames = [], [], []
    snapshotted_schemas, live_keys = set(), set()
    baseline_only, partial_window = [], []
    schemas_compared = 0
    snapshots_stored = 0
    first_snapshot = last_snapshot = None

    for r in snap_rows:
        schema = r.get("schema_name")
        snapshots_stored = int(r.get("snapshots_stored") or 0)
        first_snapshot = r.get("first_snapshot")
        last_snapshot = r.get("last_snapshot")
        snapshotted_schemas.add(schema)
        after = parse_tables(r.get("tables_after"))
        live_keys |= {(schema, t) for t in after}
        if int(r.get("snapshots_for_schema") or 0) < 2:
            # One snapshot is a baseline, not a history. Diffing it against
            # itself would report zero changes for a schema never compared.
            baseline_only.append(schema)
            continue
        if r.get("baseline_outside_window"):
            partial_window.append(schema)
        diff = compute_diff(parse_tables(r.get("tables_before")), after)
        schemas_compared += 1
        detected = r.get("current_time")
        since = r.get("baseline_time")
        for t in diff["added"]:
            known = by_key.get((schema, t)) or {}
            created.append({
                "schema_name": schema, "table_name": t, "change_type": "created",
                # Row counts are a table_stats lookup, so they are the LAST
                # OBSERVED count and are None for anything outside the 100
                # largest. `collection` below dates them. The DDL claim itself
                # does not depend on them.
                "baseline_rows": None, "current_rows": known.get("current_rows"),
                "baseline_time": since, "current_time": detected,
                "source": "schema_snapshots",
            })
        for t in diff["dropped"]:
            known = by_key.get((schema, t)) or {}
            dropped.append({
                "schema_name": schema, "table_name": t, "change_type": "dropped",
                # Last count observed before the table disappeared. None when it
                # was never among the 100 largest, which the panel renders as 0.
                "baseline_rows": known.get("current_rows"),
                "current_rows": None,
                "baseline_time": known.get("current_time") or since,
                "current_time": detected,
                "source": "schema_snapshots",
            })
        renames += [dict(rc, schema_name=schema) for rc in diff["rename_candidates"]]

    ddl_keys = {(c["schema_name"], c["table_name"]) for c in created + dropped}

    # --- row-count deltas --------------------------------------------------
    changed = []
    pairs_compared = 0
    for r in stat_rows:
        key = (r.get("schema_name"), r.get("table_name"))
        base_rows, cur_rows = r.get("baseline_rows"), r.get("current_rows")
        if base_rows is None or cur_rows is None:
            continue  # only one endpoint: a top-100 crossing, not a change
        if not r.get("current_in_window"):
            continue  # newest row predates the window: nothing to call current
        pairs_compared += 1
        if key in ddl_keys:
            continue  # already reported by the snapshot diff
        if key[0] in snapshotted_schemas and key not in live_keys:
            continue  # the complete map says it is gone; this row is a leftover
        base_rows, cur_rows = int(base_rows), int(cur_rows)
        if abs(cur_rows - base_rows) <= max(base_rows * 0.5, 1000):
            continue
        changed.append({
            "schema_name": key[0], "table_name": key[1], "change_type": "changed",
            "baseline_rows": base_rows, "current_rows": cur_rows,
            "baseline_time": r.get("baseline_time"), "current_time": r.get("current_time"),
            "source": "table_stats",
        })

    # Same display order the SQL used to produce: change_type, schema, table.
    ordered = sorted(
        changed + created + dropped,
        key=lambda c: (c["change_type"], c["schema_name"] or "", c["table_name"] or ""),
    )
    changes = ordered[:50]

    # --- what can each source actually support? ----------------------------
    if not ddl_available:
        ddl_status = "unavailable"
    elif snapshots_stored == 0:
        ddl_status = "not_collected"
    elif schemas_compared:
        ddl_status = "ok"
    else:
        ddl_status = "baseline_only"

    if pairs_compared:
        rows_status = "ok"
    elif collection == "no_data":
        rows_status = "no_data"
    else:
        rows_status = "insufficient_history"

    if changes:
        status = "ok"
    elif ddl_status == "ok" or rows_status == "ok":
        status = "no_changes"
    elif snapshots_stored == 0 and collection == "no_data":
        status = "not_collected"
    else:
        status = "insufficient_history"

    notes = []
    if status == "not_collected":
        notes.append(_SC_NO_HISTORY)
    elif status == "insufficient_history":
        notes.append(_SC_INSUFFICIENT)
    if ddl_status == "not_collected":
        notes.append(_SC_DDL_NOT_COLLECTED)
    elif ddl_status == "baseline_only":
        notes.append(_SC_DDL_BASELINE_ONLY)
    elif ddl_status == "unavailable":
        notes.append(_SC_DDL_UNAVAILABLE)
    if partial_window and first_snapshot:
        notes.append(
            f"요청 구간({days}일)보다 스냅샷 이력이 짧아 {first_snapshot} 이후 구간만 "
            f"비교했습니다: {', '.join(sorted(partial_window))}."
        )
    if collection == "stale" and age_sec is not None:
        notes.append(
            f"table_stats 수집이 약 {age_sec // 3600}시간 {(age_sec % 3600) // 60}분 전에 "
            f"멈췄습니다 (마지막 수집 {last_collected}). 표시된 현재 행 수와 DDL 판정은 "
            f"모두 그 시점 기준이며 지금 값이 아닙니다."
        )
    elif collection == "no_data" and status != "not_collected":
        # Snapshots exist but the every-run producer has no row for this cluster,
        # so nothing here can be dated. Store-on-change means an empty DDL diff
        # is only as current as the last collection, whenever that was.
        notes.append(_SC_NO_FRESHNESS)
    if rows_status == "ok":
        notes.append(_SC_ROW_DELTA_CAP)

    return {
        "cluster_id": cluster_id,
        "days": days,
        "status": status,
        "changes": changes,
        "total_changes": len(ordered),
        "truncated": len(ordered) > len(changes),
        "note": " ".join(notes),
        "ddl_detection": {
            "source": "schema_snapshots",
            "status": ddl_status,
            "schemas_compared": schemas_compared,
            "snapshots_stored": snapshots_stored,
            "first_snapshot": first_snapshot,
            "last_snapshot": last_snapshot,
            "baseline_only_schemas": sorted(baseline_only),
            "partial_window_schemas": sorted(partial_window),
            "rename_candidates": renames,
        },
        "row_deltas": {
            "source": "table_stats",
            "status": rows_status,
            "tables_compared": pairs_compared,
            "largest_tables_only": 100,
        },
        "collection": {
            "status": collection,
            "last_collected": last_collected,
            "age_hours": round(age_sec / 3600.0, 1) if age_sec is not None else None,
            "fresh_within_minutes": _FRESH_MAX_AGE_SEC // 60,
        },
    }


def _blocking_locks(query, cluster_id):
    rows = query(
        "SELECT snapshot_time, blocked_pid, blocked_user, blocking_pid, blocking_user, "
        "  blocked_query, blocking_query, locktype, blocked_mode, blocking_mode, "
        "  relation, blocked_duration_sec "
        "FROM blocking_locks "
        "WHERE cluster_id = :cid "
        "AND snapshot_time > NOW() - INTERVAL '15 minutes' "
        "ORDER BY snapshot_time DESC, blocked_duration_sec DESC LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "locks": rows}


def _cluster_settings(query, cluster_id):
    rows = query(
        "SELECT name, value, unit, updated_at FROM cluster_settings "
        "WHERE cluster_id = :cid ORDER BY name",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "settings": rows}


def _long_running(query, cluster_id):
    rows = query(
        "SELECT pid, username, state, duration_sec, xact_duration_sec, "
        "  query_text, wait_event_type, wait_event, client_addr, snapshot_time "
        "FROM long_running_queries "
        "WHERE cluster_id = :cid "
        "AND snapshot_time > NOW() - INTERVAL '15 minutes' "
        "ORDER BY snapshot_time DESC, duration_sec DESC "
        "LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "queries": rows}


def _table_sizes(query, cluster_id):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, total_bytes, table_bytes, index_bytes, snapshot_time "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT schema_name, table_name, n_live_tup, total_bytes, table_bytes, index_bytes, "
        "  CASE WHEN total_bytes > 0 THEN index_bytes::float / total_bytes ELSE 0 END AS index_ratio "
        "FROM latest "
        "WHERE total_bytes IS NOT NULL "
        "ORDER BY total_bytes DESC NULLS LAST "
        "LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "tables": rows}


# DBOps' recommended extensions. Mirrors the static list in
# pg_health_checks.RECOMMENDED_EXTENSIONS so frontend can render a single
# matrix (installed vs recommended). Keep these two lists in sync.
_RECOMMENDED_EXTENSIONS = [
    {"extname": "pg_stat_statements", "severity": "warning",
     "why": "Per-query latency aggregates feed slow-query panels + AI insight."},
    {"extname": "auto_explain", "severity": "info",
     "why": "Auto-captures EXPLAIN for slow queries — invaluable for post-mortem."},
    {"extname": "pgstattuple", "severity": "warning",
     "why": "Precise bloat measurement instead of the size-based estimate."},
    {"extname": "pg_repack", "severity": "info",
     "why": "VACUUM FULL alternative that doesn't take an exclusive lock."},
    {"extname": "pg_hint_plan", "severity": "info",
     "why": "Override planner choices when stats mislead it."},
    {"extname": "pg_cron", "severity": "info",
     "why": "Schedule VACUUM/ANALYZE jobs without an external scheduler."},
]


def _extensions(query, cluster_id):
    """Return the installed extensions list for a cluster plus a recommended-
    extensions matrix with per-row install status. UI shows them side-by-side."""
    installed = query(
        "SELECT extname, extversion, updated_at "
        "FROM cluster_extensions WHERE cluster_id = :cid "
        "ORDER BY extname",
        {"cid": cluster_id},
    )
    installed_names = {r["extname"] for r in installed}
    recommended = [
        {**rec, "installed": rec["extname"] in installed_names}
        for rec in _RECOMMENDED_EXTENSIONS
    ]
    return {
        "cluster_id": cluster_id,
        "installed": installed,
        "recommended": recommended,
    }


# Families whose cluster_health_findings rows come from MORE THAN ONE writer
# Lambda, each on its own EventBridge schedule and each owning a DISJOINT set of
# check_types:
#   rds_instance: etl_collector (cost / capacity_forecast / param_fitness /
#                  query_regression) + rds_direct_collector (InnoDB status)
#   documentdb:   etl_collector docdb_findings (connection_saturation /
#                  cost_oversized / cursor_timeout / low_cache_hit / replica_lag)
#                  + docdb_mongo_collector (docdb_mongo_long_running_ops)
# For these, one global MAX(snapshot_time) returns only whichever Lambda wrote
# last and silently drops the other writer's entire set.
# relational / dynamodb / elasticache each have exactly ONE writer (the ETL
# collector, whose findings collectors all share the cycle's run_ts), so
# MAX(snapshot_time) is exactly right there and stays: it resolves a finding the
# moment the next cycle stops emitting it.
_MULTI_WRITER_FINDING_FAMILIES = frozenset({RDS_INSTANCE, DOCUMENTDB})

# Freshness window for the multi-writer path. Must be >= the LONGEST writer
# interval of any family in the set, or the slower writer's set falls out of the
# window and we are back to the bug. So it is derived from the writers' REAL
# cadence, never from a hardcoded assumption about it:
#   FINDINGS_WRITER_INTERVAL_MIN is the ETL findings collector's schedule
#   (Settings.STATS_COLLECTION_INTERVAL_MIN, wired by cdk/stacks/agent_stack.py).
#   It is per-deployment and gitignored: a deployer who raises it to save cost
#   would otherwise silently hide half of every rds_instance / documentdb
#   cluster's findings again.
# 3x the cadence survives two consecutive missed runs of one writer. The floor is
# 3x the 5-minute rate that rds_direct_collector and docdb_mongo_collector are
# pinned to in data_stack, so an unset or garbage env var can only WIDEN the
# window relative to what ships today, never shrink it.
# Same derivation in mcp_servers/incident/tools/maintenance_findings.py (api/
# cannot import mcp_servers); pinned by a parity test.
_FINDINGS_WINDOW_FLOOR_MIN = 15


def _findings_window_min():
    try:
        cadence = int(os.environ.get("FINDINGS_WRITER_INTERVAL_MIN", ""))
    except ValueError:
        return _FINDINGS_WINDOW_FLOOR_MIN
    return max(_FINDINGS_WINDOW_FLOOR_MIN, cadence * 3)


def _health_findings(query, cluster_id):
    """Return the current maintenance health findings for this cluster. Older
    snapshots stay in the table for trend analysis; the dashboard panel only
    shows what is current.

    Gating is capability-driven: any engine family whose CAPABILITIES["findings"]
    set is non-empty gets findings returned.

    Single-writer families read one global MAX(snapshot_time). Families with two
    writer Lambdas (see _MULTI_WRITER_FINDING_FAMILIES) need the per-check_type
    window instead.

    Registry lookup failure (None) → fail closed: return empty, signal
    registry_unavailable so the UI can show a neutral placeholder."""
    eng = _registry_engine(cluster_id)
    if eng is None:
        # Registry lookup failed — fail closed; do not query the cache DB.
        return {
            "cluster_id": cluster_id,
            "snapshot_time": None,
            "counts": {"critical": 0, "warning": 0, "info": 0},
            "findings": [],
            "registry_unavailable": True,
        }
    fam = engine_family(eng)
    cap = CAPABILITIES.get(fam, {})
    if not cap.get("findings"):
        # Family has no findings collector at all, return empty.
        return {
            "cluster_id": cluster_id,
            "snapshot_time": None,
            "counts": {"critical": 0, "warning": 0, "info": 0},
            "findings": [],
        }
    _COLS = ("id, check_type, severity, subject, value_str, threshold_str, "
            "recommendation, details, snapshot_time")
    if fam in _MULTI_WRITER_FINDING_FAMILIES:
        # Two independent writer Lambdas → pick the latest snapshot *per
        # check_type* inside a freshness window instead of one global
        # MAX(snapshot_time), which would surface only whichever Lambda ran
        # last and hide the other's whole set. Still auto-resolves: a finding
        # no longer re-emitted ages out of the window.
        # Window basis is the cluster's OWN newest finding, not NOW(): a
        # single-snapshot cluster (the seeded demo writes findings once and
        # never re-emits) must keep showing them, and this is also what the
        # agent's get_maintenance_findings does, so the two never disagree.
        # ponytail: MAX(snapshot_time) OVER, not ROW_NUMBER()=1 — capacity_forecast
        # emits several subjects at one snapshot; ROW_NUMBER would keep only one.
        rows = query(
            f"SELECT {_COLS} FROM ("
            f"  SELECT {_COLS}, "
            "    MAX(snapshot_time) OVER (PARTITION BY check_type) AS ct_latest "
            "  FROM cluster_health_findings "
            "  WHERE cluster_id = :cid "
            "    AND snapshot_time >= ("
            "      SELECT MAX(snapshot_time) FROM cluster_health_findings "
            "      WHERE cluster_id = :cid"
            f"    ) - INTERVAL '{_findings_window_min()} minutes'"
            ") ranked "
            "WHERE snapshot_time = ct_latest "
            "ORDER BY "
            "  CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
            "  check_type, subject",
            {"cid": cluster_id},
        )
    else:
        rows = query(
            "WITH latest AS ("
            "  SELECT MAX(snapshot_time) AS ts FROM cluster_health_findings WHERE cluster_id = :cid"
            ") "
            f"SELECT {_COLS} "
            "FROM cluster_health_findings, latest "
            "WHERE cluster_id = :cid AND snapshot_time = latest.ts "
            "ORDER BY "
            "  CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
            "  check_type, subject",
            {"cid": cluster_id},
        )
    counts = {"critical": 0, "warning": 0, "info": 0}
    for r in rows:
        sev = r.get("severity", "info")
        if sev in counts:
            counts[sev] += 1
    # Newest of the returned rows, not rows[0]: on the multi-writer path the rows
    # carry two different snapshot_times and the panel's "as of" must be the
    # freshest one (rows are ordered by severity, so rows[0] is arbitrary).
    snapshot_time = max((r["snapshot_time"] for r in rows if r.get("snapshot_time")),
                        default=None)

    # Remediation Outcome Loop: attach each finding's track record (this cluster,
    # falling back to the '*' fleet rollup) and re-rank proven actions up.
    SEV_RANK = {"critical": 2, "warning": 1, "info": 0}
    for f in rows:
        sclass = f"finding:{f.get('check_type')}"
        agg = query(
            "SELECT COALESCE(SUM(successes),0) AS successes, COALESCE(SUM(attempts),0) AS attempts "
            "FROM remediation_outcomes_agg WHERE cluster_id = :cid AND symptom_class = :sc",
            {"cid": cluster_id, "sc": sclass},
        )
        s, a = (int(agg[0]["successes"]), int(agg[0]["attempts"])) if agg and "successes" in agg[0] else (0, 0)
        if a == 0:  # cold start → fleet prior
            fleet = query(
                "SELECT COALESCE(SUM(successes),0) AS successes, COALESCE(SUM(attempts),0) AS attempts "
                "FROM remediation_outcomes_agg WHERE cluster_id = '*' AND symptom_class = :sc",
                {"sc": sclass},
            )
            s, a = (int(fleet[0]["successes"]), int(fleet[0]["attempts"])) if fleet and "successes" in fleet[0] else (0, 0)
        f["outcome"] = {"successes": s, "attempts": a}
    rows.sort(
        key=lambda f: (SEV_RANK.get(f.get("severity"), 0),
                       f["outcome"]["successes"] / (f["outcome"]["attempts"] + 1)),
        reverse=True,
    )

    return {
        "cluster_id": cluster_id,
        "snapshot_time": snapshot_time,
        "counts": counts,
        "findings": rows,
    }


def _vacuum_stats(query, cluster_id):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, n_dead_tup, "
        "    seq_scan, idx_scan, last_vacuum, last_analyze "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT schema_name, table_name, n_live_tup, n_dead_tup, "
        "  CASE WHEN (n_live_tup + n_dead_tup) > 0 "
        "    THEN n_dead_tup::float / (n_live_tup + n_dead_tup) "
        "    ELSE 0 END AS bloat_ratio, "
        "  seq_scan, idx_scan, last_vacuum, last_analyze "
        "FROM latest "
        "ORDER BY bloat_ratio DESC, n_dead_tup DESC "
        "LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "tables": rows}


def _index_recommendations(query, cluster_id, min_seq_ratio):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, seq_scan, idx_scan, seq_tup_read, n_live_tup "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT schema_name, table_name, seq_scan, idx_scan, seq_tup_read, n_live_tup, "
        "  CASE WHEN (seq_scan + idx_scan) > 0 "
        "    THEN seq_scan::float / (seq_scan + idx_scan) "
        "    ELSE 0 END AS seq_scan_ratio "
        "FROM latest "
        "WHERE seq_scan > 100 AND n_live_tup > 1000 "
        "  AND CASE WHEN (seq_scan + idx_scan) > 0 "
        "    THEN seq_scan::float / (seq_scan + idx_scan) "
        "    ELSE 0 END >= :min_ratio "
        "ORDER BY seq_tup_read DESC "
        "LIMIT 20",
        {"cid": cluster_id, "min_ratio": float(min_seq_ratio)},
    )
    return {"cluster_id": cluster_id, "min_seq_scan_ratio": min_seq_ratio, "candidates": rows}


def _wait_events(query, cluster_id, hours):
    # Performance Insights emits one "total AAS" row per snapshot with no
    # dimensions (the bucket that aggregates everything). Keeping it here
    # would double-count and shows up as a noisy "unknown / unknown" row.
    # We filter it out and derive wait_type from the event name prefix
    # (`IO:DataFileRead` → `IO`) when PI didn't send a type explicitly.
    rows = query(
        "SELECT "
        "  dimensions->>'db.wait_event.name' AS wait_event, "
        "  COALESCE( "
        "    NULLIF(dimensions->>'db.wait_event.type', ''), "
        "    CASE "
        "      WHEN dimensions->>'db.wait_event.name' = 'CPU' THEN 'CPU' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'IO:%' THEN 'IO' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'Lock:%' THEN 'Lock' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'LWLock:%' THEN 'LWLock' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'Client:%' THEN 'Client' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'IPC:%' THEN 'IPC' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'Timeout:%' THEN 'Timeout' "
        # MySQL Performance Insights surfaces wait events as
        # `wait/<type>/<subtype>/...` (e.g. `wait/io/file/innodb/innodb_data_file`).
        # The second segment is the type bucket.
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/io/%' THEN 'IO' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/lock/%' THEN 'Lock' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/synch/%' THEN 'Sync' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/idle/%' THEN 'Idle' "
        "      ELSE 'Other' "
        "    END "
        "  ) AS wait_type, "
        "  AVG(value) AS avg_load, "
        "  MAX(value) AS max_load "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type = 'aas' "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND dimensions IS NOT NULL "
        "  AND dimensions ? 'db.wait_event.name' "
        "  AND dimensions->>'db.wait_event.name' <> '' "
        "GROUP BY wait_event, wait_type "
        "ORDER BY avg_load DESC",
        {"cid": cluster_id, "hours": str(hours)},
    )
    return {"cluster_id": cluster_id, "hours": hours, "wait_events": rows}


# Capacity forecasting: the REST twin of the Performance MCP `forecast_capacity`
# tool (mcp-servers/mcp_servers/performance/tools/forecast_capacity.py).
# Invoking the agent for a dashboard panel is heavy + slow, so the math runs
# directly against the cache DB and the panel renders in one round trip.
#
# ONE VOCABULARY (E1-5). `metric` is a LOGICAL name here too, not a raw
# metric_type. Before this, the tool took storage/connections/aas while this
# endpoint took storage_bytes/db_connections/aas, and `_CAPACITY_METRICS_BY_FAMILY`
# had no rds_instance / elasticache key: the panel answered "not applicable" for a
# cluster the agent handed an exhaustion ETA for. Logical won because the raw name
# is per-family (Aurora storage GROWS as storage_bytes, standalone RDS storage
# DEPLETES as free_storage_bytes), so a caller picking the raw name has to know the
# family first. Statuses, `direction` and `usage_pct` match the tool field for
# field; tests/unit/api/test_capacity_parity.py seeds one series and asserts both
# surfaces return the same verdict.
#
# Every series below was verified against its writer (file:line in the tool's
# docstring). Nothing is enabled on a series no collector writes: the tool once
# defaulted to `storage_gb`, which has no writer, and reported the resulting zero
# samples as a flat trend.
_VOLUME_MAX_BYTES = 128 * 1024**4  # Aurora / DocumentDB cluster volume ceiling
_CAPACITY_FALLBACK_CONNECTIONS = 5000
_CAPACITY_FALLBACK_AAS = 64
_ACU_PER_VCPU = 4.0
_MEMORY_LIMIT_PCT = 100.0
# A far-future ETA is real data but not an alert: bound the flag, not the number.
_ACTIONABLE_HORIZON_DAYS = 365
_VCPU_BY_SIZE = {
    "medium": 2, "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
    "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96, "32xlarge": 128,
}

# Logical metric -> display label. The LIMIT no longer lives here: it is
# per-family and resolved in _capacity_resolve_series, which is why adding a
# family key below is no longer a no-op the way it was when this dict owned both.
_CAPACITY_METRICS = {
    "storage": "Storage",
    "connections": "Connections",
    "aas": "Active Sessions",
    "read_capacity": "Read Capacity (RCU/min)",
    "write_capacity": "Write Capacity (WCU/min)",
    "memory": "Memory",
}

# Logical metric -> family -> the metric_type that family actually collects.
_CAPACITY_STORAGE_SERIES = {
    "relational": "storage_bytes",      # cw_collector.py:5 (VolumeBytesUsed)
    "documentdb": "storage_bytes",      # docdb_cw_collector.py:10
    "rds_instance": "free_storage_bytes",  # rds_instance_cw_collector.py:14
}
_CAPACITY_CONNECTION_SERIES = {
    "relational": "db_connections",     # cw_collector.py:7,26
    "documentdb": "db_connections",     # docdb_cw_collector.py:14
    "rds_instance": "db_connections",   # rds_instance_cw_collector.py:12
}
# `aas` has exactly one writer, pi_collector.py:5,25 (db.load.avg), so only the
# Performance-Insights-capable families have the series.
_CAPACITY_AAS_SERIES = {"relational": "aas", "rds_instance": "aas"}
# consumed_* are per-minute Sums (dynamodb_cw_collector.py:11,12,24,25); the
# ceiling is the latest provisioned_* per-second rate (:19,20) x 60.
_CAPACITY_THROUGHPUT_SERIES = {
    "read_capacity": {"dynamodb": ("consumed_rcu", "provisioned_rcu")},
    "write_capacity": {"dynamodb": ("consumed_wcu", "provisioned_wcu")},
}
# DatabaseMemoryUsagePercentage (elasticache_cw_collector.py:12) is in the
# Redis/Valkey list only. Memcached is refused below: that metric is absent from
# _MEMCACHED_METRICS and its FreeableMemory is host memory, not cache fill.
_CAPACITY_MEMORY_SERIES = {"elasticache": "memory_usage_pct"}

# Which LOGICAL metrics are valid per engine family. A metric outside its
# family's set is status=unsupported_metric with no SQL run.
_CAPACITY_METRICS_BY_FAMILY = {
    "relational": {"storage", "connections", "aas"},
    "documentdb": {"storage", "connections"},
    "rds_instance": {"storage", "connections", "aas"},
    "dynamodb": {"read_capacity", "write_capacity"},
    "elasticache": {"memory"},
}


def _capacity_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _capacity_scalar(query, sql, params):
    """Single `value` column, first row, as a float. Missing row or a
    non-numeric value is 0.0, which every caller treats as "no grounded
    limit" (a fallback is never promoted to grounded=True)."""
    rows = query(sql, params) or [{}]
    return _capacity_float((rows[0] or {}).get("value"))


def _capacity_vcpu_for(instance_class):
    ic = (instance_class or "").lower()
    if not ic or "serverless" in ic:
        return None
    return _VCPU_BY_SIZE.get(ic.rsplit(".", 1)[-1])


def _capacity_connections_limit(query, cluster_id, fam, meta):
    """(limit, basis, grounded). Same precedence as the MCP tool and
    data-pipeline/etl_collector/collectors/capacity_forecast.py:
    cluster_meta.max_connections (demo seeder only), then the latest
    cluster_settings row, then the DocumentDB DatabaseConnectionsLimit
    datapoint (DocDB has no max_connections setting at all)."""
    mc = _capacity_float(meta.get("max_connections"))
    if mc > 0:
        return mc, f"cluster_meta.max_connections={int(mc)}", True
    if fam == "documentdb":
        cl = _capacity_scalar(
            query,
            "SELECT value FROM metric_snapshots "
            "WHERE cluster_id = :cid AND metric_type = 'db_connections_limit' "
            f"  {CLUSTER_LEVEL_ONLY} "
            "ORDER BY ts DESC LIMIT 1",
            {"cid": cluster_id},
        )
        if cl > 0:
            return cl, f"DatabaseConnectionsLimit 최신 관측값={int(cl)}", True
    else:
        cs = _capacity_scalar(
            query,
            "SELECT value FROM cluster_settings "
            "WHERE cluster_id = :cid AND name = 'max_connections' "
            "ORDER BY updated_at DESC LIMIT 1",
            {"cid": cluster_id},
        )
        if cs > 0:
            return cs, f"cluster_settings.max_connections={int(cs)}", True
    return float(_CAPACITY_FALLBACK_CONNECTIONS), "max_connections 미상, 기본값 가정", False


def _capacity_aas_limit(meta):
    """(limit, basis, grounded). Provisioned instances use the instance_class
    vCPU count; Serverless v2 is db.serverless with no vCPU token, so its
    ceiling comes from serverlessv2_max_acu converted at 4 ACU per vCPU."""
    ic = meta.get("instance_class")
    vcpu = _capacity_vcpu_for(ic)
    if vcpu:
        return float(vcpu), f"인스턴스 {ic} vCPU={vcpu} (AAS 포화 기준)", True
    acu = _capacity_float(meta.get("serverlessv2_max_acu"))
    if acu > 0:
        acu_vcpu = max(1.0, acu / _ACU_PER_VCPU)
        return (acu_vcpu,
                f"serverlessv2_max_acu={round(acu, 1)} → vCPU≈{round(acu_vcpu, 1)} (AAS 포화 기준)",
                True)
    return float(_CAPACITY_FALLBACK_AAS), "인스턴스 vCPU 미상(서버리스/미등록), 기본값 가정", False


def _capacity_throughput_limit(query, cluster_id, provisioned_metric):
    """(limit, basis, grounded). provisioned_* is only collected for
    billing_mode == PROVISIONED tables (dynamodb_cw_collector.py:134), so an
    on-demand table has no row and therefore no grounded ceiling. The
    consumption trend is still real data, so it is reported with no date rather
    than refused."""
    prov = _capacity_scalar(
        query,
        "SELECT value FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :pm "
        f"  {CLUSTER_LEVEL_ONLY} "
        "ORDER BY ts DESC LIMIT 1",
        {"cid": cluster_id, "pm": provisioned_metric},
    )
    if prov > 0:
        return (prov * 60.0,
                f"{provisioned_metric} 최신값 {round(prov, 1)}/초 × 60초 = {int(prov * 60)}/분",
                True)
    return 0.0, "온디맨드(프로비저닝 용량 없음), 근거 있는 천장 없음", False


def _capacity_evictions(query, cluster_id, days_lookback):
    """Eviction total inside the window. Greater than 0 means the cache is
    already recycling memory, and "days until 100%" is meaningless for a cache
    pinned near maxmemory by an LRU/TTL policy. evictions is in BOTH the
    Redis/Valkey and Memcached metric lists (elasticache_cw_collector.py:18,33)."""
    return _capacity_scalar(
        query,
        "SELECT COALESCE(SUM(value), 0) AS value FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'evictions' "
        f"  {CLUSTER_LEVEL_ONLY} "
        "  AND ts > NOW() - (:days || ' days')::interval",
        {"cid": cluster_id, "days": str(days_lookback)},
    )


def _capacity_resolve_series(query, cluster_id, metric, fam, engine, meta):
    """(metric_type, limit, basis, grounded). metric_type None means this engine
    collects no such series, so the answer is a refusal rather than a
    zero-sample forecast."""
    if metric == "storage":
        metric_type = _CAPACITY_STORAGE_SERIES.get(fam)
        if metric_type is None:
            return None, 0.0, "", False
        if metric_type == "free_storage_bytes":
            # Free space DEPLETING to a hard floor of 0 bytes = STORAGE_FULL, so
            # the limit is grounded without any config lookup. allocated_storage_gb
            # only adds usage context. RDS storage autoscaling
            # (MaxAllocatedStorage) would push the real date out, but it is not
            # collected into resource_details so it cannot be checked here.
            alloc = _capacity_float(meta.get("allocated_storage_gb"))
            basis = "여유 스토리지 소진(0 bytes)"
            if alloc > 0:
                basis += f", 할당 {int(alloc)}GB"
            return metric_type, 0.0, basis, True
        return metric_type, float(_VOLUME_MAX_BYTES), "클러스터 볼륨 상한 128 TiB", True
    if metric == "connections":
        metric_type = _CAPACITY_CONNECTION_SERIES.get(fam)
        if metric_type is None:
            return None, 0.0, "", False
        return (metric_type, *_capacity_connections_limit(query, cluster_id, fam, meta))
    if metric == "aas":
        metric_type = _CAPACITY_AAS_SERIES.get(fam)
        if metric_type is None:
            return None, 0.0, "", False
        return (metric_type, *_capacity_aas_limit(meta))
    if metric in _CAPACITY_THROUGHPUT_SERIES:
        pair = _CAPACITY_THROUGHPUT_SERIES[metric].get(fam)
        if pair is None:
            return None, 0.0, "", False
        metric_type, provisioned_metric = pair
        return (metric_type, *_capacity_throughput_limit(query, cluster_id, provisioned_metric))
    # metric == "memory"
    metric_type = _CAPACITY_MEMORY_SERIES.get(fam)
    if metric_type is None or "memcached" in (engine or "").lower():
        return None, 0.0, "", False
    return metric_type, _MEMORY_LIMIT_PCT, "메모리 사용률 상한 100%", True


def _capacity_usage_pct(approach_down, current, limit, alloc_gb, samples):
    """0-100 or None, computed server-side so no consumer divides by `limit`.
    The depleting mode's limit is legitimately 0 (free bytes exhausted) and an
    on-demand DynamoDB table has no ceiling at all, so (current/limit)*100
    divides by zero or invents a percentage. Depleting usage is only defined
    against the ALLOCATED size, not against the limit.

    With ZERO samples the answer is None even when a denominator exists: `current`
    is then 0.0 because no row is inside the window, not because 0 was measured,
    and in the depleting mode that 0.0 works out to (alloc - 0)/alloc = 100%, i.e.
    "storage 100% used" for a cluster nothing was measured on. A reviewer got
    exactly that: {status: no_data, samples: 0, current_value: 0.0,
    usage_pct: 100.0} on an rds_instance with allocated_storage_gb 100 and no
    free_storage_bytes rows at all. A denominator is not a measurement."""
    if samples <= 0:
        return None
    if approach_down:
        if alloc_gb > 0:
            total = alloc_gb * 1024**3
            return round(max(0.0, min(100.0, (total - current) / total * 100)), 1)
        return None
    if limit > 0:
        return round(max(0.0, min(100.0, current / limit * 100)), 1)
    return None


def _capacity_reject(cluster_id, metric, status, reason, fam=None):
    out = {
        "cluster_id": cluster_id,
        "metric": metric,
        "status": status,
        "not_applicable": True,
        "reason": reason,
        "samples": 0,
        "days_until_limit": None,
        "approaching_limit": False,
        "grounded": False,
        "usage_pct": None,
    }
    if fam is not None:
        out["engine_family"] = fam
    return out


def _capacity_forecast(query, cluster_id, metric, days_lookback):
    if metric not in _CAPACITY_METRICS:
        return _capacity_reject(
            cluster_id, metric, "unknown_metric",
            f"'{metric}' 는 지원하지 않는 메트릭 이름입니다. "
            f"사용 가능한 값: {', '.join(_CAPACITY_METRICS)}.")
    eng = _registry_engine(cluster_id)
    if eng is None:
        out = _capacity_reject(
            cluster_id, metric, "unknown_cluster",
            "클러스터 레지스트리를 조회할 수 없어 엔진을 확인하지 못했습니다.")
        out["registry_unavailable"] = True
        return out
    # cluster_meta carries every limit input (max_connections, instance_class,
    # serverlessv2_max_acu, allocated_storage_gb) AND the engine the family is
    # derived from. No row means the first ETL cycle has not run, and the MCP tool
    # fail-closes on exactly this condition with the same status: without it the
    # aas / connections ceilings silently become the fleet-wide fallbacks and the
    # two surfaces stop agreeing.
    #
    # This read comes BEFORE the family gate on purpose. The MCP tool derives the
    # family FROM cluster_meta.engine, so it cannot reach a family verdict without
    # the row; if this endpoint rejected the metric first it would answer
    # unsupported_metric where the tool answers unknown_cluster for the same
    # uncollected cluster. Ordering the checks the same way is what keeps the two
    # answers identical. It costs one cheap PK lookup on an already-rejected
    # request, and the expensive regression still never runs.
    meta_rows = query(
        "SELECT engine, max_connections, instance_class, serverlessv2_max_acu, "
        "       resource_details->>'allocated_storage_gb' AS allocated_storage_gb "
        "FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    if not meta_rows:
        return _capacity_reject(
            cluster_id, metric, "unknown_cluster",
            "cluster_meta에 이 클러스터가 없습니다(미등록이거나 첫 메트릭 수집 전). "
            "등록 및 수집 상태를 확인한 뒤 다시 예측하세요.")
    meta = meta_rows[0] or {}
    # ONE source for the engine family, and it is cluster_meta.engine: the engine
    # the COLLECTOR observed, not the registry string an operator typed at
    # registration. The MCP tool derives the family from this same column, and the
    # family decides which metric_type is read (Aurora storage_bytes vs standalone
    # RDS free_storage_bytes), so it has to be the engine that wrote the rows. A
    # registry value that disagrees (typo, engine changed under the same
    # cluster_id) would otherwise send this endpoint to a series nobody writes
    # while the agent reads the real one: measured on the pair, cluster_meta.engine
    # 'mysql' with a registry 'aurora-mysql' made the two answers differ on 7
    # shared keys (family, metric_type, limit, direction, usage_pct, limit_basis,
    # days_until_limit). `eng` survives only as the fail-closed check above: None
    # means the registry lookup itself failed.
    engine = meta.get("engine")
    fam = engine_family(engine)
    allowed = _CAPACITY_METRICS_BY_FAMILY.get(fam) or set()
    if metric not in allowed:
        return _capacity_reject(
            cluster_id, metric, "unsupported_metric",
            f"{fam} 엔진(engine={engine or '미상'})에서는 {metric} 시계열이 수집되지 않아 "
            f"예측할 수 없습니다."
            + (" Memcached는 DatabaseMemoryUsagePercentage를 발행하지 않습니다."
               if metric == "memory" and fam == "elasticache" else ""),
            fam)
    metric_type, limit, limit_basis, grounded = _capacity_resolve_series(
        query, cluster_id, metric, fam, engine, meta)
    if metric_type is None:
        # Family key allowed the metric but the engine inside the family does
        # not collect it (Memcached memory today).
        return _capacity_reject(
            cluster_id, metric, "unsupported_metric",
            f"{fam} 엔진(engine={engine or '미상'})에서는 {metric} 시계열이 수집되지 않아 "
            f"예측할 수 없습니다."
            + (" Memcached는 DatabaseMemoryUsagePercentage를 발행하지 않습니다."
               if metric == "memory" else ""),
            fam)

    # RDS Data API params come through as strings, so the lookback is cast to an
    # interval the same way the other lookback queries in this file do.
    # Float-cast value keeps REGR_SLOPE happy on integer-stored metrics.
    # CLUSTER_LEVEL_ONLY on both the aggregate and `latest`: without the STRICT
    # filter the regression mixes the cluster total with its per-instance /
    # per-wait-event / per-GSI fractions and `latest` can be a fraction row.
    rows = query(
        "SELECT REGR_SLOPE(value::float, EXTRACT(EPOCH FROM ts) / 86400) AS slope, "
        "       REGR_R2(value::float, EXTRACT(EPOCH FROM ts) / 86400)    AS r2, "
        "       (array_agg(value ORDER BY ts DESC))[1]                 AS latest, "
        "       MIN(ts)                                                 AS first_ts, "
        "       MAX(ts)                                                 AS last_ts, "
        "       COUNT(*)                                                AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :mt "
        "AND ts > NOW() - (:days || ' days')::interval "
        f"{CLUSTER_LEVEL_ONLY}",
        {"cid": cluster_id, "mt": metric_type, "days": str(days_lookback)},
    )
    row = rows[0] if rows else {}
    slope = _capacity_float(row.get("slope"))
    r2 = _capacity_float(row.get("r2"))
    current = _capacity_float(row.get("latest"))
    samples = int(_capacity_float(row.get("samples")))

    # free_storage_bytes is the one series that reaches its limit by SHRINKING.
    # This is the second response mode, not a sign flip: it decides the at-limit
    # test, the ETA sign and how a consumer draws the bar.
    approach_down = metric_type == "free_storage_bytes"

    def _days(s):
        # Direction-agnostic: only a positive gap/slope ratio is approaching the
        # limit. Growing needs gap>0 & slope>0, depleting needs gap<0 & slope<0.
        # Sub-day rounds up to 1, never 0 (0 is reserved for "already there").
        if not s:
            return None
        d = (limit - current) / s
        return max(1, int(d)) if d > 0 else None

    # Already AT/PAST the limit is not the same as trending away from it: both
    # used to produce no date and approaching_limit=false, the calmest payload
    # for the most urgent state. Samples are required because `latest` is 0.0
    # when there is no row at all, and 0.0 free bytes reads as STORAGE_FULL.
    at_limit = (
        grounded
        and samples > 0
        and row.get("latest") is not None
        and (current <= limit if approach_down else current >= limit)
    )
    # An LRU/TTL cache sits near maxmemory BY DESIGN, so its slope is about 0 and
    # a "days to 100%" number is noise. If evictions happened the cache is
    # already recycling, so report that instead of an ETA, and do NOT raise the
    # actionable flag (a healthy cache evicting on policy is not exhausting).
    # A cache with ZERO evictions and a rising trend is genuinely filling up
    # (the noeviction-policy case) and keeps the ordinary growing forecast.
    evicting = (
        metric == "memory" and samples > 0
        and _capacity_evictions(query, cluster_id, days_lookback) > 0
    )

    days_until = 0 if at_limit else (_days(slope) if grounded else None)
    heading_to_limit = days_until is not None and not at_limit
    approaching = at_limit or (heading_to_limit and days_until <= _ACTIONABLE_HORIZON_DAYS)
    if evicting:
        days_until = None
        heading_to_limit = False
        approaching = False

    reason = None
    if samples == 0:
        reason = (f"최근 {days_lookback}일 동안 {metric_type} 표본이 없어 추세를 계산할 수 "
                  f"없습니다(수집 미시작이거나 이 클러스터에서 해당 메트릭이 올라오지 않음).")
    elif evicting:
        reason = ("eviction이 발생 중입니다. eviction 정책(LRU/TTL)이 걸린 캐시는 설계상 "
                  "메모리 상한 근처에서 동작하므로 '100% 도달까지 며칠'은 의미가 없습니다. "
                  "정확한 신호는 eviction 양과 hit rate이며 Maintenance Health의 "
                  "elasticache_evictions_spike · elasticache_memory_pressure finding이 "
                  "이를 임계로 관리합니다.")
    elif not grounded:
        reason = (f"한계값을 클러스터 실제 설정에서 확인할 수 없어({limit_basis}) 도달 시점을 "
                  f"단정하지 않습니다. 추세만 참고하세요.")

    out = {
        "cluster_id": cluster_id,
        "metric": metric,
        "metric_type": metric_type,
        "engine_family": fam,
        "label": _CAPACITY_METRICS[metric],
        # Same status vocabulary as the MCP tool: ok | limit_reached | evicting |
        # no_data | unsupported_metric | unknown_metric | unknown_cluster.
        "status": (
            "no_data" if samples == 0
            else "evicting" if evicting
            else "limit_reached" if at_limit
            else "ok"
        ),
        # `current_value` on BOTH surfaces: the MCP tool has always called it
        # that, and two names for the same number is the dual-vocabulary smell
        # E1-5 exists to remove.
        "current_value": current,
        # Same rounding as the MCP tool so a parity check compares values, not
        # float formatting.
        "slope_per_day": round(slope, 4),
        "r2": round(r2, 3),
        "limit": limit,
        "limit_basis": limit_basis,
        "grounded": grounded,
        # up = growing toward a ceiling, down = depleting toward 0.
        "direction": "down" if approach_down else "up",
        # Precomputed: a consumer must never divide by `limit`, which is 0 in the
        # depleting mode and 0 for an on-demand DynamoDB table.
        "usage_pct": _capacity_usage_pct(
            approach_down, current, limit,
            _capacity_float(meta.get("allocated_storage_gb")), samples),
        "days_until_limit": days_until,
        "approaching_limit": approaching,
        "forecast": (
            "no_data" if samples == 0
            else "growing" if slope > 0
            else "stable" if slope == 0
            else "depleting" if approach_down
            else "shrinking"
        ),
        "reason": reason,
        "samples": samples,
        "days_lookback": days_lookback,
        # Clamped at 0: no metric here can go negative, and an unclamped
        # depleting projection renders as negative free bytes.
        "projections": {
            "d30": max(0.0, current + slope * 30),
            "d60": max(0.0, current + slope * 60),
            "d90": max(0.0, current + slope * 90),
        },
    }
    if samples == 0:
        # Same rule as usage_pct: with no samples `current` is 0.0 for lack of a
        # row and `slope` is 0.0 for lack of a trend, so current + slope*d is a
        # fabricated flat line starting at zero (the measured shape was
        # {d30: 0.0, d60: 0.0, d90: 0.0} on an uncollected cluster). The key is
        # dropped rather than zeroed, and `projections` is optional in the client
        # type, so the panel simply renders no projection tiles.
        out.pop("projections")
    return out


# PG log filter patterns per category. The model that drives the AI panel
# can already query CloudWatch Logs through the search_logs MCP tool, but a
# pre-categorized dashboard panel is what DBAs actually scan — pganalyze /
# Datadog DBM ship the same shape (Log Insights / Database Logs).
_LOG_CATEGORY_FILTERS = {
    "slow": "filter @message like /duration: [0-9.]+ ms/",
    "vacuum": (
        "filter @message like /automatic vacuum/ or @message like "
        "/automatic analyze/"
    ),
    "error": (
        "filter @message like /ERROR:/ or @message like /FATAL:/ or "
        "@message like /PANIC:/"
    ),
    "connection": (
        "filter @message like /connection received/ or @message like "
        "/connection authorized/ or @message like /disconnection:/"
    ),
}


_MYSQL_LOG_CATEGORY_FILTERS = {
    # Aurora MySQL error log uses bracketed level prefixes; reading the
    # actual lines requires no filter, but we still gate it so the user
    # gets only ERROR/Warning/Note severities.
    "error": "filter @message like /\\[ERROR\\]/ or @message like /\\[FATAL\\]/",
    # MySQL general log connection events.
    "connection": (
        "filter @message like /Connect/ or @message like /Quit/ "
        "or @message like /Aborted connection/"
    ),
    # "slow" goes to a different log group entirely (/aws/rds/cluster/{cid}/slowquery)
    # and the entire group is slow queries — no filter needed.
    "slow": "",
    # MySQL has no autovacuum analog. We leave the key in place for UI
    # parity but use a filter that won't match anything useful.
    "vacuum": "filter @message like /InnoDB/",
}

# Per-category MySQL log group routing — slow queries and error log
# are separate streams. Default (and `all`) goes to the error log.
_MYSQL_LOG_GROUPS = {
    "slow": "slowquery",
    "connection": "general",
    "error": "error",
    "vacuum": "error",
    "all": "error",
}


def _log_insights(cluster_id, hours, category, keywords: str = ""):
    """Run a CloudWatch Logs Insights query for one category of DB logs.

    Returns the most recent matching entries (raw @timestamp + @message) so
    the frontend can render them as a feed. We deliberately do NOT pre-
    aggregate into time-buckets here — DBAs reach for log insights when
    they want to see the actual line, not a count. Tight default cap (100
    entries) keeps CW Insights scan cost predictable.

    Engine-aware: PG clusters log to a single
    /aws/rds/cluster/{cid}/postgresql group; Aurora MySQL splits logs
    across error / slowquery / general / audit streams so we pick the
    matching one per category.

    Optional `keywords` — free-text DBA input compiled into an AND chain
    of `@message like /word/` filters. Empty string means no extra
    filter beyond category. We deliberately keep this compile-side (not
    LLM) so the user can see the resulting query in the response and
    understand why a row did or didn't match.
    """
    import re
    import time

    # Gate: non-relational clusters have no /aws/rds/cluster/... log groups.
    # Registry lookup failure (None) is also treated as fail closed — do NOT
    # create CloudWatch Logs clients or build log-group paths for unknown types.
    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "entries": [], "count": 0, "category": category, "hours": hours}
    if engine_family(eng) != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True,
                "engine_family": engine_family(eng),
                "entries": [], "count": 0, "category": category, "hours": hours}

    cluster = _lookup_cluster(cluster_id)
    engine = (cluster.get("engine") or "").lower() if cluster else ""
    is_mysql = "mysql" in engine

    if is_mysql:
        suffix = _MYSQL_LOG_GROUPS.get(category, "error")
        log_group = f"/aws/rds/cluster/{cluster_id}/{suffix}"
        category_filters = _MYSQL_LOG_CATEGORY_FILTERS
    else:
        log_group = f"/aws/rds/cluster/{cluster_id}/postgresql"
        category_filters = _LOG_CATEGORY_FILTERS

    # Cross-account-aware: the RDS log group lives in the cluster's own account.
    # Reuse the registry row already fetched above to avoid a second lookup.
    client = _cluster_session(row=cluster).client("logs")

    if category not in category_filters and category != "all":
        category = "all"

    # Compile keywords → CW Insights filter clauses. Strip regex meta
    # chars so a user typing "(payment)" doesn't try to backref into the
    # @message regex engine.
    keyword_clauses: list[str] = []
    if keywords:
        for raw in keywords.split():
            cleaned = re.sub(r"[^A-Za-z0-9_./:\-]", "", raw)
            if cleaned:
                keyword_clauses.append(f"@message like /{cleaned}/")

    filter_parts: list[str] = []
    if category != "all" and (category_filters.get(category) or "").strip():
        filter_parts.append(category_filters[category])
    if keyword_clauses:
        filter_parts.append("filter " + " and ".join(keyword_clauses))

    if not filter_parts:
        query_string = (
            "fields @timestamp, @message | sort @timestamp desc | limit 100"
        )
    else:
        # filter_parts already include the `filter` prefix once.
        query_string = (
            "fields @timestamp, @message | "
            + " | ".join(filter_parts)
            + " | sort @timestamp desc | limit 100"
        )

    base_result = {
        "cluster_id": cluster_id,
        "category": category,
        "hours": hours,
        "log_group": log_group,
        # Expose the compiled query + sanitized keywords so the UI can
        # show "we ran this exact CW Insights query for you" — gives
        # DBAs a copy/paste-ready string to refine in the Console.
        "compiled_query": query_string,
        "keywords": " ".join(c.split("/")[1] for c in keyword_clauses)
        if keyword_clauses else "",
        "entries": [],
        "count": 0,
    }

    try:
        resp = client.start_query(
            logGroupName=log_group,
            startTime=int((time.time() - hours * 3600) * 1000),
            endTime=int(time.time() * 1000),
            # CloudWatch Logs Insights does not accept SQL-style comments;
            # the source-tagging convention applies only to SQL queries.
            queryString=query_string,
        )
    except client.exceptions.ResourceNotFoundException:
        engine_hint = (
            "MySQL error/slowquery/general"
            if is_mysql
            else "PostgreSQL"
        )
        return {
            **base_result,
            "error": (
                f"Log group {log_group} not found — enable {engine_hint} "
                "log exports on the cluster (parameter group + Modify "
                "cluster → Logs)."
            ),
        }
    except Exception as e:
        print(f"[search_logs] start_query failed for {cluster_id}: {e}")
        return {
            **base_result,
            "error": "로그 검색을 시작하지 못했습니다. 로그 내보내기 설정과 권한을 확인해주세요.",
        }

    qid = resp["queryId"]
    for _ in range(25):  # ~25s budget — Lambda timeout is 30s
        r = client.get_query_results(queryId=qid)
        status = r.get("status")
        if status == "Complete":
            rows = r.get("results", []) or []
            entries = []
            for row in rows:
                fields = {f["field"]: f["value"] for f in row}
                entries.append(
                    {
                        "ts": fields.get("@timestamp"),
                        "message": fields.get("@message", ""),
                    }
                )
            return {
                **base_result,
                "entries": entries,
                "count": len(entries),
            }
        if status in ("Failed", "Cancelled"):
            return {**base_result, "error": f"query {status.lower()}"}
        time.sleep(1)

    return {**base_result, "error": "query timed out — try a smaller hours window"}


def _topology_docdb(cluster_id: str) -> dict:
    """DocumentDB cluster topology (read-only). DocDB mirrors the RDS cluster
    member API (`docdb describe_db_clusters` → DBClusterMembers, writer +
    readers), so this is the relational topology path with a `docdb` client and
    the AWS/DocDB per-instance replica-lag metric. `cluster_id` IS the DocDB
    DBClusterIdentifier."""
    from datetime import datetime, timedelta

    _sess = _cluster_session(cluster_id)
    docdb = _sess.client("docdb")
    cw = _sess.client("cloudwatch")

    not_real = {
        "cluster_id": cluster_id,
        "engine_family": "documentdb",
        "error": (
            "이 DocumentDB 클러스터의 토폴로지를 조회할 수 없습니다 — 등록되지 "
            "않았거나 접근 권한이 없습니다."
        ),
        "info": True,
        "members": [],
    }
    try:
        resp = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)
    except Exception as e:
        msg = str(e)
        if "DBClusterNotFoundFault" in msg or "not found" in msg.lower():
            return not_real
        print(f"[topology] docdb describe failed for {cluster_id}: {e}")
        return {
            "cluster_id": cluster_id,
            "engine_family": "documentdb",
            "error": "토폴로지 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
            "members": [],
        }
    clusters = resp.get("DBClusters") or []
    if not clusters:
        return not_real
    cluster = clusters[0]
    raw_members = cluster.get("DBClusterMembers") or []

    end = datetime.utcnow()
    start = end - timedelta(minutes=15)

    instance_meta: dict[str, dict] = {}
    instance_ids = [
        m.get("DBInstanceIdentifier") for m in raw_members if m.get("DBInstanceIdentifier")
    ]
    if instance_ids:
        try:
            inst_resp = docdb.describe_db_instances(
                Filters=[{"Name": "db-instance-id", "Values": instance_ids}]
            )
            for inst in inst_resp.get("DBInstances", []):
                instance_meta[inst["DBInstanceIdentifier"]] = inst
        except Exception:
            pass

    members = []
    for m in raw_members:
        instance_id = m.get("DBInstanceIdentifier") or ""
        is_writer = bool(m.get("IsClusterWriter"))
        meta = instance_meta.get(instance_id, {})

        # Per-instance replica lag — DocDB publishes AWS/DocDB DBInstanceReplicaLag
        # (ms) for readers; the writer is 0 by definition.
        lag_ms = 0.0 if is_writer else None
        if not is_writer:
            try:
                lag_resp = cw.get_metric_statistics(
                    Namespace="AWS/DocDB",
                    MetricName="DBInstanceReplicaLag",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
                    StartTime=start,
                    EndTime=end,
                    Period=60,
                    Statistics=["Average"],
                )
                dps = sorted(lag_resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
                if dps:
                    lag_ms = float(dps[-1]["Average"])
            except Exception:
                pass

        members.append({
            "instance_id": instance_id,
            "is_writer": is_writer,
            "promotion_tier": m.get("PromotionTier"),
            "parameter_group_status": m.get("DBClusterParameterGroupStatus", ""),
            "instance_class": meta.get("DBInstanceClass", ""),
            "instance_status": meta.get("DBInstanceStatus", ""),
            "engine_version": meta.get("EngineVersion", ""),
            "availability_zone": meta.get("AvailabilityZone", ""),
            "replica_lag_ms": lag_ms,
        })

    members.sort(
        key=lambda x: (
            0 if x["is_writer"] else 1,
            x.get("promotion_tier") if x.get("promotion_tier") is not None else 99,
            x["instance_id"],
        )
    )

    return {
        "cluster_id": cluster_id,
        "engine_family": "documentdb",
        "engine": cluster.get("Engine", "docdb"),
        "engine_version": cluster.get("EngineVersion", ""),
        "endpoint": cluster.get("Endpoint", ""),
        "reader_endpoint": cluster.get("ReaderEndpoint", ""),
        "multi_az": bool(cluster.get("MultiAZ")),
        "status": cluster.get("Status", ""),
        "members_count": len(members),
        "members": members,
    }


def _topology(cluster_id: str) -> dict:
    """Return Aurora writer + readers with per-instance replica lag.

    Live API call against RDS + CloudWatch (15-min window, latest
    datapoint per instance). The Aurora cluster topology is too slow-
    moving to warrant cache invalidation logic and the dashboard panel
    is opt-in, so we accept the cold-call cost on button click."""
    from datetime import datetime, timedelta

    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "members": []}
    fam = engine_family(eng)
    if fam == "documentdb":
        return _topology_docdb(cluster_id)
    if fam != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": fam, "members": []}

    # Cross-account-aware: target the cluster's own account+region (spoke role
    # when registered; local session otherwise).
    _sess = _cluster_session(cluster_id)
    rds = _sess.client("rds")
    cw = _sess.client("cloudwatch")

    # Same friendly-fallback contract as _backups: never leak the raw boto3
    # fault string. The synthetic demo cluster (and any unregistered id) has
    # no real Aurora to describe.
    not_real = {
        "cluster_id": cluster_id,
        "error": (
            "이 클러스터의 복제 토폴로지를 조회할 수 없습니다 — 데모(합성) "
            "클러스터이거나 실제 Aurora로 등록되지 않았습니다. 등록된 클러스터를 "
            "선택하면 writer/reader 구성과 Replica Lag이 표시됩니다."
        ),
        # info (not error): demo/unregistered cluster — render as a neutral
        # notice, not a red failure box.
        "info": True,
        "members": [],
    }
    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
    except Exception as e:
        msg = str(e)
        if "DBClusterNotFoundFault" in msg or "not found" in msg.lower():
            return not_real
        print(f"[topology] describe failed for {cluster_id}: {e}")
        return {
            "cluster_id": cluster_id,
            "error": "토폴로지 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
            "members": [],
        }

    clusters = resp.get("DBClusters") or []
    if not clusters:
        return not_real
    cluster = clusters[0]
    raw_members = cluster.get("DBClusterMembers") or []

    end = datetime.utcnow()
    start = end - timedelta(minutes=15)

    # Batch one describe_db_instances call to avoid N round-trips for big
    # clusters. RDS returns up to 100 results per call which covers any
    # Aurora cluster (hard cap is 15 readers).
    instance_meta: dict[str, dict] = {}
    instance_ids = [
        m.get("DBInstanceIdentifier") for m in raw_members if m.get("DBInstanceIdentifier")
    ]
    if instance_ids:
        try:
            inst_resp = rds.describe_db_instances(
                Filters=[{"Name": "db-instance-id", "Values": instance_ids}]
            )
            for inst in inst_resp.get("DBInstances", []):
                instance_meta[inst["DBInstanceIdentifier"]] = inst
        except Exception:
            pass

    members = []
    for m in raw_members:
        instance_id = m.get("DBInstanceIdentifier") or ""
        is_writer = bool(m.get("IsClusterWriter"))
        meta = instance_meta.get(instance_id, {})

        # Replica lag — writer is always 0 by definition (it's the
        # source). Readers get the latest 1-min datapoint over the past
        # 15 minutes; None means the metric has never been published
        # (instance still warming up or just promoted).
        lag_ms = 0.0 if is_writer else None
        if not is_writer:
            try:
                lag_resp = cw.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="AuroraReplicaLag",
                    Dimensions=[
                        {"Name": "DBInstanceIdentifier", "Value": instance_id}
                    ],
                    StartTime=start,
                    EndTime=end,
                    Period=60,
                    Statistics=["Average"],
                )
                dps = sorted(
                    lag_resp.get("Datapoints", []),
                    key=lambda d: d["Timestamp"],
                )
                if dps:
                    lag_ms = float(dps[-1]["Average"])
            except Exception:
                pass

        members.append(
            {
                "instance_id": instance_id,
                "is_writer": is_writer,
                "promotion_tier": m.get("PromotionTier"),
                "parameter_group_status": m.get(
                    "DBClusterParameterGroupStatus", ""
                ),
                "instance_class": meta.get("DBInstanceClass", ""),
                "instance_status": meta.get("DBInstanceStatus", ""),
                "engine_version": meta.get("EngineVersion", ""),
                "availability_zone": meta.get("AvailabilityZone", ""),
                "replica_lag_ms": lag_ms,
            }
        )

    # Sort: writer first, then readers by promotion_tier ascending
    # (lower tier = higher failover priority, matches RDS console).
    members.sort(
        key=lambda x: (
            0 if x["is_writer"] else 1,
            x.get("promotion_tier") if x.get("promotion_tier") is not None else 99,
            x["instance_id"],
        )
    )

    return {
        "cluster_id": cluster_id,
        "engine": cluster.get("Engine", ""),
        "engine_version": cluster.get("EngineVersion", ""),
        "endpoint": cluster.get("Endpoint", ""),
        "reader_endpoint": cluster.get("ReaderEndpoint", ""),
        "multi_az": bool(cluster.get("MultiAZ")),
        "status": cluster.get("Status", ""),
        "members_count": len(members),
        "members": members,
    }


def _backups_docdb(cluster_id: str) -> dict:
    """DocumentDB backup inventory (read-only). DocDB mirrors the RDS cluster
    snapshot API, so this is the relational path with a `docdb` client.
    `cluster_id` IS the DocDB DBClusterIdentifier (not a slug)."""
    from datetime import datetime, timezone

    docdb = _cluster_session(cluster_id).client("docdb")

    def _iso(dt):
        if not dt:
            return None
        try:
            return dt.astimezone(timezone.utc).isoformat()
        except (AttributeError, ValueError):
            return str(dt)

    not_real = {
        "cluster_id": cluster_id,
        "engine_family": "documentdb",
        "error": (
            "이 DocumentDB 클러스터의 백업 정보를 조회할 수 없습니다 — 등록되지 "
            "않았거나 접근 권한이 없습니다."
        ),
        "info": True,
        "snapshots": [],
    }
    try:
        cl_resp = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)
    except Exception as e:
        msg = str(e)
        if "DBClusterNotFoundFault" in msg or "not found" in msg.lower():
            return not_real
        print(f"[backups] docdb describe failed for {cluster_id}: {e}")
        return {
            "cluster_id": cluster_id,
            "engine_family": "documentdb",
            "error": "백업 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
            "snapshots": [],
        }
    clusters = cl_resp.get("DBClusters") or []
    if not clusters:
        return not_real
    c = clusters[0]

    earliest = c.get("EarliestRestorableTime")
    latest = c.get("LatestRestorableTime")
    pitr_window_hours = None
    if earliest and latest:
        try:
            pitr_window_hours = round((latest - earliest).total_seconds() / 3600.0, 1)
        except (TypeError, AttributeError):
            pitr_window_hours = None

    snapshots = []
    try:
        snap_resp = docdb.describe_db_cluster_snapshots(
            DBClusterIdentifier=cluster_id, MaxRecords=100,
        )
        for s in snap_resp.get("DBClusterSnapshots", []):
            snapshots.append({
                "id": s.get("DBClusterSnapshotIdentifier"),
                "type": s.get("SnapshotType"),  # manual | automated
                "status": s.get("Status"),
                "created": _iso(s.get("SnapshotCreateTime")),
                "engine_version": s.get("EngineVersion"),
            })
        snapshots.sort(key=lambda x: x.get("created") or "", reverse=True)
    except Exception as e:
        print(f"[backups] docdb snapshot list failed for {cluster_id}: {e}")

    manual = sum(1 for s in snapshots if s.get("type") == "manual")
    return {
        "cluster_id": cluster_id,
        "engine_family": "documentdb",
        "engine": c.get("Engine", "docdb"),
        "status": c.get("Status", ""),
        "backup_retention_days": c.get("BackupRetentionPeriod"),
        "preferred_backup_window": c.get("PreferredBackupWindow"),
        "earliest_restorable_time": _iso(earliest),
        "latest_restorable_time": _iso(latest),
        "pitr_window_hours": pitr_window_hours,
        "snapshot_count": len(snapshots),
        "manual_snapshot_count": manual,
        "snapshots": snapshots,
        "checked_at": int(datetime.now(timezone.utc).timestamp() * 1000),
    }


def _backups_dynamodb(cluster_id: str) -> dict:
    """DynamoDB backup posture (read-only): PITR window + on-demand backups.
    The `ddb-<hex>` slug is the registry PK; the real table name lives in
    the registry row's `resource_name`."""
    from datetime import datetime, timezone

    row = _lookup_cluster(cluster_id)
    table_name = (row.get("resource_name") if row else "") or cluster_id
    ddb = _cluster_session(cluster_id, row=row).client("dynamodb")

    def _iso(dt):
        if not dt:
            return None
        try:
            return dt.astimezone(timezone.utc).isoformat()
        except (AttributeError, ValueError):
            return str(dt)

    result = {
        "cluster_id": cluster_id,
        "engine_family": "dynamodb",
        "table_name": table_name,
        "pitr_enabled": False,
        "earliest_restorable_time": None,
        "latest_restorable_time": None,
        "on_demand_backups": [],
        "on_demand_count": 0,
        # Shared-shape safety: the frontend BackupPanel reads `snapshots` on the
        # common path; DynamoDB has none (PITR + on-demand only).
        "snapshots": [],
        "checked_at": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    try:
        cb = ddb.describe_continuous_backups(TableName=table_name)
        pitr = (cb.get("ContinuousBackupsDescription") or {}).get(
            "PointInTimeRecoveryDescription"
        ) or {}
        result["pitr_enabled"] = pitr.get("PointInTimeRecoveryStatus") == "ENABLED"
        result["earliest_restorable_time"] = _iso(pitr.get("EarliestRestorableDateTime"))
        result["latest_restorable_time"] = _iso(pitr.get("LatestRestorableDateTime"))
    except Exception as e:
        msg = str(e)
        if "ResourceNotFound" in msg or "not found" in msg.lower():
            result["error"] = (
                "이 DynamoDB 테이블의 백업 정보를 조회할 수 없습니다 — 등록되지 "
                "않았거나 접근 권한이 없습니다."
            )
            result["info"] = True
            return result
        print(f"[backups] dynamodb continuous-backups failed for {cluster_id}: {e}")
        result["error"] = "백업 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."

    try:
        lb = ddb.list_backups(TableName=table_name, Limit=100)
        backups = []
        for b in lb.get("BackupSummaries", []):
            backups.append({
                "name": b.get("BackupName"),
                "status": b.get("BackupStatus"),
                "created": _iso(b.get("BackupCreationDateTime")),
                "size_bytes": b.get("BackupSizeBytes"),
                "type": b.get("BackupType"),
            })
        backups.sort(key=lambda x: x.get("created") or "", reverse=True)
        result["on_demand_backups"] = backups
        result["on_demand_count"] = len(backups)
    except Exception as e:
        print(f"[backups] dynamodb list-backups failed for {cluster_id}: {e}")

    return result


def _backups(cluster_id: str) -> dict:
    """Backup inventory + PITR window for one cluster (read-only).

    Live RDS calls (same same-account pattern as _topology):
      - describe_db_clusters     → PITR window + retention + windows
      - describe_db_cluster_snapshots → manual + automated snapshots

    No write actions here — this is the safe read tier of the backup
    workflow. Manual snapshot creation / restore are separate
    approval-gated write tools (a later phase).
    """
    from datetime import datetime, timezone

    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "snapshots": []}
    fam = engine_family(eng)
    if fam == "documentdb":
        return _backups_docdb(cluster_id)
    if fam == "dynamodb":
        return _backups_dynamodb(cluster_id)
    if fam != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": fam, "snapshots": []}

    # Cross-account-aware: describe the cluster in its own account+region.
    rds = _cluster_session(cluster_id).client("rds")

    # Friendly fallback for clusters RDS can't describe — most often the
    # synthetic demo cluster (no real Aurora behind it) or one that isn't
    # registered. Never surface the raw boto3 fault string to the UI.
    not_real = {
        "cluster_id": cluster_id,
        "error": (
            "이 클러스터의 실시간 백업 정보를 조회할 수 없습니다 — 데모(합성) "
            "클러스터이거나 실제 Aurora로 등록되지 않았습니다. 등록된 클러스터를 "
            "선택하면 스냅샷·PITR 윈도우가 표시됩니다."
        ),
        # info (not error): demo/unregistered cluster — render as a neutral notice.
        "info": True,
        "snapshots": [],
    }
    try:
        cl_resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
    except Exception as e:
        msg = str(e)
        if "DBClusterNotFoundFault" in msg or "not found" in msg.lower():
            return not_real
        print(f"[backups] describe failed for {cluster_id}: {e}")
        return {
            "cluster_id": cluster_id,
            "error": "백업 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
            "snapshots": [],
        }
    clusters = cl_resp.get("DBClusters") or []
    if not clusters:
        return not_real
    c = clusters[0]

    def _iso(dt):
        if not dt:
            return None
        try:
            return dt.astimezone(timezone.utc).isoformat()
        except (AttributeError, ValueError):
            return str(dt)

    earliest = c.get("EarliestRestorableTime")
    latest = c.get("LatestRestorableTime")
    pitr_window_hours = None
    if earliest and latest:
        try:
            pitr_window_hours = round(
                (latest - earliest).total_seconds() / 3600.0, 1
            )
        except (TypeError, AttributeError):
            pitr_window_hours = None

    # Snapshot inventory — both manual and automated. The API caps at
    # 100/page; one page covers any realistic cluster snapshot count
    # for the dashboard view.
    snapshots = []
    try:
        snap_resp = rds.describe_db_cluster_snapshots(
            DBClusterIdentifier=cluster_id,
            MaxRecords=100,
        )
        for s in snap_resp.get("DBClusterSnapshots", []):
            snapshots.append({
                "id": s.get("DBClusterSnapshotIdentifier"),
                "type": s.get("SnapshotType"),  # manual | automated
                "status": s.get("Status"),
                "created": _iso(s.get("SnapshotCreateTime")),
                "engine_version": s.get("EngineVersion"),
                "allocated_storage_gb": s.get("AllocatedStorage"),
                # Manual snapshots are the ones a DBA explicitly created
                # (and must clean up); automated ones expire on retention.
            })
        # Newest first.
        snapshots.sort(key=lambda x: x.get("created") or "", reverse=True)
    except Exception as e:
        print(f"[backups] snapshot list failed for {cluster_id}: {e}")

    manual = sum(1 for s in snapshots if s.get("type") == "manual")
    return {
        "cluster_id": cluster_id,
        "engine": c.get("Engine", ""),
        "status": c.get("Status", ""),
        "backup_retention_days": c.get("BackupRetentionPeriod"),
        "preferred_backup_window": c.get("PreferredBackupWindow"),
        "earliest_restorable_time": _iso(earliest),
        "latest_restorable_time": _iso(latest),
        "pitr_window_hours": pitr_window_hours,
        "snapshot_count": len(snapshots),
        "manual_snapshot_count": manual,
        "snapshots": snapshots,
        "checked_at": int(datetime.now(timezone.utc).timestamp() * 1000),
    }


def _endpoints(cluster_id: str) -> dict:
    """Cluster endpoints inventory (read-only): built-in writer/reader + any
    custom endpoints, with type/status/members. Aurora (relational) only.

    Same live-describe + cross-account + friendly-fallback contract as _backups.
    A single DescribeDBClusterEndpoints call returns every endpoint inline, so
    there is no pagination loop.
    """
    from datetime import datetime, timezone

    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True,
                "endpoints": []}
    fam = engine_family(eng)
    if fam != "relational":
        return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": fam, "endpoints": []}

    rds = _cluster_session(cluster_id).client("rds")
    not_real = {
        "cluster_id": cluster_id,
        "error": (
            "이 클러스터의 엔드포인트 정보를 조회할 수 없습니다 — 데모(합성) "
            "클러스터이거나 실제 Aurora로 등록되지 않았습니다."
        ),
        "info": True,
        "endpoints": [],
    }
    try:
        resp = rds.describe_db_cluster_endpoints(DBClusterIdentifier=cluster_id)
    except Exception as e:
        msg = str(e)
        if "DBClusterNotFoundFault" in msg or "not found" in msg.lower():
            return not_real
        print(f"[endpoints] describe failed for {cluster_id}: {e}")
        return {
            "cluster_id": cluster_id,
            "error": "엔드포인트 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
            "endpoints": [],
        }

    endpoints = []
    for ep in resp.get("DBClusterEndpoints", []):
        endpoints.append({
            "identifier": ep.get("DBClusterEndpointIdentifier"),
            "type": ep.get("EndpointType"),            # WRITER | READER | CUSTOM
            "custom_type": ep.get("CustomEndpointType"),  # READER | ANY (custom only)
            "status": ep.get("Status"),
            "endpoint": ep.get("Endpoint"),
            "static_members": ep.get("StaticMembers") or [],
            "excluded_members": ep.get("ExcludedMembers") or [],
        })
    # Built-in writer/reader first, then custom — a stable, readable order.
    _rank = {"WRITER": 0, "READER": 1, "CUSTOM": 2}
    endpoints.sort(key=lambda e: (_rank.get((e.get("type") or "").upper(), 3), e.get("identifier") or ""))
    return {
        "cluster_id": cluster_id,
        "engine": eng,
        "custom_count": sum(1 for e in endpoints if (e.get("type") or "").upper() == "CUSTOM"),
        "endpoints": endpoints,
        "checked_at": int(datetime.now(timezone.utc).timestamp() * 1000),
    }


def _engine_config_docdb(cluster_id: str) -> dict:
    """DocumentDB engine-level config (read-only). Surfaces cluster settings
    the DocDB overview panel does NOT already show (engine/version are shown
    there). Mirrors the friendly-fallback contract of _backups_docdb —
    `cluster_id` IS the DocDB DBClusterIdentifier."""
    docdb = _cluster_session(cluster_id).client("docdb")

    not_real = {
        "cluster_id": cluster_id,
        "engine_family": "documentdb",
        "error": (
            "이 DocumentDB 클러스터의 구성 정보를 조회할 수 없습니다 — 등록되지 "
            "않았거나 접근 권한이 없습니다."
        ),
        "info": True,
    }
    try:
        resp = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)
    except Exception as e:
        msg = str(e)
        if "DBClusterNotFoundFault" in msg or "not found" in msg.lower():
            return not_real
        print(f"[engine-config] docdb describe failed for {cluster_id}: {e}")
        return {
            "cluster_id": cluster_id,
            "engine_family": "documentdb",
            "error": "구성 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
        }
    clusters = resp.get("DBClusters") or []
    if not clusters:
        return not_real
    c = clusters[0]
    return {
        "cluster_id": cluster_id,
        "engine_family": "documentdb",
        "preferred_maintenance_window": c.get("PreferredMaintenanceWindow"),
        "deletion_protection": bool(c.get("DeletionProtection")),
        "storage_encrypted": bool(c.get("StorageEncrypted")),
        "db_cluster_parameter_group": c.get("DBClusterParameterGroup"),
        "backup_retention_period": c.get("BackupRetentionPeriod"),
    }


def _engine_config_dynamodb(cluster_id: str) -> dict:
    """DynamoDB table-level config (read-only). Surfaces config the DynamoDB
    overview panel does NOT already show (billing_mode/item_count/key_schema/
    GSI/LSI are shown there). The `ddb-<hex>` slug is the registry PK; the real
    table name lives in the registry row's `resource_name`."""
    row = _lookup_cluster(cluster_id)
    table_name = (row.get("resource_name") if row else "") or cluster_id
    ddb = _cluster_session(cluster_id, row=row).client("dynamodb")

    result = {
        "cluster_id": cluster_id,
        "engine_family": "dynamodb",
        "table_name": table_name,
        "table_class": None,
        "deletion_protection_enabled": None,
        "sse_type": None,
        "sse_status": None,
        "stream_enabled": None,
        "stream_view_type": None,
        "ttl_status": None,
        "ttl_attribute_name": None,
    }
    try:
        desc = (ddb.describe_table(TableName=table_name) or {}).get("Table") or {}
    except Exception as e:
        msg = str(e)
        if "ResourceNotFound" in msg or "not found" in msg.lower():
            result["error"] = (
                "이 DynamoDB 테이블의 구성 정보를 조회할 수 없습니다 — 등록되지 "
                "않았거나 접근 권한이 없습니다."
            )
            result["info"] = True
            return result
        print(f"[engine-config] dynamodb describe_table failed for {cluster_id}: {e}")
        result["error"] = "구성 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."
        return result

    # STANDARD when the table-class summary is absent (default class).
    result["table_class"] = (desc.get("TableClassSummary") or {}).get(
        "TableClass", "STANDARD"
    )
    result["deletion_protection_enabled"] = bool(desc.get("DeletionProtectionEnabled"))
    sse = desc.get("SSEDescription") or {}
    if sse:
        # SSEType absent → AWS-owned key (no SSEDescription) vs KMS/AES256.
        result["sse_type"] = sse.get("SSEType")
        result["sse_status"] = sse.get("Status")
    stream = desc.get("StreamSpecification") or {}
    result["stream_enabled"] = bool(stream.get("StreamEnabled"))
    result["stream_view_type"] = stream.get("StreamViewType")

    try:
        ttl = (ddb.describe_time_to_live(TableName=table_name) or {}).get(
            "TimeToLiveDescription"
        ) or {}
        result["ttl_status"] = ttl.get("TimeToLiveStatus")
        result["ttl_attribute_name"] = ttl.get("AttributeName")
    except Exception as e:
        print(f"[engine-config] dynamodb describe_time_to_live failed for {cluster_id}: {e}")

    return result


# Operationally meaningful parameter-group settings surfaced in the ElastiCache
# Configuration panel. maxmemory-policy (eviction) + reserved-memory-percent are
# the headline posture knobs; the rest are common Redis/Valkey tuning, plus one
# Memcached field. We surface whichever of these the group actually declares.
_ELASTICACHE_KEY_PARAMS = [
    "maxmemory-policy",
    "reserved-memory-percent",
    "maxmemory-samples",
    "timeout",
    "tcp-keepalive",
    "databases",
    "cluster-enabled",
    "slowlog-log-slower-than",
    "max_item_size",  # Memcached
]


def _ec_not_found(e: Exception) -> bool:
    msg = str(e)
    return "NotFound" in msg or "not found" in msg.lower()


def _engine_config_elasticache(cluster_id: str) -> dict:
    """ElastiCache (Redis/Valkey/Memcached) engine-level config (read-only).

    Surfaces config the ElastiCache overview panel does NOT already show: the
    parameter group + its key parameters (eviction policy etc.), maintenance
    window, snapshot retention/window, encryption (at-rest/in-transit), AUTH,
    automatic failover and Multi-AZ. resource_name is the replication-group id
    (Redis/Valkey) or the cache-cluster id (Memcached/standalone). Maintenance
    window + parameter group live on the cache CLUSTER (node), not the
    replication group, so they're read from a member node. Friendly-fallback
    like the other engine-config helpers — never leak the raw boto3 fault."""
    row = _lookup_cluster(cluster_id)
    resource_name = (row.get("resource_name") if row else "") or cluster_id
    ec = _cluster_session(cluster_id, row=row).client("elasticache")

    result = {
        "cluster_id": cluster_id,
        "engine_family": "elasticache",
        "preferred_maintenance_window": None,
        "snapshot_retention_limit": None,
        "snapshot_window": None,
        "at_rest_encryption_enabled": None,
        "storage_encryption_type": None,
        "transit_encryption_enabled": None,
        "auth_enabled": None,
        "rbac_enabled": None,
        "automatic_failover": None,
        "multi_az": None,
        "parameter_group": None,
        "parameters": {},
    }
    not_found = {
        "cluster_id": cluster_id,
        "engine_family": "elasticache",
        "error": (
            "이 ElastiCache 클러스터의 구성 정보를 조회할 수 없습니다 — 등록되지 "
            "않았거나 접근 권한이 없습니다."
        ),
        "info": True,
    }

    node_id = None  # a member cache-cluster id to read maintenance window + PG from
    # -- Redis/Valkey replication group --
    try:
        rg = (ec.describe_replication_groups(ReplicationGroupId=resource_name)
              .get("ReplicationGroups") or [])
    except Exception as e:
        if _ec_not_found(e):
            rg = []
        else:
            print(f"[engine-config] elasticache describe_replication_groups failed for {cluster_id}: {e}")
            result["error"] = "구성 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."
            return result
    if rg:
        g = rg[0]
        result["snapshot_retention_limit"] = g.get("SnapshotRetentionLimit")
        result["snapshot_window"] = g.get("SnapshotWindow")
        # At-rest: StorageEncryptionType is the authoritative posture — a node can
        # be encrypted (e.g. "sse-elasticache") even when the legacy boolean flag
        # reads false — so treat EITHER signal as encrypted and surface the type.
        enc_type = g.get("StorageEncryptionType")
        result["storage_encryption_type"] = enc_type
        result["at_rest_encryption_enabled"] = bool(g.get("AtRestEncryptionEnabled")) or bool(
            enc_type and str(enc_type).lower() != "none"
        )
        result["transit_encryption_enabled"] = bool(g.get("TransitEncryptionEnabled"))
        # AUTH posture covers BOTH a legacy auth token AND RBAC user groups — a
        # cluster authenticated via RBAC (UserGroupIds) carries no auth token but
        # is NOT open, so don't report it as "disabled".
        result["auth_enabled"] = bool(g.get("AuthTokenEnabled"))
        result["rbac_enabled"] = bool(g.get("UserGroupIds") or [])
        # AutomaticFailover / MultiAZ are status strings (enabled/disabled/...).
        result["automatic_failover"] = g.get("AutomaticFailover")
        result["multi_az"] = g.get("MultiAZ")
        members = g.get("MemberClusters") or []
        node_id = members[0] if members else None
    else:
        # -- Memcached / standalone cache cluster --
        node_id = resource_name

    # Maintenance window + parameter-group NAME live on the cache cluster (node),
    # not the replication group — read them (and fill any unset standalone fields).
    pg_name = None
    if node_id:
        try:
            ccs = (ec.describe_cache_clusters(CacheClusterId=node_id)
                   .get("CacheClusters") or [])
        except Exception as e:
            if not rg and _ec_not_found(e):
                return not_found
            print(f"[engine-config] elasticache describe_cache_clusters failed for {cluster_id}: {e}")
            ccs = []
        if not ccs and not rg:
            return not_found
        if ccs:
            c0 = ccs[0]
            result["preferred_maintenance_window"] = c0.get("PreferredMaintenanceWindow")
            pg_name = (c0.get("CacheParameterGroup") or {}).get("CacheParameterGroupName")
            # Standalone path: the RG fields above were never set — fill from the node.
            if result["snapshot_retention_limit"] is None:
                result["snapshot_retention_limit"] = c0.get("SnapshotRetentionLimit")
            if result["snapshot_window"] is None:
                result["snapshot_window"] = c0.get("SnapshotWindow")
            if result["storage_encryption_type"] is None:
                result["storage_encryption_type"] = c0.get("StorageEncryptionType")
            if result["at_rest_encryption_enabled"] is None:
                enc_type = result["storage_encryption_type"]
                result["at_rest_encryption_enabled"] = bool(c0.get("AtRestEncryptionEnabled")) or bool(
                    enc_type and str(enc_type).lower() != "none"
                )
            if result["transit_encryption_enabled"] is None:
                result["transit_encryption_enabled"] = bool(c0.get("TransitEncryptionEnabled"))
            if result["auth_enabled"] is None:
                result["auth_enabled"] = bool(c0.get("AuthTokenEnabled"))

    result["parameter_group"] = pg_name

    # Key parameters from the parameter group (eviction policy etc.).
    if pg_name:
        try:
            params = {}
            for page in ec.get_paginator("describe_cache_parameters").paginate(
                CacheParameterGroupName=pg_name
            ):
                for p in page.get("Parameters") or []:
                    name = p.get("ParameterName")
                    if name in _ELASTICACHE_KEY_PARAMS:
                        params[name] = p.get("ParameterValue")
            result["parameters"] = params
        except Exception as e:
            print(f"[engine-config] elasticache describe_cache_parameters failed for {cluster_id}: {e}")

    return result


def _engine_config(cluster_id: str) -> dict:
    """Engine-level configuration for non-relational families (read-only).

    Surfaces config NOT already shown in the overview panels — DocumentDB
    cluster settings (maintenance window, deletion protection, encryption,
    parameter group, retention), DynamoDB table settings (table class,
    deletion protection, SSE, streams, TTL), and ElastiCache settings
    (parameter group + key params, maintenance/snapshot windows, encryption,
    AUTH, failover).

    Relational clusters already have the SettingsPanel (cluster_settings),
    so they return not_applicable here. Same friendly-fallback contract as
    _topology / _backups — never leak the raw boto3 fault string.
    """
    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "not_applicable": True, "registry_unavailable": True}
    fam = engine_family(eng)
    if fam == "documentdb":
        return _engine_config_docdb(cluster_id)
    if fam == "dynamodb":
        return _engine_config_dynamodb(cluster_id)
    if fam == "elasticache":
        return _engine_config_elasticache(cluster_id)
    # relational already has the SettingsPanel — nothing engine-config-specific here.
    return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": fam}


def _param_diff(cluster_id: str) -> dict:
    """Surface a relational cluster's LIVE parameter group settings, each
    annotated with whether it differs from the family's DEFAULT parameter
    group. Returns ALL set parameters (`params`) so an operator can browse the
    full config, plus `diffs` (the differing-only subset, kept for callers that
    only want what changed) and `diff_count`.

    Baseline is `default.<family>` fetched via the SAME
    describe_db_cluster_parameters action as the current group, so both sides
    share the identical materialized representation. (The engine-default
    catalog, describe_engine_default_cluster_parameters, returns EMPTY values
    for many params the group reports concretely, which produced pure
    false-positive diffs — do not reintroduce it.)

    Relational only — DocumentDB/DynamoDB/ElastiCache have their own config
    surface via _engine_config. Two fully-paginated cross-account describes
    (current values + the default group), so this is always called through
    _cached_live with a multi-minute TTL — never per-render. Same
    friendly-fallback contract as _topology/_backups: never leak the raw
    boto3 fault string, `available: false` on any failure.
    """
    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "available": False, "registry_unavailable": True, "params": [], "diffs": []}
    fam = engine_family(eng)
    if fam != "relational":
        return {"cluster_id": cluster_id, "available": False, "not_applicable": True,
                "engine_family": fam, "params": [], "diffs": []}

    rds = _cluster_session(cluster_id).client("rds")
    try:
        cl_resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = cl_resp.get("DBClusters") or []
        pg_name = clusters[0].get("DBClusterParameterGroup") if clusters else None
        if not pg_name:
            return {"cluster_id": cluster_id, "available": False, "params": [], "diffs": []}

        fam_resp = rds.describe_db_cluster_parameter_groups(DBClusterParameterGroupName=pg_name)
        groups = fam_resp.get("DBClusterParameterGroups") or []
        family = groups[0].get("DBParameterGroupFamily") if groups else None
        if not family:
            return {"cluster_id": cluster_id, "available": False, "parameter_group": pg_name, "params": [], "diffs": []}

        # Current values — no Source filter (AWS docs: Filters isn't actually
        # honored by this action), so pull the full group and diff in-memory.
        current = []
        marker = None
        while True:
            kwargs = {"DBClusterParameterGroupName": pg_name}
            if marker:
                kwargs["Marker"] = marker
            resp = rds.describe_db_cluster_parameters(**kwargs)
            current.extend(resp.get("Parameters") or [])
            marker = resp.get("Marker")
            # Real boto Markers are non-empty strings; anything else (or a
            # MagicMock in tests) terminates the loop instead of hanging.
            if not isinstance(marker, str) or not marker:
                break

        # Baseline = the family's default cluster parameter group
        # (default.<family>, AWS naming convention). Same describe action as
        # the current group → identical materialized representation.
        default_group = f"default.{family}"
        defaults = {}
        marker = None
        while True:
            kwargs = {"DBClusterParameterGroupName": default_group}
            if marker:
                kwargs["Marker"] = marker
            resp = rds.describe_db_cluster_parameters(**kwargs)
            for p in resp.get("Parameters") or []:
                name = p.get("ParameterName")
                if name:
                    defaults[name] = p.get("ParameterValue", "")
            marker = resp.get("Marker")
            if not isinstance(marker, str) or not marker:
                break

        # Every set parameter (non-empty current value), annotated with whether
        # it differs from the default group. `params` is the full browsable
        # list; `diffs` is the differing-only subset (same shape sans `differs`,
        # kept for backward-compat).
        params = []
        for p in current:
            name = p.get("ParameterName")
            cur_val = p.get("ParameterValue", "")
            if not name or cur_val == "":
                continue  # unset — inherits the default, nothing to surface
            default_val = defaults.get(name, "")
            params.append({
                "name": name,
                "current": cur_val,
                "default": default_val or None,
                "differs": cur_val != default_val,
                "source": p.get("Source", ""),
                "apply_type": (p.get("ApplyType") or "").lower(),
            })
        params.sort(key=lambda d: d["name"])
        diffs = [
            {k: v for k, v in p.items() if k != "differs"}
            for p in params if p["differs"]
        ]

        return {
            "cluster_id": cluster_id,
            "available": True,
            "parameter_group": pg_name,
            "family": family,
            "total_params": len(current),
            "diff_count": len(diffs),
            "params": params,
            "diffs": diffs,
            "checked_at": int(time.time() * 1000),
        }
    except Exception as e:
        print(f"[param-diff] failed for {cluster_id}: {e}")
        return {"cluster_id": cluster_id, "available": False, "params": [], "diffs": []}


def _slo(
    query,
    cluster_id: str,
    days: int,
    availability_target_pct: float,
    latency_target_ms: float,
) -> dict:
    """Compute a minimal-but-honest SLO report from the cache.

    Two SLIs:
      • Availability — % of 1-minute windows in the lookback that had a
        successful metric scrape with positive uptime. We use `uptime_sec`
        because it's collected by every ETL run; absence = ETL outage OR
        cluster unreachable, which is exactly what we want to flag.
      • Latency    — % of 1-minute windows where the average
        `query_stats.mean_time_ms` (across all tracked statements) was
        below the target. This is a coarse proxy — true p95/p99 would
        require per-query histograms.

    Returns a flat shape ready for the UI: targets, actuals, error-budget
    pct (consumed = 1 - actual/target normalised against the allowed
    failure rate), plus a per-day timeline so the page can sparkline the
    burn-down.
    """
    days = max(1, min(int(days), 90))
    expected_minutes = days * 24 * 60

    # Availability: distinct minute buckets that have any uptime_sec sample
    # with a non-zero value in the window.
    avail_sql = (
        "SELECT COUNT(*)::bigint AS ok_minutes FROM ("
        "  SELECT date_trunc('minute', ts) AS minute "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cluster_id "
        "    AND ts > NOW() - (:days || ' days')::interval "
        "    AND metric_type = 'uptime_sec' "
        "    AND value > 0 "
        "  GROUP BY 1"
        ") s"
    )
    avail_row = query(avail_sql, {"cluster_id": cluster_id, "days": str(days)})
    ok_minutes = int((avail_row[0] or {}).get("ok_minutes", 0)) if avail_row else 0
    availability_actual_pct = (
        (ok_minutes / expected_minutes) * 100.0 if expected_minutes else 0.0
    )

    # Latency: per-minute average of mean_time_ms across all tracked
    # queries. Compliance = % of minutes meeting target.
    latency_sql = (
        "WITH per_minute AS ("
        "  SELECT date_trunc('minute', snapshot_time) AS minute, "
        "         AVG(mean_time_ms) AS avg_ms "
        "  FROM query_stats "
        "  WHERE cluster_id = :cluster_id "
        "    AND snapshot_time > NOW() - (:days || ' days')::interval "
        "  GROUP BY 1"
        ") "
        "SELECT "
        "  COUNT(*)::bigint AS total_minutes, "
        "  SUM(CASE WHEN avg_ms <= :target THEN 1 ELSE 0 END)::bigint AS ok_minutes, "
        "  COALESCE(AVG(avg_ms), 0)::float AS overall_avg_ms "
        "FROM per_minute"
    )
    lat_row = query(
        latency_sql,
        {
            "cluster_id": cluster_id,
            "days": str(days),
            "target": float(latency_target_ms),
        },
    )
    lat = (lat_row[0] if lat_row else {}) or {}
    lat_total = int(lat.get("total_minutes") or 0)
    lat_ok = int(lat.get("ok_minutes") or 0)
    latency_compliance_pct = (lat_ok / lat_total) * 100.0 if lat_total else None
    overall_avg_ms = float(lat.get("overall_avg_ms") or 0)

    # Error budget — "how much of the allowed failure rate is consumed?"
    # If actual_ok >= target, consumed is 0%; if actual_ok = target's allowed
    # floor (e.g. 99.9 target, 99.9 actual), consumed = 100%.
    def _budget_consumed(actual: float | None, target: float) -> float | None:
        if actual is None or target >= 100:
            return None
        allowed_fail = 100.0 - target
        actual_fail = max(0.0, 100.0 - actual)
        return min(100.0, (actual_fail / allowed_fail) * 100.0) if allowed_fail else 0.0

    avail_budget_consumed = _budget_consumed(
        availability_actual_pct, availability_target_pct
    )
    lat_budget_consumed = (
        _budget_consumed(latency_compliance_pct, availability_target_pct)
        if latency_compliance_pct is not None
        else None
    )

    # Per-day timeline for sparkline. Bucket size = day to keep payload
    # small even for 90-day windows.
    timeline_sql = (
        "WITH avail AS ("
        "  SELECT date_trunc('day', ts) AS day, "
        "         COUNT(DISTINCT date_trunc('minute', ts)) AS ok_minutes "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cluster_id "
        "    AND ts > NOW() - (:days || ' days')::interval "
        "    AND metric_type = 'uptime_sec' "
        "    AND value > 0 "
        "  GROUP BY 1"
        "), lat AS ("
        "  SELECT date_trunc('day', snapshot_time) AS day, "
        "         AVG(mean_time_ms) AS avg_ms "
        "  FROM query_stats "
        "  WHERE cluster_id = :cluster_id "
        "    AND snapshot_time > NOW() - (:days || ' days')::interval "
        "  GROUP BY 1"
        ") "
        "SELECT to_char(d.day, 'YYYY-MM-DD') AS day, "
        "       COALESCE(a.ok_minutes, 0) AS ok_minutes, "
        "       COALESCE(l.avg_ms, 0) AS avg_ms "
        "FROM ("
        "  SELECT generate_series("
        "    date_trunc('day', NOW() - (:days || ' days')::interval), "
        "    date_trunc('day', NOW()), '1 day') AS day"
        ") d "
        "LEFT JOIN avail a ON a.day = d.day "
        "LEFT JOIN lat l ON l.day = d.day "
        "ORDER BY d.day"
    )
    tl_rows = query(timeline_sql, {"cluster_id": cluster_id, "days": str(days)})
    timeline = []
    minutes_per_day = 24 * 60
    for r in tl_rows or []:
        ok_min = int(r.get("ok_minutes") or 0)
        avg_ms = float(r.get("avg_ms") or 0)
        day_avail_pct = min(100.0, (ok_min / minutes_per_day) * 100.0)
        timeline.append(
            {
                "day": r.get("day"),
                "availability_pct": round(day_avail_pct, 3),
                "avg_latency_ms": round(avg_ms, 2),
                "availability_ok": day_avail_pct >= availability_target_pct,
                "latency_ok": avg_ms > 0 and avg_ms <= latency_target_ms,
                "no_data": ok_min == 0 and avg_ms == 0,
            }
        )

    return {
        "cluster_id": cluster_id,
        "window_days": days,
        "expected_minutes": expected_minutes,
        "availability": {
            "target_pct": availability_target_pct,
            "actual_pct": round(availability_actual_pct, 3),
            "ok_minutes": ok_minutes,
            "budget_consumed_pct": (
                round(avail_budget_consumed, 1) if avail_budget_consumed is not None else None
            ),
            "allowed_downtime_minutes": round(
                expected_minutes * (100.0 - availability_target_pct) / 100.0, 1
            ),
            "actual_downtime_minutes": max(0, expected_minutes - ok_minutes),
        },
        "latency": {
            "target_ms": latency_target_ms,
            "compliance_pct": (
                round(latency_compliance_pct, 3)
                if latency_compliance_pct is not None
                else None
            ),
            "overall_avg_ms": round(overall_avg_ms, 2),
            "budget_consumed_pct": (
                round(lat_budget_consumed, 1)
                if lat_budget_consumed is not None
                else None
            ),
            "samples_minutes": lat_total,
        },
        "timeline": timeline,
    }


def _instances(query, cluster_id):
    """Cluster member list for the Compare instance picker, from
    cluster_meta.instances (populated by the meta collector)."""
    rows = query(
        "SELECT instances::text AS instances FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    if not rows or not rows[0].get("instances"):
        return {"instances": []}
    try:
        return {"instances": json.loads(rows[0]["instances"]) or []}
    except (ValueError, TypeError):
        return {"instances": []}


def _resource_details(query, cluster_id: str) -> dict:
    """Return engine + resource_details JSONB from cluster_meta for the cluster.

    resource_details carries engine-specific topology that is too large / too
    volatile to include in the main _overview response:
      - DynamoDB: billing_mode, item_count, table_size_bytes, gsi (list), table_status
      - DocumentDB: instances (list), instance_count, engine_version
    The column is JSONB in PG; the Data API returns it as a stringValue which we
    parse back to a dict here so the frontend gets a proper object."""
    # cluster_meta has NO engine_family column (that lives on the DDB registry);
    # derive it from `engine` so this SELECT can't fail on a missing column.
    # engine_version is stored as a plain column (not inside resource_details JSONB)
    # by the DocDB collector — SELECT it explicitly so the DocDB panel can render it.
    rows = query(
        "SELECT engine, engine_version, resource_details "
        "FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    if not rows:
        return {
            "cluster_id": cluster_id,
            "engine": None,
            "engine_family": None,
            "resource_details": None,
        }
    row = rows[0]
    rd = row.get("resource_details")
    if isinstance(rd, str):
        try:
            rd = json.loads(rd)
        except (ValueError, TypeError):
            rd = None
    eng = row.get("engine")
    eng_ver = row.get("engine_version")

    # Normalise DocDB resource_details so the panel always gets a consistent shape:
    #   engine_version — merge in from the cluster_meta column when absent from JSONB
    #   instances      — collector stores plain strings; wrap each as {"instance_id": str}
    if rd is not None and engine_family(eng) == "documentdb":
        if isinstance(rd, dict):
            # Merge engine_version from the column if not already present in JSONB
            if eng_ver is not None and "engine_version" not in rd:
                rd = {**rd, "engine_version": eng_ver}
            # Normalise instances list: strings → {"instance_id": str}
            instances = rd.get("instances")
            if isinstance(instances, list):
                normalised = []
                for inst in instances:
                    if isinstance(inst, str):
                        normalised.append({"instance_id": inst})
                    else:
                        normalised.append(inst)
                rd = {**rd, "instances": normalised}

    return {
        "cluster_id": cluster_id,
        "engine": eng,
        "engine_family": engine_family(eng),
        "resource_details": rd,
    }


def lambda_handler(event, context):
    _set_origin(event)
    raw_path_early = event.get("rawPath") or event.get("path") or ""
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    query = _make_query(_rds_data(), cluster_arn, secret_arn, database)

    if raw_path_early.endswith("/api/learning"):
        try:
            return _response(200, _learning_overview(query, event), max_age=30)
        except Exception:
            print(f"Learning overview error: {traceback.format_exc()}")
            return _response(500, {"error": "Internal server error"})

    if raw_path_early.endswith("/multi-cluster/overview"):
        try:
            # Fleet overview is the highest-traffic read in the app
            # (loaded on every sidebar nav). 20s browser cache means
            # rapid tab-switching stops re-hitting the lambda.
            return _response(
                200, _multi_cluster_overview(query, event), max_age=20
            )
        except Exception:
            print(f"Multi-cluster overview error: {traceback.format_exc()}")
            return _response(500, {"error": "Internal server error"})

    path_params = event.get("pathParameters") or {}
    cluster_id = path_params.get("cluster_id")
    if not cluster_id or not CLUSTER_ID_RE.match(cluster_id):
        return _response(400, {"error": "invalid cluster_id"})

    forbid = _require_visible(event, cluster_id)
    if forbid:
        return forbid

    qs = event.get("queryStringParameters") or {}
    raw_path = raw_path_early

    # Absolute window (Dashboard custom time picker). When both are present
    # and parseable, every endpoint that supports an absolute window will use
    # them in preference to the relative `hours` arg. We accept ISO-8601
    # strings (e.g. "2026-05-18T14:00:00Z") — RDS Data API's timestamptz cast
    # tolerates either Z-suffix or "+00:00".
    from_iso = (qs.get("from") or "").strip() or None
    to_iso = (qs.get("to") or "").strip() or None

    try:
        # Custom endpoints panel (P2-⑤) rides the base dashboard route via a
        # ?view=endpoints sub-view param — no new API route to register/regen.
        # Same live-describe throttle cache as topology/backups (25s server TTL).
        if qs.get("view") == "endpoints":
            return _response(
                200,
                _cached_live(f"endpoints:{cluster_id}", 25, lambda: _endpoints(cluster_id)),
                max_age=30,
            )
        if raw_path.endswith("/timeseries"):
            metric_type = qs.get("metric", "aas")
            hours = _parse_int(qs.get("hours"), 1)
            return _response(
                200,
                _timeseries(query, cluster_id, metric_type, hours, from_iso, to_iso),
                # Dashboard panels auto-refresh every ~30s for some
                # users. 15s browser cache halves the lambda QPS for
                # the per-cluster timeseries endpoint without making
                # the chart visibly stale.
                max_age=15,
            )
        if raw_path.endswith("/wait-events"):
            hours = _parse_int(qs.get("hours"), 1)
            return _response(200, _wait_events(query, cluster_id, hours), max_age=30)
        if raw_path.endswith("/slow-queries"):
            hours = _parse_int(qs.get("hours"), 1)
            threshold = _parse_float(qs.get("threshold_ms"), 100.0)
            return _response(200, _slow_queries(query, cluster_id, hours, threshold), max_age=30)
        if raw_path.endswith("/query-detail"):
            qh = qs.get("query_hash")
            if not qh:
                return _response(400, {"error": "query_hash required"})
            return _response(200, _query_detail(query, cluster_id, qh))
        if raw_path.endswith("/workload-diff"):
            before_iso = (qs.get("before") or "").strip()
            after_iso = (qs.get("after") or "").strip()
            if not before_iso or not after_iso:
                return _response(400, {"error": "before and after timestamps required (ISO-8601)"})
            regression_pct = _parse_float(qs.get("regression_pct"), 20.0)
            match_window_min = _parse_int(qs.get("match_window_min"), 120, min_v=10, max_v=1440)
            return _response(
                200,
                _workload_diff(
                    query, cluster_id, before_iso, after_iso,
                    regression_pct, match_window_min,
                ),
                max_age=30,
            )
        if raw_path.endswith("/vacuum-stats"):
            return _response(200, _vacuum_stats(query, cluster_id), max_age=30)
        if raw_path.endswith("/table-sizes"):
            return _response(200, _table_sizes(query, cluster_id), max_age=30)
        if raw_path.endswith("/health-findings"):
            return _response(200, _health_findings(query, cluster_id), max_age=30)
        if raw_path.endswith("/extensions"):
            return _response(200, _extensions(query, cluster_id), max_age=30)
        if raw_path.endswith("/table-indexes"):
            schema = (qs.get("schema") or "").strip()
            table_name = (qs.get("table") or "").strip()
            result = _table_indexes(cluster_id, schema, table_name)
            status = 400 if "error" in result and result.get("error") in ("schema and table required",) else 200
            if "error" in result and status == 200:
                # cluster lookup / execution errors — surface as 502/404.
                status = 404 if "not registered" in str(result.get("error")) else 502
            return _response(status, result)
        if raw_path.endswith("/long-running"):
            return _response(200, _long_running(query, cluster_id), max_age=15)
        if raw_path.endswith("/blocking-locks"):
            return _response(200, _blocking_locks(query, cluster_id), max_age=15)
        if raw_path.endswith("/settings"):
            return _response(200, _cluster_settings(query, cluster_id), max_age=30)
        if raw_path.endswith("/schema-changes"):
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=90)
            return _response(200, _schema_changes(query, cluster_id, days), max_age=30)
        if raw_path.endswith("/timeline"):
            hours = _parse_int(qs.get("hours"), 24, min_v=1, max_v=168)
            categories = (qs.get("categories") or "").split(",")
            categories = [c.strip() for c in categories if c.strip()]
            return _response(
                200,
                _timeline(query, cluster_id, hours, categories or None),
                max_age=30,
            )
        if raw_path.endswith("/anomalies"):
            hours = _parse_int(qs.get("hours"), 4)
            threshold = _parse_float(qs.get("threshold"), 2.5)
            return _response(200, _anomalies(query, cluster_id, hours, threshold), max_age=30)
        if raw_path.endswith("/active-sessions"):
            hours = _parse_int(qs.get("hours"), 1, min_v=1, max_v=24)
            return _response(200, _active_sessions(query, cluster_id, hours), max_age=10)
        if raw_path.endswith("/live-activity"):
            # On-demand LIVE top (P2-⑧): queries the TARGET cluster while the
            # live view is open. Server-side min-interval throttle (1s TTL,
            # keyed per cluster + buffers flag) so N concurrent viewers polling
            # ~2s each still hit the target at most ~1×/s, not N×. No browser
            # cache — every poll must reflect the latest snapshot; the throttle
            # (not HTTP caching) is what bounds DB load.
            buffers = (qs.get("buffers") or "").lower() == "true"
            return _response(
                200,
                _cached_live(
                    f"live-activity:{cluster_id}:{buffers}",
                    1.0,
                    lambda: _live_activity(cluster_id, buffers),
                ),
            )
        if raw_path.endswith("/audit-log"):
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=90)
            action_type = qs.get("action_type")
            return _response(200, _audit_log(query, cluster_id, days, action_type), max_age=30)
        if raw_path.endswith("/change-impact"):
            window_hours = _parse_int(qs.get("window_hours"), 2, min_v=1, max_v=24)
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=30)
            return _response(200, _change_impact(query, cluster_id, window_hours, days), max_age=30)
        if raw_path.endswith("/batch-timeseries"):
            metrics_csv = qs.get("metrics", "")
            metric_names = [m.strip() for m in metrics_csv.split(",") if m.strip()]
            hours = _parse_int(qs.get("hours"), 1)
            offset_hours = _parse_int(qs.get("offset_hours"), 0, min_v=0)
            instance = (qs.get("instance") or "").strip() or None
            return _response(
                200,
                _batch_timeseries(
                    query,
                    cluster_id,
                    metric_names,
                    hours,
                    offset_hours,
                    from_iso,
                    to_iso,
                    instance=instance,
                ),
                max_age=15,
            )
        if raw_path.endswith("/index-recommendations"):
            min_ratio = _parse_float(qs.get("min_seq_ratio"), 0.5)
            return _response(200, _index_recommendations(query, cluster_id, min_ratio), max_age=30)
        if raw_path.endswith("/log-insights"):
            hours = _parse_int(qs.get("hours"), 1)
            category = qs.get("category", "all")
            keywords = (qs.get("q") or qs.get("keywords") or "").strip()
            return _response(
                200,
                _log_insights(cluster_id, hours, category, keywords),
                max_age=30,
            )
        if raw_path.endswith("/capacity-forecast"):
            # LOGICAL metric name, same vocabulary as the forecast_capacity MCP
            # tool (raw metric_type values like storage_bytes are rejected with
            # status=unknown_metric).
            metric = (qs.get("metric") or "storage").strip()
            days_lookback = _parse_int(qs.get("days_lookback"), 30, min_v=7, max_v=90)
            return _response(
                200, _capacity_forecast(query, cluster_id, metric, days_lookback), max_age=30
            )
        if raw_path.endswith("/redundant-indexes"):
            return _response(200, _redundant_indexes(cluster_id), max_age=30)
        if raw_path.endswith("/topology"):
            # Server-side TTL (just under the 30s browser max_age) bounds the
            # cross-account rds:Describe* + cloudwatch:GetMetric* burst this
            # endpoint fires, so many pollers share one live call per window.
            return _response(
                200,
                _cached_live(f"topology:{cluster_id}", 25, lambda: _topology(cluster_id)),
                max_age=30,
            )
        if raw_path.endswith("/backups"):
            # 60s cache — snapshot inventory + PITR window move slowly
            # (automated snapshots are daily, PITR window slides by the
            # minute but minute-granularity staleness is fine here). Server
            # TTL (55s) caps the rds:Describe* rate across concurrent pollers.
            return _response(
                200,
                _cached_live(f"backups:{cluster_id}", 55, lambda: _backups(cluster_id)),
                max_age=60,
            )
        if raw_path.endswith("/engine-config"):
            # 60s cache — engine-level config (maintenance window, deletion
            # protection, SSE, streams, TTL) is near-static; minute-grained
            # staleness is fine for a read-only config panel. Server TTL (55s)
            # caps the cross-account describe rate across concurrent pollers.
            return _response(
                200,
                _cached_live(f"engine-config:{cluster_id}", 55, lambda: _engine_config(cluster_id)),
                max_age=60,
            )
        if raw_path.endswith("/param-diff"):
            # 55s cache — same class as engine-config/backups: the ~500-param
            # engine-default catalog is static per family, and current values
            # only move through the approval-gated modify_parameter tool, so
            # minute-grained staleness is fine. Server TTL (55s) caps the
            # cross-account DescribeDBCluster*Parameters burst across
            # concurrent pollers (two fully-paginated describes per miss).
            return _response(
                200,
                _cached_live(f"param-diff:{cluster_id}", 55, lambda: _param_diff(cluster_id)),
                max_age=60,
            )
        if raw_path.endswith("/slo"):
            days = _parse_int(qs.get("days"), 30, min_v=1, max_v=90)
            avail_t = _parse_float(qs.get("availability_target"), 99.9)
            lat_t = _parse_float(qs.get("latency_target_ms"), 100)
            return _response(200, _slo(query, cluster_id, days, avail_t, lat_t), max_age=30)
        if raw_path.endswith("/schema-graph"):
            schema = (qs.get("schema") or "public").strip() or "public"
            return _response(200, _schema_graph(cluster_id, schema), max_age=30)
        if raw_path.endswith("/resource-details"):
            return _response(200, _resource_details(query, cluster_id), max_age=60)
        if raw_path.endswith("/instances"):
            return _response(200, _instances(query, cluster_id), max_age=30)
        return _response(200, _overview(query, cluster_id), max_age=30)
    except Exception:
        print(f"Dashboard error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"})

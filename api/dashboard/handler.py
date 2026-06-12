import json
import os
import re
import traceback

import boto3
from engine_family import CAPABILITIES, engine_family


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
        return {
            "error": "execution_failed",
            "message": str(e)[:300],
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
        return {"error": "execution_failed", "message": str(e)[:300], "candidates": []}

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
        return {"error": "execution_failed", "message": str(e)[:300]}

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
        cache_hdr["Cache-Control"] = f"private, max-age={int(max_age)}"
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            **cors,
            **cache_hdr,
        },
        "body": json.dumps(body, default=str),
    }


def _overview(query, cluster_id):
    meta = query(
        "SELECT * FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    recent_metrics = query(
        "SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val "
        "FROM metric_snapshots WHERE cluster_id = :cid AND ts > NOW() - INTERVAL '1 hour' "
        "GROUP BY metric_type",
        {"cid": cluster_id},
    )
    top_queries = query(
        "SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms "
        "FROM query_stats WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "ORDER BY total_time_ms DESC LIMIT 10",
        {"cid": cluster_id},
    )
    recent_events = query(
        "SELECT id, event_time as ts, event_type, severity, source, message, raw_event "
        "FROM event_log WHERE cluster_id = :cid "
        "ORDER BY event_time DESC LIMIT 10",
        {"cid": cluster_id},
    )

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
    absolute (from/to) overrides relative (hours)."""
    if from_iso and to_iso:
        rows = query(
            "SELECT ts, value, dimensions::text as dimensions "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "AND metric_type = :mt "
            "AND ts >= :from_ts::timestamptz "
            "AND ts <= :to_ts::timestamptz "
            "ORDER BY ts ASC",
            {"cid": cluster_id, "mt": metric_type, "from_ts": from_iso, "to_ts": to_iso},
        )
    else:
        rows = query(
            "SELECT ts, value, dimensions::text as dimensions "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "AND metric_type = :mt "
            "AND ts > NOW() - (:hours || ' hours')::interval "
            "ORDER BY ts ASC",
            {"cid": cluster_id, "mt": metric_type, "hours": str(hours)},
        )
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "hours": hours,
        "from": from_iso,
        "to": to_iso,
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


def _batch_timeseries(
    query,
    cluster_id,
    metric_names,
    hours,
    offset_hours=0,
    from_iso=None,
    to_iso=None,
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
    }
    if not metric_names:
        return {**base_meta, "series": {}}

    placeholders = ", ".join(f":m{i}" for i in range(len(metric_names)))
    params = {"cid": cluster_id}
    for i, m in enumerate(metric_names):
        params[f"m{i}"] = m

    use_absolute = bool(from_iso) and bool(to_iso)
    if use_absolute:
        params["from_ts"] = from_iso
        params["to_ts"] = to_iso
        sql = (
            f"SELECT ts, metric_type, value, dimensions::text as dimensions "
            f"FROM metric_snapshots "
            f"WHERE cluster_id = :cid "
            f"AND metric_type IN ({placeholders}) "
            f"AND ts >= :from_ts::timestamptz "
            f"AND ts <= :to_ts::timestamptz "
            f"ORDER BY ts ASC"
        )
    else:
        params["hours"] = str(hours)
        params["offset"] = str(offset_hours)
        # Window: (NOW - hours, NOW - offset_hours]. When offset_hours=0 the
        # upper bound collapses to NOW, matching the original "last N hours"
        # semantics.
        sql = (
            f"SELECT ts, metric_type, value, dimensions::text as dimensions "
            f"FROM metric_snapshots "
            f"WHERE cluster_id = :cid "
            f"AND metric_type IN ({placeholders}) "
            f"AND ts > NOW() - (:hours || ' hours')::interval "
            f"AND ts <= NOW() - (:offset || ' hours')::interval "
            f"ORDER BY ts ASC"
        )

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
        kwargs: dict = {"ProjectionExpression": "cluster_id, engine"}
        while True:
            resp = tbl.scan(**kwargs)
            for item in resp.get("Items", []):
                cid = item.get("cluster_id")
                if cid:
                    out[cid] = {"engine": item.get("engine") or ""}
            last = resp.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last
    except Exception as e:
        print(f"[dashboard] registered cluster scan failed: {e}")
        return None


def _multi_cluster_overview(query):
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


def _anomalies(query, cluster_id, hours, threshold):
    """Seasonal anomaly detection.

    For each metric we have a per-hour-of-week baseline (median + IQR) in
    `metric_baselines`. Robust z-score = (recent_max - median) / IQR
    (1.349×IQR ≈ 1 stddev for a normal distribution, but the IQR doesn't
    blow up on outliers, so the score is stable on a cluster that has a
    handful of legitimate spikes per day).

    Falls back to the legacy flat-mean+stddev baseline when no seasonal
    baseline exists for the current bucket (cold-start: less than ~14 days
    of history). The fallback rows are tagged `mode='flat'` so the UI can
    explain why a finding's confidence is lower."""
    rows = query(
        "WITH "
        "current_hour AS ( "
        "  SELECT (EXTRACT(DOW FROM NOW())::int * 24 + EXTRACT(HOUR FROM NOW())::int) AS how "
        "), "
        "recent AS ( "
        "  SELECT metric_type, MAX(value) AS recent_max, AVG(value) AS recent_avg "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cid "
        "    AND ts > NOW() - (:hours || ' hours')::interval "
        "    AND (dimensions IS NULL OR dimensions::text = '{}') "
        "  GROUP BY metric_type "
        "), "
        "seasonal AS ( "
        "  SELECT b.metric_type, b.median, b.iqr, b.sample_count "
        "  FROM metric_baselines b, current_hour c "
        "  WHERE b.cluster_id = :cid AND b.hour_of_week = c.how "
        "), "
        "flat AS ( "
        "  SELECT metric_type, AVG(value) AS mean, STDDEV(value) AS stddev "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cid "
        "    AND ts BETWEEN NOW() - INTERVAL '7 days' AND NOW() - (:hours || ' hours')::interval "
        "    AND (dimensions IS NULL OR dimensions::text = '{}') "
        "  GROUP BY metric_type "
        "  HAVING STDDEV(value) > 0 AND COUNT(*) > 50 "
        ") "
        "SELECT "
        "  r.metric_type, "
        "  r.recent_max, "
        "  r.recent_avg, "
        "  COALESCE(s.median, f.mean) AS baseline_mean, "
        "  COALESCE(s.iqr, f.stddev) AS baseline_stddev, "
        "  CASE WHEN s.iqr IS NOT NULL "
        "    THEN (r.recent_max - s.median) / NULLIF(s.iqr, 0) "
        "    ELSE (r.recent_max - f.mean) / NULLIF(f.stddev, 0) "
        "  END AS z_score, "
        "  CASE WHEN s.iqr IS NOT NULL THEN 'seasonal' ELSE 'flat' END AS mode, "
        "  s.sample_count "
        "FROM recent r "
        "LEFT JOIN seasonal s ON s.metric_type = r.metric_type "
        "LEFT JOIN flat     f ON f.metric_type = r.metric_type "
        "WHERE (s.iqr IS NOT NULL OR f.stddev IS NOT NULL) "
        "  AND ABS( "
        "    CASE WHEN s.iqr IS NOT NULL "
        "      THEN (r.recent_max - s.median) / NULLIF(s.iqr, 0) "
        "      ELSE (r.recent_max - f.mean) / NULLIF(f.stddev, 0) "
        "    END "
        "  ) >= :threshold "
        "ORDER BY 6 DESC "  # ABS of z_score column
        "LIMIT 20",
        {"cid": cluster_id, "hours": str(hours), "threshold": float(threshold)},
    )
    return {"cluster_id": cluster_id, "hours": hours, "threshold": threshold, "anomalies": rows}


def _schema_changes(query, cluster_id, days):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, snapshot_time "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        "), "
        "baseline AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, snapshot_time "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid "
        "  AND snapshot_time < NOW() - (:days || ' days')::interval "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT "
        "  COALESCE(l.schema_name, b.schema_name) AS schema_name, "
        "  COALESCE(l.table_name, b.table_name) AS table_name, "
        "  b.n_live_tup AS baseline_rows, "
        "  l.n_live_tup AS current_rows, "
        "  CASE "
        "    WHEN b.table_name IS NULL THEN 'created' "
        "    WHEN l.table_name IS NULL THEN 'dropped' "
        "    ELSE 'changed' "
        "  END AS change_type, "
        "  b.snapshot_time AS baseline_time, "
        "  l.snapshot_time AS current_time "
        "FROM latest l "
        "FULL OUTER JOIN baseline b "
        "  ON l.schema_name = b.schema_name AND l.table_name = b.table_name "
        "WHERE b.table_name IS NULL "
        "   OR l.table_name IS NULL "
        "   OR (b.n_live_tup IS NOT NULL AND l.n_live_tup IS NOT NULL "
        "       AND ABS(l.n_live_tup - b.n_live_tup) > GREATEST(b.n_live_tup * 0.5, 1000)) "
        "ORDER BY change_type, schema_name, table_name "
        "LIMIT 50",
        {"cid": cluster_id, "days": str(days)},
    )
    return {"cluster_id": cluster_id, "days": days, "changes": rows}


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


def _health_findings(query, cluster_id):
    """Return the *latest* snapshot of maintenance health findings for this
    cluster. Older snapshots stay in the table for trend analysis but the
    dashboard panel only ever shows the most recent one.

    Gating is capability-driven: any engine family whose CAPABILITIES["findings"]
    set is non-empty gets findings returned. This lets relational (Aurora) AND
    dynamodb both surface their respective findings, while documentdb (empty set)
    still returns an empty response.

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
        # Family has no findings capability (e.g. documentdb) — return empty.
        return {
            "cluster_id": cluster_id,
            "snapshot_time": None,
            "counts": {"critical": 0, "warning": 0, "info": 0},
            "findings": [],
        }
    rows = query(
        "WITH latest AS ("
        "  SELECT MAX(snapshot_time) AS ts FROM cluster_health_findings WHERE cluster_id = :cid"
        ") "
        "SELECT id, check_type, severity, subject, value_str, threshold_str, recommendation, details, snapshot_time "
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
    snapshot_time = rows[0]["snapshot_time"] if rows else None
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


# Capacity forecasting: simple linear regression on the last N days of
# metric_snapshots. The Performance MCP server has a `forecast_capacity`
# tool already, but invoking it through the agent for a dashboard panel is
# heavy + slow. We replicate the math directly against the cache DB so the
# panel renders in one round trip.
#
# Limits are deliberately conservative defaults — Aurora autoscales
# storage (cluster cap is 128 TB) so the storage value is a "well past
# any sane operator" ceiling. Connections / AAS limits come from typical
# saturation points for the popular instance classes; when cluster_settings
# carries the actual max_connections we'll override below.
_CAPACITY_METRICS = {
    # metric_type, display unit, hard cap (in stored units)
    "storage_bytes": {"limit": 128 * 1024**4, "label": "Storage"},  # 128 TiB
    # Canonical total-connections metric = db_connections (CloudWatch
    # DatabaseConnections), collected for every cluster. The PI-only
    # "connections" was empty whenever Performance Insights was off.
    "db_connections": {"limit": 5000, "label": "Connections"},
    "aas": {"limit": 64.0, "label": "Active Sessions"},
    # DynamoDB provisioned throughput — consumed_* are per-minute Sums; the
    # ceiling is the provisioned per-second rate × 60. Default limits below are
    # placeholders only — _capacity_forecast OVERRIDES them from the latest
    # provisioned_rcu/provisioned_wcu datapoint (and returns not_applicable when
    # there is none, i.e. on-demand tables).
    "consumed_rcu": {"limit": 60000.0, "label": "Read Capacity (RCU/min)"},
    "consumed_wcu": {"limit": 60000.0, "label": "Write Capacity (WCU/min)"},
}

# Which metrics are valid per engine family. Relational keeps the full
# (storage/connections/aas) set; the new engines forecast only the things that
# "grow toward a limit" — DocDB connections + storage, DynamoDB provisioned
# throughput. A metric outside its family's set → not_applicable (no SQL).
_CAPACITY_METRICS_BY_FAMILY = {
    "relational": {"storage_bytes", "db_connections", "aas"},
    "documentdb": {"db_connections", "storage_bytes"},
    "dynamodb": {"consumed_rcu", "consumed_wcu"},
}


def _capacity_forecast(query, cluster_id, metric, days_lookback):
    eng = _registry_engine(cluster_id)
    if eng is None:
        return {"cluster_id": cluster_id, "metric": metric, "not_applicable": True,
                "registry_unavailable": True}
    fam = engine_family(eng)
    # Engine-aware dispatch: relational, documentdb and dynamodb all forecast
    # against metric_snapshots — only the valid metric set + limit resolution
    # differ. Any other family is genuinely not applicable.
    allowed = _CAPACITY_METRICS_BY_FAMILY.get(fam)
    if allowed is None:
        return {"cluster_id": cluster_id, "metric": metric, "not_applicable": True, "engine_family": fam}
    # Metric must be both globally known AND valid for this engine family.
    if metric not in _CAPACITY_METRICS or metric not in allowed:
        return {"cluster_id": cluster_id, "metric": metric, "not_applicable": True,
                "engine_family": fam}
    # RDS Data API params come through as strings — we cast to interval the
    # same way the other lookback queries in this file do, instead of using
    # MAKE_INTERVAL which would need an integer-typed param. Float-cast
    # value to keep REGR_SLOPE happy when the metric is stored as integer.
    rows = query(
        "SELECT REGR_SLOPE(value::float, EXTRACT(EPOCH FROM ts) / 86400) AS slope, "
        "       (array_agg(value ORDER BY ts DESC))[1]                 AS latest, "
        "       MIN(ts)                                                 AS first_ts, "
        "       MAX(ts)                                                 AS last_ts, "
        "       COUNT(*)                                                AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :mt "
        "AND ts > NOW() - (:days || ' days')::interval",
        {"cid": cluster_id, "mt": metric, "days": str(days_lookback)},
    )
    row = rows[0] if rows else {}
    slope = float(row.get("slope") or 0)
    current = float(row.get("latest") or 0)
    samples = int(row.get("samples") or 0)
    spec = _CAPACITY_METRICS[metric]
    limit = float(spec["limit"])

    # --- Dynamic limit resolution (engine-aware) ---------------------------
    # Relational: connection limit comes from cluster_settings.max_connections
    # when present; otherwise the conservative default ceiling stands.
    if fam == "relational" and metric == "db_connections":
        cfg = query(
            "SELECT value FROM cluster_settings "
            "WHERE cluster_id = :cid AND name = 'max_connections' "
            "ORDER BY updated_at DESC LIMIT 1",
            {"cid": cluster_id},
        )
        try:
            mc = int(cfg[0]["value"]) if cfg else 0
            if mc > 0:
                limit = float(mc)
        except (ValueError, KeyError, TypeError):
            pass

    # DocumentDB: connection ceiling is the LATEST DatabaseConnectionsLimit
    # metric (db_connections_limit), NOT cluster_settings — DocDB has no
    # max_connections setting. storage_bytes keeps the Aurora-style ceiling
    # (DocDB storage auto-scales the same way), so no override there.
    if fam == "documentdb" and metric == "db_connections":
        lim_rows = query(
            "SELECT value FROM metric_snapshots "
            "WHERE cluster_id = :cid AND metric_type = 'db_connections_limit' "
            "AND (dimensions IS NULL OR dimensions::text = '{}') "
            "ORDER BY ts DESC LIMIT 1",
            {"cid": cluster_id},
        )
        try:
            cl = float(lim_rows[0]["value"]) if lim_rows else 0.0
            if cl > 0:
                limit = cl
        except (ValueError, KeyError, TypeError):
            pass

    # DynamoDB: per-minute capacity ceiling = LATEST provisioned_* × 60
    # (consumed_* are per-minute Sums). No provisioned datapoint means the
    # table is on-demand (or capacity unknown) — there is no limit to forecast
    # toward, so we return not_applicable rather than a misleading default.
    if fam == "dynamodb" and metric in ("consumed_rcu", "consumed_wcu"):
        prov_metric = "provisioned_rcu" if metric == "consumed_rcu" else "provisioned_wcu"
        prov_rows = query(
            "SELECT value FROM metric_snapshots "
            "WHERE cluster_id = :cid AND metric_type = :pm "
            "AND (dimensions IS NULL OR dimensions::text = '{}') "
            "ORDER BY ts DESC LIMIT 1",
            {"cid": cluster_id, "pm": prov_metric},
        )
        provisioned = 0.0
        try:
            provisioned = float(prov_rows[0]["value"]) if prov_rows else 0.0
        except (ValueError, KeyError, TypeError):
            provisioned = 0.0
        if provisioned <= 0:
            return {
                "cluster_id": cluster_id,
                "metric": metric,
                "not_applicable": True,
                "engine_family": fam,
                "reason": "on-demand or no provisioned capacity",
            }
        limit = provisioned * 60.0

    days_until = None
    if slope > 0 and current < limit:
        days_until = max(0, int((limit - current) / slope))
    forecast = "growing" if slope > 0.01 else "shrinking" if slope < -0.01 else "stable"

    return {
        "cluster_id": cluster_id,
        "metric": metric,
        "engine_family": fam,
        "label": spec["label"],
        "current": current,
        "slope_per_day": slope,
        "limit": limit,
        "days_until_limit": days_until,
        "forecast": forecast,
        "samples": samples,
        "days_lookback": days_lookback,
        "projections": {
            "d30": current + slope * 30,
            "d60": current + slope * 60,
            "d90": current + slope * 90,
        },
    }


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


def _engine_config(cluster_id: str) -> dict:
    """Engine-level configuration for non-relational families (read-only).

    Surfaces config NOT already shown in the overview panels — DocumentDB
    cluster settings (maintenance window, deletion protection, encryption,
    parameter group, retention) and DynamoDB table settings (table class,
    deletion protection, SSE, streams, TTL).

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
    # relational already has the SettingsPanel — nothing engine-config-specific here.
    return {"cluster_id": cluster_id, "not_applicable": True, "engine_family": fam}


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

    if raw_path_early.endswith("/multi-cluster/overview"):
        try:
            # Fleet overview is the highest-traffic read in the app
            # (loaded on every sidebar nav). 20s browser cache means
            # rapid tab-switching stops re-hitting the lambda.
            return _response(
                200, _multi_cluster_overview(query), max_age=20
            )
        except Exception:
            print(f"Multi-cluster overview error: {traceback.format_exc()}")
            return _response(500, {"error": "Internal server error"})

    path_params = event.get("pathParameters") or {}
    cluster_id = path_params.get("cluster_id")
    if not cluster_id or not CLUSTER_ID_RE.match(cluster_id):
        return _response(400, {"error": "invalid cluster_id"})

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
            return _response(200, _wait_events(query, cluster_id, hours))
        if raw_path.endswith("/slow-queries"):
            hours = _parse_int(qs.get("hours"), 1)
            threshold = _parse_float(qs.get("threshold_ms"), 100.0)
            return _response(200, _slow_queries(query, cluster_id, hours, threshold))
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
            return _response(200, _vacuum_stats(query, cluster_id))
        if raw_path.endswith("/table-sizes"):
            return _response(200, _table_sizes(query, cluster_id))
        if raw_path.endswith("/health-findings"):
            return _response(200, _health_findings(query, cluster_id))
        if raw_path.endswith("/extensions"):
            return _response(200, _extensions(query, cluster_id))
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
            return _response(200, _long_running(query, cluster_id))
        if raw_path.endswith("/blocking-locks"):
            return _response(200, _blocking_locks(query, cluster_id))
        if raw_path.endswith("/settings"):
            return _response(200, _cluster_settings(query, cluster_id))
        if raw_path.endswith("/schema-changes"):
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=90)
            return _response(200, _schema_changes(query, cluster_id, days))
        if raw_path.endswith("/timeline"):
            hours = _parse_int(qs.get("hours"), 24, min_v=1, max_v=168)
            categories = (qs.get("categories") or "").split(",")
            categories = [c.strip() for c in categories if c.strip()]
            return _response(
                200,
                _timeline(query, cluster_id, hours, categories or None),
            )
        if raw_path.endswith("/anomalies"):
            hours = _parse_int(qs.get("hours"), 4)
            threshold = _parse_float(qs.get("threshold"), 2.5)
            return _response(200, _anomalies(query, cluster_id, hours, threshold))
        if raw_path.endswith("/audit-log"):
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=90)
            action_type = qs.get("action_type")
            return _response(200, _audit_log(query, cluster_id, days, action_type))
        if raw_path.endswith("/change-impact"):
            window_hours = _parse_int(qs.get("window_hours"), 2, min_v=1, max_v=24)
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=30)
            return _response(200, _change_impact(query, cluster_id, window_hours, days), max_age=30)
        if raw_path.endswith("/batch-timeseries"):
            metrics_csv = qs.get("metrics", "")
            metric_names = [m.strip() for m in metrics_csv.split(",") if m.strip()]
            hours = _parse_int(qs.get("hours"), 1)
            offset_hours = _parse_int(qs.get("offset_hours"), 0, min_v=0)
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
                ),
                max_age=15,
            )
        if raw_path.endswith("/index-recommendations"):
            min_ratio = _parse_float(qs.get("min_seq_ratio"), 0.5)
            return _response(200, _index_recommendations(query, cluster_id, min_ratio))
        if raw_path.endswith("/log-insights"):
            hours = _parse_int(qs.get("hours"), 1)
            category = qs.get("category", "all")
            keywords = (qs.get("q") or qs.get("keywords") or "").strip()
            return _response(
                200,
                _log_insights(cluster_id, hours, category, keywords),
            )
        if raw_path.endswith("/capacity-forecast"):
            metric = (qs.get("metric") or "storage_bytes").strip()
            days_lookback = _parse_int(qs.get("days_lookback"), 30, min_v=7, max_v=90)
            return _response(
                200, _capacity_forecast(query, cluster_id, metric, days_lookback)
            )
        if raw_path.endswith("/redundant-indexes"):
            return _response(200, _redundant_indexes(cluster_id))
        if raw_path.endswith("/topology"):
            return _response(200, _topology(cluster_id))
        if raw_path.endswith("/backups"):
            # 60s cache — snapshot inventory + PITR window move slowly
            # (automated snapshots are daily, PITR window slides by the
            # minute but minute-granularity staleness is fine here).
            return _response(200, _backups(cluster_id), max_age=60)
        if raw_path.endswith("/engine-config"):
            # 60s cache — engine-level config (maintenance window, deletion
            # protection, SSE, streams, TTL) is near-static; minute-grained
            # staleness is fine for a read-only config panel.
            return _response(200, _engine_config(cluster_id), max_age=60)
        if raw_path.endswith("/slo"):
            days = _parse_int(qs.get("days"), 30, min_v=1, max_v=90)
            avail_t = _parse_float(qs.get("availability_target"), 99.9)
            lat_t = _parse_float(qs.get("latency_target_ms"), 100)
            return _response(200, _slo(query, cluster_id, days, avail_t, lat_t))
        if raw_path.endswith("/schema-graph"):
            schema = (qs.get("schema") or "public").strip() or "public"
            return _response(200, _schema_graph(cluster_id, schema))
        if raw_path.endswith("/resource-details"):
            return _response(200, _resource_details(query, cluster_id), max_age=60)
        return _response(200, _overview(query, cluster_id))
    except Exception:
        print(f"Dashboard error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"})

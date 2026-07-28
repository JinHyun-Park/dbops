"""DocumentDB Mongo-protocol deep-diagnosis collector (in-VPC, read-only).

CloudWatch can't see DocumentDB internals: live operations (currentOp) and
server status (connections/opcounters/mem). This Lambda connects over the Mongo
wire protocol (TLS 27017) with a least-privilege read-only user and emits
metric_snapshots rows + cluster_health_findings into the cache DB.

Slow ops come from the CONTROL PLANE, not from this Mongo connection. Managed
Amazon DocumentDB implements neither the `profile` command nor `system.*`
collections, so the profiler level cannot be read and slow ops cannot be listed
over the wire protocol. The profiler is a CUSTOM cluster-parameter-group feature
(`profiler`, `profiler_threshold_ms`, `profiler_sampling_rate`) whose output is
exported to CloudWatch Logs at /aws/docdb/{cluster_id}/profiler; the
approval-gated `set_docdb_profiler` tool turns it on. E1-6 ingests that log
group here (logs:FilterLogEvents + rds:DescribeDBClusterParameters) because
DocumentDB already has exactly TWO findings writers with disjoint check_type
sets (this Lambda and etl_collector/collectors/docdb_findings.py) and the
dashboard/agent freshness window is derived from that fact: a third
independently scheduled writer would force the window wider.

The profiler branch is deliberately NOT behind the `mongo_secret_arn` gate: it
needs no Mongo credentials, and the product's own DocumentDB registration path
never writes that field, so gating it there would leave the feature dark on
every cluster registered through the UI.

Design (docs/superpowers/specs/2026-06-12-docdb-mongo-deep-diagnosis-design.md):
  - Separate from the ETL collector: that Lambda is NOT in a VPC and packages
    only boto3; this one is in-VPC and bundles pymongo + the RDS/DocDB CA.
  - Per-cluster creds come from a Secrets Manager secret whose ARN is on the
    registry row (`mongo_secret_arn`). No secret → skip the MONGO half only;
    the profiler-log half still runs.
  - READ-ONLY allowlist only — hardcoded commands, NO generic runCommand/eval.
  - Hard fail-safe: any connect/command error logs + skips that cluster and
    NEVER raises, so one bad cluster can't break the whole run.

pymongo is imported lazily inside _connect_mongo (NOT at module top) so the
unit tests can patch _CLIENT_FACTORY and run without pymongo installed.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import boto3

# TLS CA bundle for DocumentDB, vendored into the asset during CDK bundling
# (downloaded from truststore.pki.rds.amazonaws.com) or committed as a fallback.
# Resolved relative to this file so the path is valid in the deployed package.
_CA_BUNDLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global-bundle.pem")

# Long-running-op threshold (seconds). Active ops at/above this are counted.
LONG_RUNNING_SECS = 10
LONG_RUNNING_WARNING = 1   # ≥1 long op → warning
LONG_RUNNING_CRITICAL = 5  # ≥5 long ops → critical

# Mongo server-selection timeout — fail fast on an unreachable cluster.
SERVER_SELECTION_TIMEOUT_MS = 5000

# --- CloudWatch profiler-log ingestion (E1-6) ------------------------------
# Slow-op counts: same thresholds the deleted system.profile version used
# (git e5f2c81), so the revived finding keeps its severity contract.
SLOW_OPS_WARNING = 1   # ≥1 profiled slow op in the window → warning
SLOW_OPS_CRITICAL = 5  # ≥5 → critical

# Fallback cadence (minutes) when COLLECTOR_INTERVAL_MIN is unset or garbage.
# data_stack wires the env var from the SAME value that builds this Lambda's
# EventBridge rate, so the window below can never drift from the schedule.
DEFAULT_INTERVAL_MIN = 5

# CloudWatch Logs delivery is best-effort: AWS documents "typically 1-2 minutes"
# for a profiled op to appear (profiling.html / the profiler blog post). Every
# read window is shifted this far into the past so an event that took the
# documented lag to arrive is still inside the window we read, with 1 minute of
# headroom over the documented upper bound.
PROFILER_DELIVERY_LAG_MIN = 3

# Bounded read: page size and the hard cap on parsed events per cluster per run.
# Hitting the cap makes the finding's count a FLOOR, which the finding says.
PROFILER_PAGE_LIMIT = 1000
PROFILER_EVENT_CAP = 5000

# AWS-documented defaults for the two profiler knobs, used when the parameter
# row carries no explicit ParameterValue (which means "engine default").
DOC_DEFAULT_THRESHOLD_MS = 100
DOC_DEFAULT_SAMPLING_RATE = 1.0

# How many filter/pipeline keys go into an op-shape label before truncation.
SHAPE_KEY_MAX = 6
# Rows written to query_stats per cluster per run, worst offenders first.
SHAPES_PERSISTED = 20
# Namespaces listed in the slow-ops finding.
TOP_NAMESPACES = 5


def _client_factory(host, port, username, password):
    """Default MongoClient factory. Imports pymongo lazily so the module can be
    loaded (and unit-tested) without pymongo installed. Tests patch this with a
    fake-client factory via the module-level _CLIENT_FACTORY hook below."""
    import pymongo  # lazy: not importable in the test env

    return pymongo.MongoClient(
        host=host,
        port=int(port),
        username=username,
        password=password,
        tls=True,
        tlsCAFile=_CA_BUNDLE_PATH,
        retryWrites=False,
        readPreference="secondaryPreferred",
        serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
        connectTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
    )


# Indirection so tests can inject a fake client without importing pymongo.
_CLIENT_FACTORY = _client_factory


def _scan_all(table):
    """Paginated DynamoDB scan — never truncate at the 1MB page boundary."""
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _make_cache_execute(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name):
    """RDS-Data execute helper for the cache DB — mirrors etl_collector.handler."""

    def cache_execute(sql, params):
        sql_params = []
        for key, value in params.items():
            if value is None:
                sql_params.append({"name": key, "value": {"isNull": True}})
            elif isinstance(value, bool):
                sql_params.append({"name": key, "value": {"booleanValue": value}})
            elif isinstance(value, int):
                sql_params.append({"name": key, "value": {"longValue": value}})
            elif isinstance(value, float):
                sql_params.append({"name": key, "value": {"doubleValue": value}})
            else:
                sql_params.append({"name": key, "value": {"stringValue": str(value)}})
        rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql=f"/* source=dbops-docdbmongo */ {sql}", parameters=sql_params,
        )

    return cache_execute


def _insert_metric(cache_execute, cluster_id, ts, metric_type, value):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type, "value": float(value)},
    )


# query_stats holds CUMULATIVE per-query counters everywhere else in this
# product: pg_stat_statements (stats_collector), performance-schema digests
# (mysql_query_stats) and dm_exec_query_stats (mssql_query_stats) are all
# counters that only grow, and every reader is written against that contract
# (api/dashboard _slow_queries / _query_detail / _workload_diff, api/alerts,
# report_generator, and the LAG-delta engine-neutral query_regression collector).
# Profiler log events are individual ops, so this statement ADDS the window's
# totals to the newest existing row for the same (cluster_id, query_hash),
# keeping the counters monotonic and the table's contract intact. mean_time_ms is
# therefore the lifetime mean, exactly like the other three writers.
# The LEFT JOIN over a one-row source is what makes the first-ever window insert
# (no previous row) still produce a row instead of selecting nothing.
_QUERY_STATS_ACCUMULATE = (
    "INSERT INTO query_stats "
    "(cluster_id, snapshot_time, query_hash, query_text, calls, total_time_ms, "
    " mean_time_ms, rows_returned) "
    "SELECT :cluster_id, :ts::timestamptz, :query_hash, :query_text, "
    "       COALESCE(p.calls, 0) + :calls, "
    "       COALESCE(p.total_time_ms, 0) + :total_time_ms, "
    "       (COALESCE(p.total_time_ms, 0) + :total_time_ms) "
    "         / NULLIF(COALESCE(p.calls, 0) + :calls, 0), "
    "       COALESCE(p.rows_returned, 0) + :rows_returned "
    "FROM (SELECT 1) AS one "
    "LEFT JOIN ("
    "  SELECT calls, total_time_ms, rows_returned FROM query_stats "
    "  WHERE cluster_id = :cluster_id AND query_hash = :query_hash "
    "  ORDER BY snapshot_time DESC LIMIT 1"
    ") p ON TRUE"
)


def _insert_query_stats(cache_execute, cluster_id, ts, shape):
    cache_execute(_QUERY_STATS_ACCUMULATE, {
        "cluster_id": cluster_id,
        "ts": ts,
        "query_hash": shape["query_hash"],
        "query_text": shape["query_text"],
        "calls": int(shape["calls"]),
        "total_time_ms": float(shape["total_time_ms"]),
        "rows_returned": int(shape["rows_returned"]),
    })


def _insert_finding(cache_execute, cluster_id, ts, finding):
    cache_execute(
        "INSERT INTO cluster_health_findings "
        "(cluster_id, snapshot_time, check_type, severity, subject, "
        "value_str, threshold_str, recommendation, details) "
        "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
        ":value_str, :threshold_str, :recommendation, :details::jsonb)",
        {
            "cluster_id": cluster_id,
            "ts": ts,
            "check_type": finding["check_type"],
            "severity": finding["severity"],
            "subject": finding["subject"],
            "value_str": finding["value_str"],
            "threshold_str": finding["threshold_str"],
            "recommendation": finding["recommendation"],
            "details": json.dumps(finding["details"]),
        },
    )


# ---------------------------------------------------------------------------
# Read-only command allowlist. Each helper issues ONE hardcoded command via the
# pymongo command interface — there is NO generic runCommand/eval surface.
# ---------------------------------------------------------------------------


def _run_server_status(client):
    """`serverStatus` — connections, opcounters, mem (read-only diagnostic)."""
    return client.admin.command("serverStatus")


def _run_current_op(client):
    """`currentOp` for active ops only (read-only diagnostic)."""
    return client.admin.command("currentOp", **{"$ownOps": False, "active": True})


# NOTE: there is deliberately NO profiling helper here. `profile: -1` and
# <db>.system.profile are MongoDB-only; on managed DocumentDB the first is not a
# supported command and the second is not a supported collection, so the old
# branch could only ever log an error every 5 minutes per cluster. See the module
# docstring for the mechanism that actually works (parameter group + CloudWatch
# Logs export, driven by the set_docdb_profiler tool).


def _emit_server_status_metrics(cache_execute, cluster_id, ts, status):
    """Map serverStatus fields → mongo_* metric_snapshots rows."""
    conns = status.get("connections", {}) or {}
    opc = status.get("opcounters", {}) or {}
    mem = status.get("mem", {}) or {}

    def put(metric_type, value):
        if value is None:
            return
        try:
            _insert_metric(cache_execute, cluster_id, ts, metric_type, float(value))
        except (TypeError, ValueError):
            return

    put("mongo_connections_current", conns.get("current"))
    put("mongo_connections_available", conns.get("available"))
    put("mongo_opcounters_query", opc.get("query"))
    put("mongo_opcounters_insert", opc.get("insert"))
    put("mongo_opcounters_update", opc.get("update"))
    put("mongo_opcounters_delete", opc.get("delete"))
    put("mongo_mem_resident_mb", mem.get("resident"))


def _build_long_running_finding(current_op):
    """currentOp(active) → docdb_mongo_long_running_ops finding (or None)."""
    ops = current_op.get("inprog", []) or []
    long_ops = []
    for op in ops:
        secs = op.get("secs_running")
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            continue
        if secs >= LONG_RUNNING_SECS:
            long_ops.append(op)

    count = len(long_ops)
    if count < LONG_RUNNING_WARNING:
        return None

    max_secs = max(float(op.get("secs_running") or 0) for op in long_ops)
    severity = "critical" if count >= LONG_RUNNING_CRITICAL else "warning"
    namespaces = []
    for op in long_ops:
        ns = op.get("ns")
        if ns and ns not in namespaces:
            namespaces.append(ns)
    return {
        "check_type": "docdb_mongo_long_running_ops",
        "severity": severity,
        "subject": "DocumentDB Long-Running Operations",
        "value_str": f"{count}건 (최대 {int(max_secs)}s)",
        "threshold_str": (
            f"active op ≥ {LONG_RUNNING_SECS}s 가 {LONG_RUNNING_CRITICAL}건 이상"
            if severity == "critical"
            else f"active op ≥ {LONG_RUNNING_SECS}s 가 {LONG_RUNNING_WARNING}건 이상"
        ),
        "recommendation": (
            f"{LONG_RUNNING_SECS}초 이상 실행 중인 active operation이 {count}건 있습니다 "
            f"(최대 {int(max_secs)}초). 인덱스 누락/풀스캔/락 경합을 의심하고, 해당 "
            "네임스페이스의 쿼리 플랜과 인덱스를 점검하세요."
        ),
        "details": {
            "long_running_count": count,
            "max_secs_running": round(max_secs, 1),
            "threshold_secs": LONG_RUNNING_SECS,
            "namespaces": namespaces[:10],
        },
    }


# ---------------------------------------------------------------------------
# Profiler log ingestion: control plane only, no Mongo connection involved.
# ---------------------------------------------------------------------------


def _session_for(region, role_arn=""):
    """A boto3 Session for a registered cluster's account+region.

    Verbatim copy of etl_collector.handler._session_for (no shared layer spans
    the data-pipeline Lambdas). The Mongo half of this collector reaches the
    cluster over the network and needs no role, but the profiler half calls the
    DocumentDB control plane and CloudWatch Logs IN THE CLUSTER'S OWN ACCOUNT,
    so a spoke row without the role chain would read the hub account instead and
    silently find nothing."""
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName="dbops-docdbmongo",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def profiler_log_group(cluster_id):
    """CloudWatch Logs group the profiler delivers to, PER CLUSTER.

    Same name mcp_servers/operations/tools/set_docdb_profiler.profiler_log_group
    builds and tells the operator to query, so ingestion and the tool's advice
    can never point at different groups."""
    return f"/aws/docdb/{cluster_id}/profiler"


def _interval_min():
    """This Lambda's schedule in minutes, wired from data_stack (the same value
    that builds the EventBridge rate). Garbage or unset falls back to the rate
    data_stack ships, so the window can only match or over-read, never gap."""
    try:
        value = int(os.environ.get("COLLECTOR_INTERVAL_MIN", ""))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MIN
    return value if value > 0 else DEFAULT_INTERVAL_MIN


def profiler_window_ms(now_ms, interval_min):
    """The (start, end) epoch-ms window to read, aligned to the interval grid and
    shifted back by the delivery lag.

    Grid alignment is what makes consecutive runs read ADJACENT windows: with a
    5-minute rate, a run at 10:02 reads [09:52, 09:57] and the next at 10:07
    reads [09:57, 10:02]. No overlap means the cumulative query_stats counters
    stay truthful (an overlapping window would inflate `calls` by the overlap
    factor); no gap means a slow op is not silently dropped.

    ponytail: a MISSED run loses exactly its own window, because the window is
    derived from the clock and not from a stored watermark. Upgrade path if that
    matters: persist the last window end per cluster and start from it."""
    step = int(interval_min) * 60_000
    end = (int(now_ms) // step) * step - PROFILER_DELIVERY_LAG_MIN * 60_000
    return end - step, end


def _profiler_params(docdb, pg_name):
    """The three profiler parameters from a cluster parameter group.

    Paginated: `Marker` is only followed when it is a real string, so a bare
    MagicMock in a test cannot spin this loop forever."""
    wanted = ("profiler", "profiler_threshold_ms", "profiler_sampling_rate")
    found = {}
    marker = None
    while True:
        kwargs = {"DBClusterParameterGroupName": pg_name, "MaxRecords": 100}
        if isinstance(marker, str) and marker:
            kwargs["Marker"] = marker
        resp = docdb.describe_db_cluster_parameters(**kwargs)
        for param in resp.get("Parameters") or []:
            name = param.get("ParameterName")
            if name in wanted:
                found[name] = (param.get("ParameterValue") or "").strip()
        marker = resp.get("Marker")
        if len(found) == len(wanted) or not isinstance(marker, str) or not marker:
            return found


def read_profiler_state(docdb, cluster_id):
    """("on" | "off" | "unknown", details) read from the CONTROL PLANE.

    "off" means the operator is BLIND: profiled slow ops are not reaching
    CloudWatch Logs. Three independent ways for that to be true, all of which
    `set_docdb_profiler` fixes, and all of which look identical to a healthy
    cluster if you only look at an empty log group:
      - the cluster parameter group has profiler != enabled (AWS default is
        `disabled`, and a parameter row with no ParameterValue means exactly
        that default),
      - the cluster does not export the `profiler` log type (docs: without that
        step "profiling logs will not be sent to CloudWatch Logs"),
      - profiler_sampling_rate is 0.0, i.e. zero percent of slow ops are logged.

    "unknown" is returned when the control-plane read itself failed. It emits NO
    finding on purpose: claiming "profiler off" on a failed lookup would nag an
    operator whose profiler is on. NEVER raises."""
    try:
        resp = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
        exports = cluster.get("EnabledCloudwatchLogsExports") or []
        pg_name = (cluster.get("DBClusterParameterGroup") or "").strip()
        if not pg_name:
            return "unknown", {"reason": "no_parameter_group"}
        params = _profiler_params(docdb, pg_name)
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} profiler state lookup failed: {e}")
        return "unknown", {"reason": "lookup_failed"}

    log_export = "profiler" in exports
    param_value = (params.get("profiler") or "").lower() or "disabled"
    try:
        threshold_ms = int(params.get("profiler_threshold_ms") or DOC_DEFAULT_THRESHOLD_MS)
    except (TypeError, ValueError):
        threshold_ms = DOC_DEFAULT_THRESHOLD_MS
    try:
        sampling_rate = float(params.get("profiler_sampling_rate") or DOC_DEFAULT_SAMPLING_RATE)
    except (TypeError, ValueError):
        sampling_rate = DOC_DEFAULT_SAMPLING_RATE

    details = {
        "parameter_group": pg_name,
        "profiler_param": param_value,
        "log_export": log_export,
        "threshold_ms": threshold_ms,
        "sampling_rate": sampling_rate,
    }
    blind = param_value != "enabled" or not log_export or sampling_rate <= 0.0
    return ("off" if blind else "on"), details


def _write_finding(cache_execute, cluster_id, run_ts, finding):
    """Insert one finding, returning 1 on success and 0 on a cache-write failure.
    Keeps collect_profiler's "NEVER raises" contract true: a Data API hiccup on
    one write must not cost the rest of the run."""
    try:
        _insert_finding(cache_execute, cluster_id, run_ts, finding)
        return 1
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} finding write failed "
              f"({finding.get('check_type')}): {e}")
        return 0


def _error_code(exc):
    """The bounded AWS error CODE for an exception (never its message)."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code")
        if isinstance(code, str) and code:
            return code
    return type(exc).__name__


def _op_shape(entry):
    """(query_hash, query_text) identifying the SHAPE of a profiled op.

    Identity is op + namespace + the command's filter keys (or its aggregation
    stage names), never the values, so one shape aggregates across parameter
    values. planSummary is deliberately EXCLUDED: a COLLSCAN/IXSCAN flip is the
    regression we want to see ON the same query_hash, and folding it into the
    identity would hide the flip behind a brand-new row instead."""
    op = str(entry.get("op") or "?")
    ns = str(entry.get("ns") or "?")
    command = entry.get("command")
    command = command if isinstance(command, dict) else {}
    filt = command.get("filter")
    pipeline = command.get("pipeline")
    if isinstance(filt, dict):
        parts = sorted(k for k in filt if isinstance(k, str))
    elif isinstance(pipeline, list):
        parts = [
            stage_key
            for stage in pipeline if isinstance(stage, dict)
            for stage_key in sorted(k for k in stage if isinstance(k, str))
        ]
    else:
        parts = []
    text = f"{op} {ns}"
    if parts:
        text += " {" + ", ".join(parts[:SHAPE_KEY_MAX]) + "}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32], text


def parse_profiler_entry(message):
    """One CloudWatch profiler log event → a normalized record, or None.

    Field names are the ones AWS documents for the profiler entry itself (op, ts,
    ns, command, nreturned, millis, planSummary, client, user; see
    docs.aws.amazon.com/documentdb/latest/devguide/performance-slow-queries.html
    and the Logs-Insights examples in profiling.html which filter on ns/millis/
    op/planSummary). Anything that is not a JSON object carrying a numeric
    `millis` is skipped rather than guessed at."""
    if not isinstance(message, str):
        return None
    try:
        entry = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    try:
        millis = float(entry.get("millis"))
    except (TypeError, ValueError):
        return None
    try:
        nreturned = int(entry.get("nreturned") or 0)
    except (TypeError, ValueError):
        nreturned = 0
    query_hash, query_text = _op_shape(entry)
    return {
        "query_hash": query_hash,
        "query_text": query_text,
        "ns": str(entry.get("ns") or "?"),
        "op": str(entry.get("op") or "?"),
        "millis": millis,
        "nreturned": nreturned,
        "plan_summary": str(entry.get("planSummary") or ""),
    }


def fetch_profiler_events(logs_client, cluster_id, start_ms, end_ms):
    """(records, status) for one window. status is "ok", "no_log_group" or
    "read_failed": bounded markers, never exception text.

    "no_log_group" is NOT an error: AWS says the group can take up to an hour to
    appear after the profiler is enabled, and it never appears at all until the
    first op crosses the threshold. It is reported separately from "ok" so an
    empty result can never be presented as a measured "no slow ops".

    Paginated on nextToken, which is only followed when it is a real string (a
    bare MagicMock would otherwise loop forever), and capped."""
    group = profiler_log_group(cluster_id)
    records = []
    token = None
    while True:
        kwargs = {
            "logGroupName": group,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": PROFILER_PAGE_LIMIT,
        }
        if isinstance(token, str) and token:
            kwargs["nextToken"] = token
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except Exception as e:
            if _error_code(e) == "ResourceNotFoundException":
                return [], "no_log_group"
            print(f"[docdb_mongo] {cluster_id} profiler log read failed: {e}")
            return records, "read_failed"
        for event in resp.get("events") or []:
            record = parse_profiler_entry(event.get("message"))
            if record is not None:
                records.append(record)
        token = resp.get("nextToken")
        if len(records) >= PROFILER_EVENT_CAP or not isinstance(token, str) or not token:
            return records[:PROFILER_EVENT_CAP], "ok"


def aggregate_profiler_records(records):
    """Group parsed records by op shape, worst total time first. The per-shape
    totals are this WINDOW's totals; _insert_query_stats turns them into the
    cumulative counters query_stats holds."""
    shapes = {}
    for record in records:
        shape = shapes.get(record["query_hash"])
        if shape is None:
            shape = shapes[record["query_hash"]] = {
                "query_hash": record["query_hash"],
                "query_text": record["query_text"],
                "ns": record["ns"],
                "calls": 0,
                "total_time_ms": 0.0,
                "max_millis": 0.0,
                "rows_returned": 0,
                "plan_summaries": [],
            }
        shape["calls"] += 1
        shape["total_time_ms"] += record["millis"]
        shape["max_millis"] = max(shape["max_millis"], record["millis"])
        shape["rows_returned"] += record["nreturned"]
        plan = record["plan_summary"]
        if plan and plan not in shape["plan_summaries"]:
            shape["plan_summaries"].append(plan)
    return sorted(shapes.values(), key=lambda s: s["total_time_ms"], reverse=True)


def build_slow_ops_finding(shapes, window_minutes, state_details, truncated=False):
    """Aggregated shapes → docdb_mongo_slow_ops (or None when the window was
    genuinely clean, which is the good-news case)."""
    count = sum(shape["calls"] for shape in shapes)
    if count < SLOW_OPS_WARNING:
        return None
    max_millis = max(shape["max_millis"] for shape in shapes)
    threshold_ms = state_details.get("threshold_ms", DOC_DEFAULT_THRESHOLD_MS)
    sampling_rate = state_details.get("sampling_rate", DOC_DEFAULT_SAMPLING_RATE)

    ns_counts = {}
    for shape in shapes:
        ns_counts[shape["ns"]] = ns_counts.get(shape["ns"], 0) + shape["calls"]
    top = sorted(ns_counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_NAMESPACES]
    top_str = ", ".join(f"{ns} ({n})" for ns, n in top)
    collscan = sorted({
        shape["query_text"] for shape in shapes if "COLLSCAN" in shape["plan_summaries"]
    })

    # Every qualifier below exists because the count is NOT always the true
    # number of slow ops: sampling logs only a fraction of them, and the read cap
    # truncates a very busy window. Saying "N건" flat in either case would
    # under-report a problem.
    qualifiers = []
    if sampling_rate < 1.0:
        qualifiers.append(
            f"profiler_sampling_rate={sampling_rate}이므로 기록된 건수는 표본이며 "
            "실제 발생량은 더 많습니다"
        )
    if truncated:
        qualifiers.append(
            f"이번 창에서 {PROFILER_EVENT_CAP}건 상한에 도달해 집계가 잘렸습니다 "
            "(실제 건수는 더 많습니다)"
        )
    if collscan:
        qualifiers.append(
            "COLLSCAN(전체 스캔) 플랜으로 실행된 op: " + ", ".join(collscan[:3])
        )

    recommendation = (
        f"최근 {window_minutes}분간 profiler_threshold_ms({threshold_ms}ms)를 넘긴 op이 "
        f"{count}건 기록됐습니다 (최대 {int(max_millis)}ms). 상위 네임스페이스: {top_str}. "
        "해당 컬렉션의 인덱스와 쿼리 패턴을 점검하세요"
    )
    if qualifiers:
        recommendation += ". " + ". ".join(qualifiers)
    recommendation += "."

    return {
        "check_type": "docdb_mongo_slow_ops",
        "severity": "critical" if count >= SLOW_OPS_CRITICAL else "warning",
        "subject": "DocumentDB Slow Operations",
        "value_str": f"{count}건 (최대 {int(max_millis)}ms)",
        "threshold_str": f"profiler millis ≥ {threshold_ms}ms (최근 {window_minutes}분)",
        "recommendation": recommendation,
        "details": {
            "slow_op_count": count,
            "max_millis": round(max_millis, 1),
            "threshold_ms": threshold_ms,
            "sampling_rate": sampling_rate,
            "window_minutes": window_minutes,
            "truncated": bool(truncated),
            "log_group": None,  # filled by the caller (needs cluster_id)
            "top_namespaces": [{"ns": ns, "count": n} for ns, n in top],
            "collscan_shapes": collscan[:TOP_NAMESPACES],
        },
    }


def build_profiler_off_finding(state_details):
    """docdb_mongo_profiler_off: the operator is blind, and the fix is the
    approval-gated `set_docdb_profiler` tool (NOT db.setProfilingLevel, which
    managed DocumentDB does not implement).

    An empty log group with the profiler ON renders as nothing at all (no slow
    ops is good news). This finding is the OTHER case, and says which of the
    three prerequisites is actually missing instead of a generic 'profiler off'."""
    param_value = state_details.get("profiler_param", "disabled")
    log_export = bool(state_details.get("log_export"))
    sampling_rate = state_details.get("sampling_rate", DOC_DEFAULT_SAMPLING_RATE)

    if param_value != "enabled":
        value_str = f"프로파일러 OFF (profiler={param_value})"
        cause = (
            f"클러스터 파라미터 그룹 '{state_details.get('parameter_group', '?')}'의 "
            f"profiler 파라미터가 '{param_value}'입니다"
        )
    elif not log_export:
        value_str = "profiler=enabled, CloudWatch Logs 내보내기 OFF"
        cause = (
            "profiler 파라미터는 켜져 있지만 클러스터가 profiler 로그를 CloudWatch Logs로 "
            "내보내지 않습니다. 이 단계가 빠지면 프로파일러 출력이 어디에도 전달되지 않습니다"
        )
    else:
        value_str = f"profiler=enabled, sampling_rate={sampling_rate}"
        cause = (
            f"profiler_sampling_rate가 {sampling_rate}이라 느린 op이 한 건도 기록되지 "
            "않습니다"
        )

    return {
        "check_type": "docdb_mongo_profiler_off",
        "severity": "info",
        "subject": "DocumentDB Profiler Disabled",
        "value_str": value_str,
        "threshold_str": "느린 op 가시성: profiler=enabled + profiler 로그 내보내기 + sampling_rate > 0",
        "recommendation": (
            f"{cause}. 느린 op을 추적할 수 없는 상태이므로, 이 클러스터의 슬로우 쿼리 "
            "지표가 비어 있는 것은 '느린 쿼리가 없다'는 뜻이 아닙니다. 채팅에서 "
            "set_docdb_profiler 도구로 프로파일러를 켜세요 (승인이 필요하며, 파라미터 "
            "그룹과 로그 내보내기를 함께 설정합니다). DocumentDB는 db.setProfilingLevel()을 "
            "지원하지 않습니다."
        ),
        "details": dict(state_details),
    }


def collect_profiler(session, cache_execute, cluster_id, run_ts, now_ms=None):
    """Control-plane profiler pass for one cluster: read the state, ingest the
    log window into query_stats, emit findings. NEVER raises."""
    result = {"findings_emitted": 0}
    try:
        docdb = session.client("docdb")
        logs_client = session.client("logs")
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} profiler client init failed: {e}")
        result["profiler"] = "unknown"
        return result

    state, details = read_profiler_state(docdb, cluster_id)
    result["profiler"] = state
    if state == "unknown":
        # No finding: see read_profiler_state. The failure is in the log only.
        return result
    if state == "off":
        result["findings_emitted"] = _write_finding(
            cache_execute, cluster_id, run_ts, build_profiler_off_finding(details))
        return result

    interval_min = _interval_min()
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms, end_ms = profiler_window_ms(now_ms, interval_min)
    records, status = fetch_profiler_events(logs_client, cluster_id, start_ms, end_ms)
    result["log_read"] = status
    result["window_minutes"] = interval_min
    result["events"] = len(records)
    if status != "ok":
        # Missing group / failed read: nothing was MEASURED, so nothing is
        # claimed. The profiler-on state is already in `result`.
        return result

    shapes = aggregate_profiler_records(records)
    persisted = 0
    for shape in shapes[:SHAPES_PERSISTED]:
        try:
            _insert_query_stats(cache_execute, cluster_id, run_ts, shape)
            persisted += 1
        except Exception as e:
            print(f"[docdb_mongo] {cluster_id} query_stats write failed: {e}")
    result["query_stats_rows"] = persisted

    finding = build_slow_ops_finding(
        shapes, interval_min, details, truncated=len(records) >= PROFILER_EVENT_CAP,
    )
    if finding:
        finding["details"]["log_group"] = profiler_log_group(cluster_id)
        result["findings_emitted"] = _write_finding(
            cache_execute, cluster_id, run_ts, finding)
    return result


def _diagnose_cluster(client, cache_execute, cluster_id, run_ts):
    """Run the read-only allowlist against one connected cluster and write
    metrics + findings. Returns a small result dict. Individual command
    failures are isolated so a partial diagnosis still lands."""
    findings_emitted = 0

    # --- serverStatus → metrics ---
    try:
        status = _run_server_status(client)
        _emit_server_status_metrics(cache_execute, cluster_id, run_ts, status)
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} serverStatus failed: {e}")

    # --- currentOp → long-running-ops finding ---
    try:
        current_op = _run_current_op(client)
        finding = _build_long_running_finding(current_op)
        if finding:
            _insert_finding(cache_execute, cluster_id, run_ts, finding)
            findings_emitted += 1
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} currentOp failed: {e}")

    # No profiling branch HERE: slow ops come from the CloudWatch profiler log
    # export (collect_profiler), which needs no Mongo connection and therefore
    # runs outside this function.
    return {"cluster_id": cluster_id, "findings_emitted": findings_emitted}


def _process_cluster(resource, secrets, cache_execute, run_ts):
    """Connect to one DocumentDB cluster and diagnose it. NEVER raises — any
    failure logs and returns a skip marker so other clusters still run."""
    cluster_id = resource.get("cluster_id", "?")
    secret_arn = resource.get("mongo_secret_arn")
    if not secret_arn:
        return {"cluster_id": cluster_id, "skipped": "no mongo_secret_arn"}

    client = None
    try:
        raw = secrets.get_secret_value(SecretId=secret_arn).get("SecretString") or "{}"
        creds = json.loads(raw)
        host = creds.get("host")
        port = creds.get("port", 27017)
        username = creds.get("username")
        password = creds.get("password")
        if not host or not username or not password:
            return {"cluster_id": cluster_id, "skipped": "incomplete mongo creds"}

        client = _CLIENT_FACTORY(host, port, username, password)
        return _diagnose_cluster(client, cache_execute, cluster_id, run_ts)
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} connect/diagnose failed: {e}")
        return {"cluster_id": cluster_id, "error": str(e)}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    clusters_table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    rds_data = boto3.client("rds-data")
    secrets = boto3.client("secretsmanager")

    cache_cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    cache_secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    cache_db_name = os.environ.get("CACHE_DB_NAME", "dbops")

    cache_execute = _make_cache_execute(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name)

    # Single run_ts shared by every finding this cycle so the dashboard's
    # MAX(snapshot_time) query returns them together (same contract as ETL).
    run_ts = datetime.now(timezone.utc).isoformat()

    results = []
    for resource in _scan_all(clusters_table):
        family = resource.get("engine_family") or ""
        if family != "documentdb":
            continue
        cluster_id = resource.get("cluster_id", "?")
        result = {"cluster_id": cluster_id}

        # Profiler pass FIRST and unconditionally: it is control-plane only, so a
        # cluster with no Mongo credentials (which is every cluster registered
        # through the product UI) still gets slow-op ingestion and the
        # profiler-off guidance.
        try:
            session = _session_for(resource.get("region", ""), resource.get("spoke_role_arn", ""))
            result.update(collect_profiler(session, cache_execute, cluster_id, run_ts))
        except Exception as e:
            print(f"[docdb_mongo] {cluster_id} profiler pass failed: {e}")
            result["profiler"] = "unknown"

        if resource.get("mongo_secret_arn"):
            mongo = _process_cluster(resource, secrets, cache_execute, run_ts)
            result["findings_emitted"] = (
                result.get("findings_emitted", 0) + mongo.pop("findings_emitted", 0)
            )
            mongo.pop("cluster_id", None)
            result.update(mongo)
        else:
            result["mongo"] = "skipped: no mongo_secret_arn"
        results.append(result)

    return {"statusCode": 200, "body": json.dumps({"processed": len(results), "results": results}, default=str)}

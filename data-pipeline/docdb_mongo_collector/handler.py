"""DocumentDB Mongo-protocol deep-diagnosis collector (in-VPC, read-only).

CloudWatch can't see DocumentDB internals — live operations (currentOp),
server status (connections/opcounters/mem), and slow-op profiling
(getProfilingStatus + system.profile). This Lambda connects over the Mongo
wire protocol (TLS 27017) with a least-privilege read-only user and emits
metric_snapshots rows + cluster_health_findings into the cache DB.

Design (docs/superpowers/specs/2026-06-12-docdb-mongo-deep-diagnosis-design.md):
  - Separate from the ETL collector: that Lambda is NOT in a VPC and packages
    only boto3; this one is in-VPC and bundles pymongo + the RDS/DocDB CA.
  - Per-cluster creds come from a Secrets Manager secret whose ARN is on the
    registry row (`mongo_secret_arn`). No secret → skip that cluster (no-op).
  - READ-ONLY allowlist only — hardcoded commands, NO generic runCommand/eval.
  - Hard fail-safe: any connect/command error logs + skips that cluster and
    NEVER raises, so one bad cluster can't break the whole run.

pymongo is imported lazily inside _connect_mongo (NOT at module top) so the
unit tests can patch _CLIENT_FACTORY and run without pymongo installed.
"""

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

# Slow-op profile read window (minutes) — only recent system.profile entries.
SLOW_OPS_WINDOW_MIN = 15
# Cap on system.profile docs scanned per cluster (read-only, bounded).
SLOW_OPS_LIMIT = 200

# Mongo server-selection timeout — fail fast on an unreachable cluster.
SERVER_SELECTION_TIMEOUT_MS = 5000


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


def _run_profiling_status(client, db_name):
    """`profile: -1` returns the current profiling level + slowms without changing it."""
    return client[db_name].command("profile", -1)


def _read_system_profile(client, db_name, slowms, since_dt):
    """Read recent slow ops from <db>.system.profile (read-only find)."""
    coll = client[db_name]["system.profile"]
    cursor = coll.find(
        {"millis": {"$gte": int(slowms)}, "ts": {"$gte": since_dt}}
    ).sort("ts", -1).limit(SLOW_OPS_LIMIT)
    return list(cursor)


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


def _build_profiling_findings(profiling_status, slow_samples, slowms):
    """getProfilingStatus → either docdb_mongo_slow_ops (profiler on, slow ops
    found) or docdb_mongo_profiler_off (profiler off, info). Returns a list
    (possibly empty)."""
    level = profiling_status.get("was", profiling_status.get("level", 0))
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 0

    if level <= 0:
        return [
            {
                "check_type": "docdb_mongo_profiler_off",
                "severity": "info",
                "subject": "DocumentDB Profiler Disabled",
                "value_str": "프로파일러 OFF (level 0)",
                "threshold_str": "느린 쿼리 가시성을 위해 profiler level 1 권장",
                "recommendation": (
                    "데이터베이스 프로파일러가 꺼져 있어 느린 쿼리를 추적할 수 없습니다. "
                    "`db.setProfilingLevel(1, { slowms: 100 })`로 slowms 임계값을 넘는 op만 "
                    "기록하도록 활성화하면 system.profile에서 느린 쿼리를 진단할 수 있습니다."
                ),
                "details": {"profiling_level": level},
            }
        ]

    count = len(slow_samples)
    if count == 0:
        return []

    ns_counts = {}
    max_millis = 0
    for doc in slow_samples:
        ns = doc.get("ns") or "?"
        ns_counts[ns] = ns_counts.get(ns, 0) + 1
        try:
            max_millis = max(max_millis, int(doc.get("millis") or 0))
        except (TypeError, ValueError):
            pass
    top = sorted(ns_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_str = ", ".join(f"{ns} ({n})" for ns, n in top)
    severity = "critical" if count >= LONG_RUNNING_CRITICAL else "warning"
    return [
        {
            "check_type": "docdb_mongo_slow_ops",
            "severity": severity,
            "subject": "DocumentDB Slow Operations",
            "value_str": f"{count}건 (최대 {max_millis}ms)",
            "threshold_str": f"system.profile millis ≥ {int(slowms)}ms (최근 {SLOW_OPS_WINDOW_MIN}분)",
            "recommendation": (
                f"최근 {SLOW_OPS_WINDOW_MIN}분간 slowms({int(slowms)}ms) 임계값을 넘는 op이 "
                f"{count}건 기록됐습니다. 상위 네임스페이스: {top_str}. 해당 컬렉션의 인덱스와 "
                "쿼리 패턴을 점검하세요."
            ),
            "details": {
                "slow_op_count": count,
                "max_millis": max_millis,
                "slowms": int(slowms),
                "window_minutes": SLOW_OPS_WINDOW_MIN,
                "top_namespaces": [{"ns": ns, "count": n} for ns, n in top],
            },
        }
    ]


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

    # --- getProfilingStatus (+ system.profile) → slow-ops / profiler-off ---
    try:
        prof = _run_profiling_status(client, "admin")
        level = prof.get("was", prof.get("level", 0))
        try:
            level_int = int(level)
        except (TypeError, ValueError):
            level_int = 0
        slowms = prof.get("slowms", 100) or 100
        slow_samples = []
        if level_int > 0:
            since_dt = datetime.now(timezone.utc) - _timedelta_minutes(SLOW_OPS_WINDOW_MIN)
            try:
                slow_samples = _read_system_profile(client, "admin", slowms, since_dt)
            except Exception as e:
                print(f"[docdb_mongo] {cluster_id} system.profile read failed: {e}")
        for finding in _build_profiling_findings(prof, slow_samples, slowms):
            _insert_finding(cache_execute, cluster_id, run_ts, finding)
            findings_emitted += 1
    except Exception as e:
        print(f"[docdb_mongo] {cluster_id} getProfilingStatus failed: {e}")

    return {"cluster_id": cluster_id, "findings_emitted": findings_emitted}


def _timedelta_minutes(minutes):
    from datetime import timedelta

    return timedelta(minutes=minutes)


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
        if not resource.get("mongo_secret_arn"):
            continue
        results.append(_process_cluster(resource, secrets, cache_execute, run_ts))

    return {"statusCode": 200, "body": json.dumps({"processed": len(results), "results": results}, default=str)}

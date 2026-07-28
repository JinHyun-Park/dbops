"""DocumentDB Mongo-protocol deep-diagnosis collector — unit tests.

These tests MUST run without pymongo installed: the collector imports pymongo
lazily inside its client factory, and we patch the module-level _CLIENT_FACTORY
hook with a fake client. We also patch boto3 (resource/client) at the module
level so lambda_handler runs with no AWS.

Strategy:
  - Load the handler via importlib (mirrors test_docdb_findings._load).
  - Fake MongoClient whose .admin.command returns injected fixtures for
    serverStatus / currentOp, and which RECORDS every command / collection it is
    asked for (so the removed, DocumentDB-unsupported profiler calls stay gone).
  - Capture cache writes by patching the module's cache-execute helper, and
    assert which check_types were emitted.
"""

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "docdb_mongo_collector"


def _load():
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(
        "docdb_mongo_collector_handler", _ROOT / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h = _load()

_RUN_TS = "2026-06-12T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fake Mongo client — returns injected command results; never imports pymongo
# ---------------------------------------------------------------------------


class _FakeDB:
    """Records every command / collection access so a test can assert the
    collector never issues one DocumentDB does not support (`profile`,
    <db>.system.profile). Unknown commands return {} rather than raising: the
    collector swallows command errors by design, so a raise here would be
    invisible and the assertion has to be on the RECORD, not on the exception."""

    def __init__(self, command_results, calls):
        self._command_results = command_results
        self.calls = calls

    def command(self, name, *args, **kwargs):
        self.calls.append(name)
        if name == "serverStatus":
            return self._command_results.get("serverStatus", {})
        if name == "currentOp":
            return self._command_results.get("currentOp", {"inprog": []})
        return {}

    def __getitem__(self, name):
        self.calls.append(f"collection:{name}")
        return MagicMock()


class _FakeClient:
    def __init__(self, command_results=None, calls=None, raise_on_connect=False):
        if raise_on_connect:
            raise RuntimeError("connection refused")
        self.calls = calls if calls is not None else []
        self._db = _FakeDB(command_results or {}, self.calls)
        self.closed = False

    @property
    def admin(self):
        return self._db

    def __getitem__(self, name):
        return self._db

    def close(self):
        self.closed = True


class _ResourceNotFound(Exception):
    """Stand-in for botocore's ResourceNotFoundException: the collector reads the
    bounded Error.Code, never the message."""

    def __init__(self):
        super().__init__("The specified log group does not exist.")
        self.response = {"Error": {"Code": "ResourceNotFoundException"}}


class _AwsError(Exception):
    """A botocore-shaped ClientError whose MESSAGE is exactly what must never
    reach a finding payload: it carries the caller's role ARN."""

    def __init__(self, code):
        super().__init__(
            "An error occurred (%s) when calling the FilterLogEvents operation: "
            "User: arn:aws:sts::123456789012:assumed-role/dbops-spoke-role/dbops "
            "is not authorized to perform: logs:FilterLogEvents" % code
        )
        self.response = {"Error": {"Code": code}}


class _FakeDocDB:
    """DocumentDB control plane. describe_db_cluster_parameters is deliberately
    PAGINATED (profiler on page 1, the other two on page 2) so the collector's
    Marker loop is exercised, and the Marker is a real string so the
    isinstance-str guard is what ends it."""

    def __init__(self, parameter_group="dbops-docdb-cpg", exports=("profiler",),
                 profiler="enabled", threshold_ms="100", sampling_rate="1.0",
                 describe_error=None, params_error=None):
        self.parameter_group = parameter_group
        self.exports = list(exports)
        self.profiler = profiler
        self.threshold_ms = threshold_ms
        self.sampling_rate = sampling_rate
        self.describe_error = describe_error
        self.params_error = params_error
        self.param_calls = []

    def describe_db_clusters(self, DBClusterIdentifier=None):
        if self.describe_error:
            raise self.describe_error
        return {"DBClusters": [{
            "DBClusterIdentifier": DBClusterIdentifier,
            "DBClusterParameterGroup": self.parameter_group,
            "EnabledCloudwatchLogsExports": self.exports,
        }]}

    def describe_db_cluster_parameters(self, **kwargs):
        if self.params_error:
            raise self.params_error
        self.param_calls.append(kwargs)
        if not kwargs.get("Marker"):
            return {
                "Parameters": [
                    {"ParameterName": "audit_logs", "ParameterValue": "disabled"},
                    {"ParameterName": "profiler", "ParameterValue": self.profiler},
                ],
                "Marker": "page-2",
            }
        return {"Parameters": [
            {"ParameterName": "profiler_threshold_ms", "ParameterValue": self.threshold_ms},
            {"ParameterName": "profiler_sampling_rate", "ParameterValue": self.sampling_rate},
        ]}


class _FakeLogs:
    def __init__(self, pages=None, error=None):
        self.pages = pages if pages is not None else [{"events": []}]
        self.error = error
        self.calls = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        idx = min(len(self.calls) - 1, len(self.pages) - 1)
        return self.pages[idx]


class _FakeSession:
    def __init__(self, docdb=None, logs=None):
        self._docdb = docdb
        self._logs = logs

    def client(self, name, *a, **k):
        if name == "docdb" and self._docdb is not None:
            return self._docdb
        if name == "logs" and self._logs is not None:
            return self._logs
        return MagicMock()


def _run_handler(clusters, command_results=None, connect_errors=None,
                 mongo_calls=None, docdb=None, logs=None):
    """Run lambda_handler with fakes. Returns (emitted, metric_writes, result).

    clusters: list of registry rows.
    connect_errors: set of cluster_ids whose client factory should raise.
    mongo_calls: optional list that collects every Mongo command / collection
      name the collector touched.
    docdb/logs: control-plane fakes for the profiler pass. Left None, the pass
      gets bare MagicMocks, its parameter read raises, and the state resolves to
      "unknown", which must emit NO finding.

    query_stats writes land in `_run_handler.query_stats` on the returned result
    dict under key "_query_stats" (see below).
    """
    connect_errors = connect_errors or set()
    emitted = []
    metric_writes = []
    query_stats = []

    def fake_cache_execute(rds_data, c_arn, s_arn, db):
        def cache_execute(sql, params):
            low = sql.lower()
            if "query_stats" in low:
                query_stats.append({"sql": sql, **params})
            elif sql.strip().upper().startswith("INSERT INTO CLUSTER_HEALTH_FINDINGS") or (
                "cluster_health_findings" in low
            ):
                emitted.append(
                    {
                        "cluster_id": params["cluster_id"],
                        "check_type": params["check_type"],
                        "severity": params["severity"],
                        "value_str": params.get("value_str", ""),
                        "recommendation": params.get("recommendation", ""),
                        "details": params.get("details", "{}"),
                        "ts": params.get("ts"),
                    }
                )
            elif "metric_snapshots" in low:
                metric_writes.append(
                    {"cluster_id": params["cluster_id"], "metric_type": params["metric_type"],
                     "value": params["value"], "ts": params.get("ts")}
                )
        return cache_execute

    def fake_factory(host, port, username, password):
        # cluster_id isn't passed to the factory; map by host (we set host=cluster_id).
        if host in connect_errors:
            raise RuntimeError("connection refused")
        return _FakeClient(command_results=command_results, calls=mongo_calls)

    table = MagicMock()
    table.scan.return_value = {"Items": clusters}

    dynamo_resource = MagicMock()
    dynamo_resource.Table.return_value = table

    secrets_client = MagicMock()

    def get_secret_value(SecretId):
        # cluster row carried host == cluster_id so the fake factory can route errors.
        cid = SecretId.replace("secret:", "")
        return {"SecretString": (
            '{"host": "%s", "port": 27017, "username": "ro", "password": "pw"}' % cid
        )}

    secrets_client.get_secret_value.side_effect = get_secret_value

    def fake_boto3_client(service, *a, **k):
        if service == "secretsmanager":
            return secrets_client
        return MagicMock()  # rds-data

    env = {
        "CLUSTERS_TABLE": "clusters",
        "CACHE_DB_CLUSTER_ARN": "arn:cache",
        "CACHE_DB_SECRET_ARN": "secret:cache",
        "CACHE_DB_NAME": "dbops",
    }

    session = _FakeSession(docdb=docdb, logs=logs)

    with patch.dict(h.os.environ, env, clear=False), \
         patch.object(h.boto3, "resource", return_value=dynamo_resource), \
         patch.object(h.boto3, "client", side_effect=fake_boto3_client), \
         patch.object(h, "_make_cache_execute", side_effect=fake_cache_execute), \
         patch.object(h, "_session_for", return_value=session), \
         patch.object(h, "_CLIENT_FACTORY", fake_factory):
        result = h.lambda_handler({}, None)

    result["_query_stats"] = query_stats
    return emitted, metric_writes, result


def _docdb_row(cluster_id, with_secret=True, family="documentdb"):
    row = {"cluster_id": cluster_id, "engine_family": family}
    if with_secret:
        row["mongo_secret_arn"] = f"secret:{cluster_id}"
    return row


# ---------------------------------------------------------------------------
# Test 1 — long-running ops finding fires (currentOp secs_running ≥ 10)
# ---------------------------------------------------------------------------


def test_long_running_ops_warning_fires():
    cmd = {
        "serverStatus": {},
        "currentOp": {"inprog": [
            {"secs_running": 12, "ns": "appdb.orders"},
            {"secs_running": 3, "ns": "appdb.users"},  # below threshold → ignored
        ]},
    }
    emitted, _, _ = _run_handler([_docdb_row("docdb-a")], command_results=cmd)
    fin = next((e for e in emitted if e["check_type"] == "docdb_mongo_long_running_ops"), None)
    assert fin is not None, f"expected long-running finding, got {[e['check_type'] for e in emitted]}"
    assert fin["severity"] == "warning"
    assert "1" in fin["value_str"]


def test_long_running_ops_critical_when_five_or_more():
    cmd = {
        "serverStatus": {},
        "currentOp": {"inprog": [{"secs_running": 20, "ns": f"db.c{i}"} for i in range(5)]},
    }
    emitted, _, _ = _run_handler([_docdb_row("docdb-a")], command_results=cmd)
    fin = next((e for e in emitted if e["check_type"] == "docdb_mongo_long_running_ops"), None)
    assert fin is not None
    assert fin["severity"] == "critical"


def test_no_long_running_ops_when_all_short():
    cmd = {
        "serverStatus": {},
        "currentOp": {"inprog": [{"secs_running": 2, "ns": "db.c"}]},
    }
    emitted, _, _ = _run_handler([_docdb_row("docdb-a")], command_results=cmd)
    assert not any(e["check_type"] == "docdb_mongo_long_running_ops" for e in emitted)


# ---------------------------------------------------------------------------
# Test 2/3: the profiler path is GONE (E-0)
#
# Managed Amazon DocumentDB supports neither the `profile` command nor
# <db>.system.profile, so the old branch issued an unsupported command every
# 5 minutes per cluster and its docdb_mongo_profiler_off recommendation told the
# DBA to run db.setProfilingLevel(...) on an engine that has no such call. Both
# the call and the advice are removed; slow ops arrive via the CloudWatch
# profiler log export (E-1) instead.
# ---------------------------------------------------------------------------


def test_collector_never_issues_unsupported_profiler_commands():
    calls = []
    cmd = {"serverStatus": {}, "currentOp": {"inprog": [{"secs_running": 12, "ns": "db.c"}]}}
    emitted, metric_writes, _ = _run_handler(
        [_docdb_row("docdb-a")], command_results=cmd, mongo_calls=calls
    )
    assert "profile" not in calls, f"unsupported `profile` command issued: {calls}"
    assert not any(c.startswith("collection:") for c in calls), (
        f"touched a collection (system.profile is unsupported): {calls}"
    )
    assert calls == ["serverStatus", "currentOp"], calls
    # The supported branches still work.
    assert any(e["check_type"] == "docdb_mongo_long_running_ops" for e in emitted)
    # With no control-plane fakes the profiler state resolves to "unknown", and an
    # unknown state must claim NOTHING in either direction.
    assert not any(
        e["check_type"] in ("docdb_mongo_slow_ops", "docdb_mongo_profiler_off")
        for e in emitted
    ), f"unknown state must emit no profiler finding, got {[e['check_type'] for e in emitted]}"


def test_no_profiler_setprofilinglevel_advice_in_source():
    """The impossible recommendation must not survive in the source either.

    The unsupported surface must not be CALLED, and the one thing the operator
    may still be told about db.setProfilingLevel is that DocumentDB does not
    implement it (that confusion is what cost the original implementation), so
    every mention has to sit inside an explicit denial rather than advice."""
    src = (_ROOT / "handler.py").read_text()
    assert "_read_system_profile" not in src
    assert "_run_profiling_status" not in src
    # (that the collector never ACCESSES <db>.system.profile is asserted on the
    # recorded Mongo calls above, not on prose that may still name it)
    denials =("지원하지 않습니다", "does not implement", "NOT db.setProfilingLevel")
    offset = src.find("setProfilingLevel")
    while offset != -1:
        window = src[max(0, offset - 200):offset + 200]
        assert any(d in window for d in denials), (
            f"setProfilingLevel mentioned as advice near offset {offset}"
        )
        offset = src.find("setProfilingLevel", offset + 1)


# ---------------------------------------------------------------------------
# Test 4 — serverStatus emits mongo_* metric rows
# ---------------------------------------------------------------------------


def test_server_status_emits_metrics():
    cmd = {
        "serverStatus": {
            "connections": {"current": 42, "available": 158},
            "opcounters": {"query": 1000, "insert": 50, "update": 30, "delete": 5},
            "mem": {"resident": 2048},
        },
        "currentOp": {"inprog": []},
    }
    _, metric_writes, _ = _run_handler([_docdb_row("docdb-a")], command_results=cmd)
    mtypes = {m["metric_type"] for m in metric_writes}
    assert "mongo_connections_current" in mtypes
    assert "mongo_connections_available" in mtypes
    assert "mongo_opcounters_query" in mtypes
    assert "mongo_mem_resident_mb" in mtypes
    cur = next(m for m in metric_writes if m["metric_type"] == "mongo_connections_current")
    assert cur["value"] == 42.0


# ---------------------------------------------------------------------------
# Test 5: a documentdb row with NO mongo secret still gets the profiler pass
#
# This is the whole reason the profiler branch sits outside the mongo_secret_arn
# gate: api/clusters `_register_docdb` never writes that field, so every cluster
# registered through the product UI arrives here without it. Gating slow-op
# ingestion on it would ship the feature dark.
# ---------------------------------------------------------------------------


def test_documentdb_without_mongo_secret_still_runs_profiler_pass():
    clusters = [
        {"cluster_id": "pg-1", "engine_family": "relational"},
        _docdb_row("docdb-no-secret", with_secret=False),  # documentdb but no secret
        {"cluster_id": "ddb-1", "engine_family": "dynamodb"},
    ]
    emitted, metric_writes, result = _run_handler(
        clusters,
        docdb=_FakeDocDB(profiler="disabled"),
        logs=_FakeLogs(error=_ResourceNotFound()),
    )
    # No Mongo connection was made, so no serverStatus metrics.
    assert metric_writes == []
    import json
    body = json.loads(result["body"])
    assert body["processed"] == 1
    row = body["results"][0]
    assert row["cluster_id"] == "docdb-no-secret"
    assert row["mongo"] == "skipped: no mongo_secret_arn"
    # ...but the profiler guidance finding IS emitted.
    assert [e["check_type"] for e in emitted] == ["docdb_mongo_profiler_off"]


# ---------------------------------------------------------------------------
# Test 6 — one cluster connection error → logged, no raise, others processed
# ---------------------------------------------------------------------------


def test_connection_error_is_isolated_other_clusters_still_run():
    cmd = {
        "serverStatus": {},
        "currentOp": {"inprog": [{"secs_running": 30, "ns": "db.c"}]},
    }
    clusters = [_docdb_row("docdb-bad"), _docdb_row("docdb-good")]
    # docdb-bad's host == "docdb-bad" → factory raises for it only.
    emitted, _, result = _run_handler(
        clusters, command_results=cmd, connect_errors={"docdb-bad"}
    )
    # Good cluster still produced its long-running finding.
    good = [e for e in emitted if e["cluster_id"] == "docdb-good"]
    assert any(e["check_type"] == "docdb_mongo_long_running_ops" for e in good)
    # No finding for the bad cluster, but the run did not raise.
    assert not any(e["cluster_id"] == "docdb-bad" for e in emitted)
    import json
    body = json.loads(result["body"])
    assert body["processed"] == 2  # both attempted
    bad = next(r for r in body["results"] if r["cluster_id"] == "docdb-bad")
    assert "error" in bad


# ---------------------------------------------------------------------------
# Test 7 — shared run_ts across all findings (one cycle, one snapshot_time)
# ---------------------------------------------------------------------------


def test_shared_run_ts_across_findings():
    cmd = {
        "serverStatus": {},
        "currentOp": {"inprog": [{"secs_running": 30, "ns": "db.c"}]},
    }
    emitted, _, _ = _run_handler([_docdb_row("docdb-a")], command_results=cmd)
    ts_values = {e["ts"] for e in emitted}
    assert len(ts_values) == 1, f"expected single shared run_ts, got {ts_values}"


# ---------------------------------------------------------------------------
# Test 8 — non-documentdb rows are skipped entirely
# ---------------------------------------------------------------------------


def test_non_documentdb_rows_skipped():
    cmd = {
        "serverStatus": {},
        "currentOp": {"inprog": [{"secs_running": 99, "ns": "db.c"}]},
    }
    clusters = [
        {"cluster_id": "pg-1", "engine_family": "relational", "mongo_secret_arn": "secret:pg-1"},
    ]
    emitted, _, result = _run_handler(clusters, command_results=cmd)
    assert emitted == []
    import json
    assert json.loads(result["body"])["processed"] == 0


# ---------------------------------------------------------------------------
# E1-6: CloudWatch profiler-log ingestion
#
# FIXTURE PROVENANCE: the profiler entry below is the one AWS publishes at
# docs.aws.amazon.com/documentdb/latest/devguide/performance-slow-queries.html
# ("Query running slow" -> Investigate), field for field. It is
# DOCUMENTATION-DERIVED, not captured from a live cluster: the dev cluster
# dbops-docdb-test has profiler=enabled and exports=["profiler"], but
# /aws/docdb/dbops-docdb-test/profiler does not exist yet (verified 2026-07-28:
# filter-log-events returns ResourceNotFoundException), because a log group is
# only created once an op first crosses profiler_threshold_ms. The field names are
# independently corroborated by the Logs Insights examples AWS documents for this
# group (profiling.html "Common queries" filters on ns, millis, op, planSummary).
# ---------------------------------------------------------------------------

_DOC_PROFILER_ENTRY = {
    "op": "query",
    "ts": 1721374275673,
    "ns": "test.perf",
    "command": {
        "find": "perf",
        "filter": {"threadRunCount": 0},
        "$db": "test",
        "lsid": {"id": {"$binary": "oO2wEtpgQIK+y9KGByYnsw==", "$type": "4"}},
        "$readPreference": {"mode": "secondaryPreferred"},
    },
    "cursorExhausted": True,
    "nreturned": 0,
    "responseLength": 0,
    "protocol": "op_query",
    "millis": 137,
    "planSummary": "IXSCAN",
    "execStats": {
        "stage": "FETCH",
        "nReturned": "0",
        "executionTimeMillisEstimate": "100.346",
        "inputStage": {
            "stage": "IXSCAN",
            "nReturned": "0",
            "executionTimeMillisEstimate": "100.342",
            "indexName": "threadRunCount_1",
        },
    },
    "client": "172.31.6.165:43154",
    "appName": "ProdAppTester14",
    "user": "adminuser",
}


def _event(entry):
    return {"message": json.dumps(entry), "eventId": "e1", "timestamp": entry.get("ts", 0)}


def test_parses_the_documented_profiler_entry():
    rec = h.parse_profiler_entry(json.dumps(_DOC_PROFILER_ENTRY))
    assert rec is not None
    assert rec["op"] == "query"
    assert rec["ns"] == "test.perf"
    assert rec["millis"] == 137.0
    assert rec["plan_summary"] == "IXSCAN"
    assert rec["nreturned"] == 0
    # Shape identity is op + ns + the filter KEYS, never the values, and never
    # planSummary (a plan flip must land on the SAME query_hash).
    assert rec["query_text"] == "query test.perf {threadRunCount}"
    other_value = dict(_DOC_PROFILER_ENTRY)
    other_value["command"] = dict(_DOC_PROFILER_ENTRY["command"], filter={"threadRunCount": 99})
    collscan = dict(_DOC_PROFILER_ENTRY, planSummary="COLLSCAN")
    assert h.parse_profiler_entry(json.dumps(other_value))["query_hash"] == rec["query_hash"]
    assert h.parse_profiler_entry(json.dumps(collscan))["query_hash"] == rec["query_hash"]
    # A different filter shape is a different query.
    other_shape = dict(_DOC_PROFILER_ENTRY)
    other_shape["command"] = dict(_DOC_PROFILER_ENTRY["command"], filter={"customerId": 1})
    assert h.parse_profiler_entry(json.dumps(other_shape))["query_hash"] != rec["query_hash"]


def test_parse_rejects_non_profiler_lines():
    assert h.parse_profiler_entry("not json at all") is None
    assert h.parse_profiler_entry(json.dumps(["a", "list"])) is None
    assert h.parse_profiler_entry(json.dumps({"op": "query", "ns": "a.b"})) is None  # no millis
    assert h.parse_profiler_entry(json.dumps({"millis": "slow"})) is None
    assert h.parse_profiler_entry(None) is None


def test_aggregate_pipeline_op_uses_stage_names():
    entry = {
        "op": "command", "ns": "test.daily_cases", "millis": 559,
        "planSummary": "COLLSCAN", "nreturned": 1,
        "command": {"aggregate": "daily_cases", "pipeline": [{"$match": {}}, {"$group": {}}]},
    }
    rec = h.parse_profiler_entry(json.dumps(entry))
    assert rec["query_text"] == "command test.daily_cases {$match, $group}"


def test_slow_ops_finding_and_query_stats_rows():
    entries = [
        _DOC_PROFILER_ENTRY,
        dict(_DOC_PROFILER_ENTRY, millis=463),
        {"op": "command", "ns": "test.daily_cases", "millis": 900, "planSummary": "COLLSCAN",
         "nreturned": 5, "command": {"aggregate": "daily_cases", "pipeline": [{"$match": {}}]}},
    ]
    emitted, _, result = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": [_event(e) for e in entries]}]),
    )
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_slow_ops")
    assert fin["severity"] == "warning"  # 3 ops, below the 5-op critical line
    assert fin["value_str"] == "3건 (최대 900ms)"
    details = json.loads(fin["details"])
    assert details["slow_op_count"] == 3
    assert details["threshold_ms"] == 100          # read from the parameter group
    assert details["sampling_rate"] == 1.0
    assert details["window_minutes"] == 5
    assert details["truncated"] is False
    assert details["log_group"] == "/aws/docdb/docdb-a/profiler"
    assert {n["ns"] for n in details["top_namespaces"]} == {"test.perf", "test.daily_cases"}
    assert details["collscan_shapes"] == ["command test.daily_cases {$match}"]
    assert "COLLSCAN" in fin["recommendation"]

    rows = result["_query_stats"]
    assert len(rows) == 2, [r["query_text"] for r in rows]
    # Worst total time first, and every row carries the cluster_id the readers
    # filter on.
    assert rows[0]["query_text"] == "command test.daily_cases {$match}"
    assert rows[0]["calls"] == 1
    assert rows[0]["total_time_ms"] == 900.0
    assert rows[0]["rows_returned"] == 5
    perf = next(r for r in rows if r["query_text"] == "query test.perf {threadRunCount}")
    assert perf["calls"] == 2
    assert perf["total_time_ms"] == 600.0  # 137 + 463
    assert len({row["ts"] for row in rows}) == 1, "one window is one snapshot_time"
    for row in rows:
        assert row["cluster_id"] == "docdb-a"
        assert len(row["query_hash"]) <= 64  # query_stats.query_hash is VARCHAR(64)
        # Deliberately NOT the finding's run_ts (it was, before the accumulation
        # was made idempotent). A query_stats row is stamped with the grid-aligned
        # WINDOW END, because that is the idempotency key a retry has to re-derive,
        # and run_ts differs between a run and its retry. Asserted without knowing
        # the test's own clock: a window end is always exactly (interval - lag)
        # into its 5-minute grid cell, and it is always in the past.
        assert row["ts"] != fin["ts"]
        row_ms = int(datetime.fromisoformat(row["ts"]).timestamp() * 1000)
        assert row_ms % (5 * 60_000) == (5 - h.PROFILER_DELIVERY_LAG_MIN) * 60_000
        assert row_ms < int(datetime.fromisoformat(fin["ts"]).timestamp() * 1000)


def test_query_stats_rows_are_readable_by_the_existing_readers():
    """The persisted rows must satisfy the query_stats contract every existing
    reader is written against: the same table, the same columns, and CUMULATIVE
    counters (api/dashboard _slow_queries / _query_detail, api/alerts,
    report_generator and the engine-neutral query_regression collector all read
    calls / total_time_ms / mean_time_ms / rows_returned that only grow)."""
    _, _, result = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": [_event(_DOC_PROFILER_ENTRY)]}]),
    )
    row = result["_query_stats"][0]
    sql = " ".join(row["sql"].split())  # normalize the concatenation whitespace
    assert "INSERT INTO query_stats" in sql
    # Exactly the columns the readers select.
    for column in ("cluster_id", "snapshot_time", "query_hash", "query_text",
                   "calls", "total_time_ms", "mean_time_ms", "rows_returned"):
        assert column in sql, column
    # Cumulative, not per-window: each counter is ADDED to the newest prior row
    # for the same (cluster_id, query_hash). Asserted POSITIONALLY (the value that
    # lands in each column), because the accumulate expressions also appear inside
    # the mean_time_ms division, so a bare substring check would pass even with a
    # raw per-window value written into the calls column.
    assert (
        "SELECT :cluster_id, :ts::timestamptz, :query_hash, :query_text, "
        "COALESCE(p.calls, 0) + :calls, "
        "COALESCE(p.total_time_ms, 0) + :total_time_ms, "
        "(COALESCE(p.total_time_ms, 0) + :total_time_ms) "
        "/ NULLIF(COALESCE(p.calls, 0) + :calls, 0), "
        "COALESCE(p.rows_returned, 0) + :rows_returned"
    ) in sql, sql
    # The prior row is really read (all three counters), and it is the NEWEST row
    # for this exact (cluster_id, query_hash).
    assert (
        "SELECT calls, total_time_ms, rows_returned FROM query_stats "
        "WHERE cluster_id = :cluster_id AND query_hash = :query_hash "
        "ORDER BY snapshot_time DESC LIMIT 1"
    ) in sql, sql
    assert "LEFT JOIN" in sql, "a first-ever window must still insert a row"
    assert "NULLIF" in sql, "mean_time_ms must not divide by zero"


def test_empty_log_group_with_profiler_on_is_good_news_not_blindness():
    """Profiler ON + nothing logged = no slow ops. That must produce NEITHER a
    slow-ops finding NOR a 'we are blind' finding."""
    emitted, _, result = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": []}]),
    )
    assert [e["check_type"] for e in emitted] == []
    row = json.loads(result["body"])["results"][0]
    assert row["profiler"] == "on"
    assert row["log_read"] == "ok"
    assert row["events"] == 0
    assert result["_query_stats"] == []


def test_missing_log_group_with_profiler_on_is_reported_not_claimed_clean():
    """The group does not exist yet (AWS: it can take up to an hour to appear, and
    never appears until the first op crosses the threshold). Nothing was measured,
    so nothing is claimed in either direction, and the state is distinguishable
    from a successful empty read."""
    emitted, _, result = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(error=_ResourceNotFound()),
    )
    assert emitted == []
    row = json.loads(result["body"])["results"][0]
    assert row["profiler"] == "on"
    assert row["log_read"] == "no_log_group"


def test_failed_log_read_is_a_finding_not_silence():
    """The one blindness the commit headline MISSED: profiler ON and the log group
    unreadable (IAM denial, throttle, spoke role). Before this finding existed,
    that produced 0 findings and 0 query_stats rows, byte-identical to a healthy
    cluster with a genuinely empty window, and the read_failed marker only reached
    the Lambda return value, i.e. CloudWatch, where nobody looks."""
    emitted, _, result = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(error=_AwsError("AccessDeniedException")),
    )
    row = json.loads(result["body"])["results"][0]
    assert row["profiler"] == "on"
    assert row["log_read"] == "read_failed"
    assert row["log_read_error"] == "AccessDeniedException"
    assert result["_query_stats"] == []

    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_profiler_read_failed")
    assert fin["severity"] == "info"
    assert "AccessDeniedException" in fin["value_str"]
    assert "logs:FilterLogEvents" in fin["recommendation"]
    # Bounded CODE only. The exception text carries the assumed-role ARN.
    blob = json.dumps(fin, ensure_ascii=False)
    assert "assumed-role" not in blob and "not authorized to perform" not in blob

    # And it must be DISTINGUISHABLE from the healthy case, which is the whole
    # point: same profiler state, same zero rows, different finding count.
    healthy, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": []}]),
    )
    assert healthy == []

    # A check_type with no CHECK_LABELS entry only ever shows under "All", so the
    # panel label is part of the fix, not decoration.
    panel = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "components"
             / "dashboard" / "maintenance-health-panel.tsx").read_text()
    assert "docdb_mongo_profiler_read_failed:" in panel


def test_engine_default_profiler_params_are_not_read_as_blindness():
    """AWS returns a Parameters entry with NO ParameterValue when the value is the
    engine default, which is the common case for a group where only `profiler` was
    modified. Both defaults are read at the source (profiling.html, 2026-07-28):
    profiler_threshold_ms default 100 (permitted 50-INT_MAX), profiler_sampling_rate
    default 1.0 (permitted 0.0-1.0). A wrong sampling default is not a cosmetic
    bug: <= 0.0 makes read_profiler_state call a HEALTHY cluster blind and
    fabricates docdb_mongo_profiler_off, which is what this collector's own
    Directive forbids."""
    assert h.DOC_DEFAULT_THRESHOLD_MS == 100
    assert h.DOC_DEFAULT_SAMPLING_RATE == 1.0

    state, details = h.read_profiler_state(
        _FakeDocDB(threshold_ms=None, sampling_rate=None), "docdb-a")
    assert state == "on", details
    assert details["threshold_ms"] == 100
    assert details["sampling_rate"] == 1.0

    # End to end: no fabricated finding on that cluster.
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(threshold_ms=None, sampling_rate=None),
        logs=_FakeLogs(pages=[{"events": []}]),
    )
    assert emitted == []


def test_profiler_off_emits_guidance_even_when_log_group_missing():
    """ResourceNotFoundException must NOT become silence: the profiler being off
    is exactly why the log group does not exist, and that is the case the
    operator has to be told about."""
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(profiler="disabled"),
        logs=_FakeLogs(error=_ResourceNotFound()),
    )
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_profiler_off")
    assert fin["severity"] == "info"
    assert "profiler=disabled" in fin["value_str"]
    assert "set_docdb_profiler" in fin["recommendation"]
    # The impossible MongoDB advice must not come back with the finding.
    assert "setProfilingLevel" in fin["recommendation"]
    assert "지원하지 않습니다" in fin["recommendation"]
    details = json.loads(fin["details"])
    assert details["parameter_group"] == "dbops-docdb-cpg"
    assert details["profiler_param"] == "disabled"


def test_profiler_param_on_but_log_export_off_is_still_blind():
    """AWS: without the log-export step "profiling logs will not be sent to
    CloudWatch Logs". profiler=enabled alone is not visibility."""
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(profiler="enabled", exports=()),
        logs=_FakeLogs(error=_ResourceNotFound()),
    )
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_profiler_off")
    assert "내보내기 OFF" in fin["value_str"]
    assert json.loads(fin["details"])["log_export"] is False


def test_zero_sampling_rate_is_blind():
    """profiler_sampling_rate=0.0 logs zero percent of slow ops, so the operator
    is as blind as with the parameter off."""
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(sampling_rate="0.0"),
        logs=_FakeLogs(pages=[{"events": []}]),
    )
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_profiler_off")
    assert "sampling_rate=0.0" in fin["value_str"]


def test_partial_sampling_rate_qualifies_the_count():
    """With sampling < 1.0 the logged count is a SAMPLE. Reporting it flat would
    under-report the real slow-op volume."""
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(sampling_rate="0.5"),
        logs=_FakeLogs(pages=[{"events": [_event(_DOC_PROFILER_ENTRY)]}]),
    )
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_slow_ops")
    assert "표본" in fin["recommendation"]
    assert json.loads(fin["details"])["sampling_rate"] == 0.5


def test_profiler_state_lookup_failure_claims_nothing():
    """A failed control-plane read is "unknown", not "off": nagging an operator
    whose profiler is already on would be a fabricated finding."""
    emitted, _, result = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(describe_error=RuntimeError("boom")),
        logs=_FakeLogs(),
    )
    assert not any(e["check_type"].startswith("docdb_mongo_profiler") for e in emitted)
    assert json.loads(result["body"])["results"][0]["profiler"] == "unknown"


def test_five_slow_ops_is_critical():
    entries = [dict(_DOC_PROFILER_ENTRY, millis=200 + i) for i in range(5)]
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": [_event(e) for e in entries]}]),
    )
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_slow_ops")
    assert fin["severity"] == "critical"


def test_log_read_is_paginated_and_bounded():
    """nextToken is followed only while it is a real string (a bare MagicMock
    marker would otherwise spin forever) and the window is passed as epoch ms."""
    page1 = {"events": [_event(_DOC_PROFILER_ENTRY)], "nextToken": "t2"}
    page2 = {"events": [_event(dict(_DOC_PROFILER_ENTRY, millis=500))],
             "nextToken": MagicMock()}
    logs = _FakeLogs(pages=[page1, page2])
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": []}},
        docdb=_FakeDocDB(), logs=logs,
    )
    assert len(logs.calls) == 2, logs.calls
    assert logs.calls[1]["nextToken"] == "t2"
    fin = next(e for e in emitted if e["check_type"] == "docdb_mongo_slow_ops")
    assert json.loads(fin["details"])["slow_op_count"] == 2
    first = logs.calls[0]
    assert first["logGroupName"] == "/aws/docdb/docdb-a/profiler"
    assert isinstance(first["startTime"], int) and isinstance(first["endTime"], int)
    assert first["endTime"] - first["startTime"] == 5 * 60_000


def test_windows_are_adjacent_and_lag_shifted():
    """Consecutive runs must read ADJACENT windows: an overlap would inflate the
    cumulative query_stats counters, a gap would silently drop slow ops."""
    step_ms = 5 * 60_000
    # Two runs one interval apart, at deliberately unaligned wall-clock offsets.
    start_a, end_a = h.profiler_window_ms(1_000_000_000_000 + 137_000, 5)
    start_b, end_b = h.profiler_window_ms(1_000_000_000_000 + 137_000 + step_ms, 5)
    assert end_a - start_a == step_ms
    assert start_b == end_a, "windows must be adjacent (no gap, no overlap)"

    # The pair above is EXACTLY step_ms apart, so it is adjacent even without the
    # grid snap (`end = now - lag` passes it too). Real EventBridge invocations are
    # not exactly 300000 ms apart, so the assertion that actually tests grid
    # alignment is a pair one interval apart PLUS jitter, landing in two different
    # grid cells: base sits 100000 ms into its cell, so +23000 stays in it and
    # +step-17000 crosses into the next one.
    base = 1_000_000_000_000
    assert base % step_ms == 100_000, "fixture must be mid-cell, not grid-aligned"
    jittered_a = h.profiler_window_ms(base + 23_000, 5)
    jittered_b = h.profiler_window_ms(base + step_ms - 17_000, 5)
    assert jittered_a[1] - jittered_a[0] == step_ms
    assert jittered_b[1] - jittered_b[0] == step_ms
    assert jittered_b[0] == jittered_a[1], (
        "jittered consecutive runs must still read adjacent windows, which only "
        "the interval-grid snap gives"
    )
    # ...and the window ends in the past by EXACTLY the delivery lag. Measured
    # from a now that is already on the interval grid, so grid alignment
    # contributes nothing and only the lag shift is under test (an unaligned now
    # sits up to one interval in the past on its own and would hide a missing
    # lag entirely).
    aligned_now = 1_000_000_200_000
    assert aligned_now % step_ms == 0, "fixture must be grid-aligned"
    _, end_aligned = h.profiler_window_ms(aligned_now, 5)
    assert end_aligned == aligned_now - h.PROFILER_DELIVERY_LAG_MIN * 60_000
    # Pins the lag VALUE, not just the relationship, because the constant's
    # comment makes an arithmetic claim: the AWS Database blog post (NOT the
    # developer guide, which gives no delivery number at all) says "It typically
    # takes 1-2 minutes for your queries to show up in the log events", and 3 is
    # that upper bound plus one minute of headroom. Change the number and that
    # sentence stops being true.
    assert h.PROFILER_DELIVERY_LAG_MIN == 3
    # The read bound is unreachable in a unit test (5000 parsed events), so pin
    # the pair instead. Both matter: the cap must stay >= one page or a single
    # full page would report itself as truncated, and it is what keeps one busy
    # cluster's run inside the Lambda's memory and timeout.
    assert h.PROFILER_PAGE_LIMIT == 1000
    assert h.PROFILER_EVENT_CAP == 5000
    assert h.PROFILER_EVENT_CAP >= h.PROFILER_PAGE_LIMIT


def test_window_falls_back_to_the_shipped_cadence_on_a_garbage_env():
    with patch.dict(h.os.environ, {"COLLECTOR_INTERVAL_MIN": "not-a-number"}, clear=False):
        assert h._interval_min() == h.DEFAULT_INTERVAL_MIN
    with patch.dict(h.os.environ, {"COLLECTOR_INTERVAL_MIN": "0"}, clear=False):
        assert h._interval_min() == h.DEFAULT_INTERVAL_MIN
    with patch.dict(h.os.environ, {"COLLECTOR_INTERVAL_MIN": "20"}, clear=False):
        assert h._interval_min() == 20


def test_both_documentdb_findings_writers_stay_disjoint_and_share_one_snapshot():
    """Freshness-window regression guard (commit 67d1c3e). DocumentDB has exactly
    two findings writers, and the dashboard/agent window is floored at 15 minutes
    on that basis. The new profiler check_types therefore have to (a) come from
    THIS writer, not a third one, and (b) land on the same run_ts as the rest of
    this writer's findings, so one MAX(snapshot_time) batch returns them all."""
    entries = [_DOC_PROFILER_ENTRY]
    emitted, _, _ = _run_handler(
        [_docdb_row("docdb-a")],
        command_results={"serverStatus": {}, "currentOp": {"inprog": [
            {"secs_running": 30, "ns": "test.perf"}]}},
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": [_event(e) for e in entries]}]),
    )
    types = {e["check_type"] for e in emitted}
    assert types == {"docdb_mongo_long_running_ops", "docdb_mongo_slow_ops"}
    assert len({e["ts"] for e in emitted}) == 1, "one cycle must be one snapshot_time"

    # The OTHER writer's check_types (etl_collector/collectors/docdb_findings.py)
    # must not overlap, or the dashboard's latest-per-check_type ranking would
    # have two writers fighting over one row.
    other = (Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"
             / "collectors" / "docdb_findings.py").read_text()
    mine = {"docdb_mongo_long_running_ops", "docdb_mongo_slow_ops",
            "docdb_mongo_profiler_off", "docdb_mongo_profiler_read_failed"}
    for check_type in mine:
        assert f'"{check_type}"' not in other, f"{check_type} written by both writers"


# ---------------------------------------------------------------------------
# Idempotency of the query_stats accumulation.
#
# These tests EXECUTE the collector's own accumulate statement (stdlib sqlite3,
# in memory) instead of recording it, because what has to hold is a property of
# the STATEMENT: re-reading one window must not add its totals twice. A
# recording fake can only assert the SQL text, which is how the double count got
# through the first review of ee0a63c.
# ---------------------------------------------------------------------------

_SQLITE_QUERY_STATS = (
    "CREATE TABLE query_stats (cluster_id TEXT, snapshot_time TEXT, "
    "query_hash TEXT, query_text TEXT, calls INTEGER, total_time_ms REAL, "
    "mean_time_ms REAL, rows_returned INTEGER)"
)

_MID_CELL_MS = 1_000_000_000_000 + 137_000  # deliberately NOT on the interval grid
_STEP_MS = 5 * 60_000


def _sqlite_cache(conn):
    """cache_execute backed by sqlite3, running the collector's REAL sql string.

    Only the Postgres-only cast is stripped (`::timestamptz`); the accumulate
    expression and its guard run verbatim, so weakening either changes what these
    tests measure. Writes aimed at the other tables are dropped: this fake models
    query_stats only."""

    def cache_execute(sql, params):
        if "query_stats" not in sql:
            return
        conn.execute(sql.replace("::timestamptz", ""), params)

    return cache_execute


def _profiler_pass(cache_execute, run_ts, now_ms, entries, cluster_id="docdb-a"):
    """One profiler pass exactly as lambda_handler calls it, with the clock pinned
    so a second pass can be aimed at the same window."""
    session = _FakeSession(
        docdb=_FakeDocDB(),
        logs=_FakeLogs(pages=[{"events": [_event(e) for e in entries]}]),
    )
    return h.collect_profiler(session, cache_execute, cluster_id, run_ts, now_ms=now_ms)


def _stats_rows(conn):
    return conn.execute(
        "SELECT snapshot_time, calls, total_time_ms, mean_time_ms, rows_returned "
        "FROM query_stats ORDER BY snapshot_time, query_text"
    ).fetchall()


def _window_end_iso(now_ms):
    """The window end as an ISO timestamp, derived here INDEPENDENTLY of the
    handler's own conversion so the assertion is on the value, not on a shared
    helper."""
    end_ms = h.profiler_window_ms(now_ms, 5)[1]
    return datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat()


def _two_events():
    """Two ops of the SAME shape: calls 2, total 600ms, mean 300ms."""
    return [dict(_DOC_PROFILER_ENTRY, millis=137), dict(_DOC_PROFILER_ENTRY, millis=463)]


def test_the_same_window_read_twice_advances_the_counters_once():
    """The real failure mode is a SEQUENTIAL retry, not concurrency: EventBridge
    delivery is at-least-once and an async Lambda invocation retries twice by
    default, so a second pass lands in the same interval grid cell, derives the
    SAME window and re-reads the SAME profiler events.

    Unguarded, pass 1 writes prev+W and pass 2 reads prev+W as its own prev and
    writes prev+2W. The inflation is permanent, and every reader of query_stats is
    written against monotonic counters (api/dashboard _slow_queries /
    _query_detail / _workload_diff, api/alerts, report_generator, and the LAG-delta
    query_regression collector, which would publish a regression that never
    happened)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(_SQLITE_QUERY_STATS)
    cache = _sqlite_cache(conn)

    # First-ever window for this (cluster_id, query_hash): no previous row, and a
    # row still has to land, carrying this window's own totals.
    first = _profiler_pass(cache, "2026-06-12T00:00:07+00:00", _MID_CELL_MS, _two_events())
    assert first["query_stats_rows"] == 1
    expected = [(_window_end_iso(_MID_CELL_MS), 2, 600.0, 300.0, 0)]
    assert _stats_rows(conn) == expected, (
        "snapshot_time must be the grid-aligned WINDOW END, which is the only value "
        "a retry re-derives; run_ts differs between a run and its retry"
    )

    # The retry: 41 s later, still inside the same grid cell, different run_ts.
    _profiler_pass(cache, "2026-06-12T00:00:48+00:00", _MID_CELL_MS + 41_000, _two_events())
    assert (_MID_CELL_MS + 41_000) // _STEP_MS == _MID_CELL_MS // _STEP_MS, (
        "fixture must keep both passes in ONE grid cell, or they read two windows"
    )
    assert _stats_rows(conn) == expected, (
        "a re-read of the same window must not add its totals a second time"
    )


def test_two_different_windows_still_accumulate_both():
    """The guard must not block legitimate progress. Consecutive runs read ADJACENT
    windows, so the second window is a different snapshot_time and its totals go on
    top, and the regression collector's LAG delta still recovers the per-window
    mean from the cumulative pair."""
    conn = sqlite3.connect(":memory:")
    conn.execute(_SQLITE_QUERY_STATS)
    cache = _sqlite_cache(conn)

    _profiler_pass(cache, "2026-06-12T00:00:07+00:00", _MID_CELL_MS, _two_events())
    _profiler_pass(cache, "2026-06-12T00:05:07+00:00", _MID_CELL_MS + _STEP_MS, _two_events())

    rows = _stats_rows(conn)
    assert [r[0] for r in rows] == [
        _window_end_iso(_MID_CELL_MS), _window_end_iso(_MID_CELL_MS + _STEP_MS)
    ]
    assert [r[1] for r in rows] == [2, 4], rows          # calls, cumulative
    assert [r[2] for r in rows] == [600.0, 1200.0], rows  # total_time_ms, cumulative

    # What the LAG-delta reader gets out of those two rows: 2 calls, 300ms mean.
    delta = conn.execute(
        "SELECT (calls - prev_calls) AS d_calls, "
        "       (total_time_ms - prev_total) / (calls - prev_calls) AS interval_mean "
        "FROM (SELECT calls, total_time_ms, "
        "             LAG(calls) OVER w AS prev_calls, "
        "             LAG(total_time_ms) OVER w AS prev_total "
        "      FROM query_stats "
        "      WINDOW w AS (PARTITION BY query_hash ORDER BY snapshot_time)) o "
        "WHERE prev_calls IS NOT NULL"
    ).fetchall()
    assert delta == [(2, 300.0)], delta

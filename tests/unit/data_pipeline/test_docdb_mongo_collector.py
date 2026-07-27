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
import sys
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


def _run_handler(clusters, command_results=None, connect_errors=None,
                 mongo_calls=None):
    """Run lambda_handler with fakes. Returns (emitted_findings, result_dict).

    clusters: list of registry rows.
    connect_errors: set of cluster_ids whose client factory should raise.
    mongo_calls: optional list that collects every Mongo command / collection
      name the collector touched.
    """
    connect_errors = connect_errors or set()
    emitted = []
    metric_writes = []

    def fake_cache_execute(rds_data, c_arn, s_arn, db):
        def cache_execute(sql, params):
            if sql.strip().upper().startswith("INSERT INTO CLUSTER_HEALTH_FINDINGS") or (
                "cluster_health_findings" in sql.lower()
            ):
                emitted.append(
                    {
                        "cluster_id": params["cluster_id"],
                        "check_type": params["check_type"],
                        "severity": params["severity"],
                        "value_str": params.get("value_str", ""),
                        "ts": params.get("ts"),
                    }
                )
            elif "metric_snapshots" in sql.lower():
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

    with patch.dict(h.os.environ, env, clear=False), \
         patch.object(h.boto3, "resource", return_value=dynamo_resource), \
         patch.object(h.boto3, "client", side_effect=fake_boto3_client), \
         patch.object(h, "_make_cache_execute", side_effect=fake_cache_execute), \
         patch.object(h, "_CLIENT_FACTORY", fake_factory):
        result = h.lambda_handler({}, None)

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
    assert not any(
        e["check_type"] in ("docdb_mongo_slow_ops", "docdb_mongo_profiler_off")
        for e in emitted
    ), f"profiler findings must be gone, got {[e['check_type'] for e in emitted]}"


def test_no_profiler_setprofilinglevel_advice_in_source():
    """The impossible recommendation must not survive in the source either."""
    src = (_ROOT / "handler.py").read_text()
    assert "setProfilingLevel" not in src
    assert "_read_system_profile" not in src
    assert "_run_profiling_status" not in src


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
# Test 5 — no documentdb-with-secret → no-op
# ---------------------------------------------------------------------------


def test_no_documentdb_with_secret_is_noop():
    clusters = [
        {"cluster_id": "pg-1", "engine_family": "relational"},
        _docdb_row("docdb-no-secret", with_secret=False),  # documentdb but no secret
        {"cluster_id": "ddb-1", "engine_family": "dynamodb"},
    ]
    emitted, metric_writes, result = _run_handler(clusters)
    assert emitted == []
    assert metric_writes == []
    import json
    body = json.loads(result["body"])
    assert body["processed"] == 0


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

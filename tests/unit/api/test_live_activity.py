"""Unit tests for the on-demand LIVE top endpoint (_live_activity, P2-⑧).

Covers:
  - PG cluster → snapshot shape (sessions / blocking / db_counters), the
    `/* source=dbops-live */` marker + includeResultMetadata=True on every stmt,
    buffercache null by default and present when ?buffers=true.
  - Non-PG (DynamoDB) and MySQL → not_applicable, NO Data API call.
  - Data API fault → available:false friendly reason, no str(e) leak.
  - Throttle: two rapid lambda_handler calls issue the target queries once.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_live", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest

_PG_CLUSTER = {
    "cluster_id": "prod-pg",
    "cluster_arn": "arn:aws:rds:ap-northeast-2:123:cluster:prod-pg",
    "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:123:secret:prod-pg",
    "db_name": "appdb",
}


def _resp(cols, records):
    """Build an RDS Data API execute_statement response envelope."""
    return {
        "columnMetadata": [{"name": c} for c in cols],
        "records": records,
    }


# Field-shape helpers matching the handler's scalar parser.
def _s(v):
    return {"stringValue": v}


def _l(v):
    return {"longValue": v}


def _d(v):
    return {"doubleValue": v}


_SESSIONS = _resp(
    ["pid", "usename", "state", "wait", "age_sec", "query", "backend_type"],
    [
        [_l(101), _s("app"), _s("active"), _s("CPU"), _d(12.5), _s("SELECT 1"), _s("client backend")],
        [_l(102), _s("app"), _s("idle in transaction"), _s("Lock:relation"), _d(4.0), _s("UPDATE t"), _s("client backend")],
    ],
)
_BLOCKING = _resp(["pid", "blockers"], [[_l(102), _s("101,55")]])
_COUNTERS = _resp(
    ["xact_commit", "xact_rollback", "tup_returned", "tup_fetched",
     "tup_inserted", "tup_updated", "tup_deleted", "blks_read", "blks_hit"],
    [[_l(1000), _l(2), _l(5000), _l(4000), _l(300), _l(150), _l(20), _l(10), _l(9000)]],
)
_BUF_SUMMARY = _resp(["used", "total"], [[_l(8000), _l(16384)]])
_BUF_TOP = _resp(["relation", "buffers"], [[_s("orders"), _l(4000)], [_s("users"), _l(1200)]])


def _mock_rds():
    """A mock rds-data client routing execute_statement by SQL content."""
    m = MagicMock()

    def _exec(**kw):
        sql = kw["sql"]
        if "pg_blocking_pids" in sql:
            return _BLOCKING
        if "pg_stat_activity" in sql:
            return _SESSIONS
        if "pg_stat_database" in sql:
            return _COUNTERS
        if "relname" in sql:  # buffercache top relations
            return _BUF_TOP
        if "pg_buffercache" in sql:
            return _BUF_SUMMARY
        raise AssertionError(f"unexpected SQL: {sql}")

    m.execute_statement.side_effect = _exec
    return m


def _patch_pg(monkeypatch, rds, engine="aurora-postgresql", cluster=_PG_CLUSTER):
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: engine)
    monkeypatch.setattr(handler, "_lookup_cluster", lambda cid: dict(cluster) if cluster else {})
    monkeypatch.setattr(handler.boto3, "client", lambda svc, *a, **k: rds)


# ---------------------------------------------------------------------------


def test_live_activity_pg_shape(monkeypatch):
    rds = _mock_rds()
    _patch_pg(monkeypatch, rds)
    out = handler._live_activity("prod-pg")

    assert out["available"] is True
    assert out["cluster_id"] == "prod-pg"
    assert isinstance(out["captured_at"], int)
    assert len(out["sessions"]) == 2
    assert out["sessions"][0]["pid"] == 101
    # blocking: CSV parsed into an int list
    assert out["blocking"] == [{"pid": 102, "blockers": [101, 55]}]
    assert out["db_counters"]["xact_commit"] == 1000
    # buffercache is NOT fetched on the default (poll) path.
    assert out["buffercache"] is None

    # Every statement carries the live marker + includeResultMetadata=True, and
    # targets the cluster's OWN arn/secret/db (not the cache DB).
    for c in rds.execute_statement.call_args_list:
        kw = c.kwargs
        assert kw["sql"].startswith("/* source=dbops-live */")
        assert kw["includeResultMetadata"] is True
        assert kw["resourceArn"] == _PG_CLUSTER["cluster_arn"]
        assert kw["database"] == "appdb"


def test_live_activity_buffers(monkeypatch):
    rds = _mock_rds()
    _patch_pg(monkeypatch, rds)
    out = handler._live_activity("prod-pg", buffers=True)

    assert out["available"] is True
    bc = out["buffercache"]
    assert bc["used"] == 8000
    assert bc["total"] == 16384
    assert bc["top_relations"][0]["relation"] == "orders"


def test_live_activity_non_pg_not_applicable(monkeypatch):
    rds = _mock_rds()
    _patch_pg(monkeypatch, rds, engine="dynamodb", cluster=None)
    out = handler._live_activity("some-ddb")

    assert out["available"] is False
    assert out["not_applicable"] is True
    assert "PostgreSQL" in out["reason"]
    rds.execute_statement.assert_not_called()  # no DB hit for non-PG


def test_live_activity_mysql_out_of_scope(monkeypatch):
    rds = _mock_rds()
    _patch_pg(monkeypatch, rds, engine="aurora-mysql")
    out = handler._live_activity("prod-mysql")

    assert out["available"] is False
    assert out["not_applicable"] is True
    rds.execute_statement.assert_not_called()


def test_live_activity_data_api_fault_no_leak(monkeypatch):
    rds = MagicMock()
    secret = "arn:aws:secretsmanager:ap-northeast-2:123:secret:prod-pg-SECRET"
    rds.execute_statement.side_effect = Exception(f"HttpEndpointNotEnabled {secret}")
    _patch_pg(monkeypatch, rds)
    out = handler._live_activity("prod-pg")

    assert out["available"] is False
    assert "RDS Data API" in out["reason"]
    # No str(e) — the exception's ARN-bearing message must not reach the client.
    assert secret not in json.dumps(out, ensure_ascii=False)
    assert "HttpEndpointNotEnabled" not in json.dumps(out, ensure_ascii=False)


def test_live_activity_missing_data_api(monkeypatch):
    """Registry row without cluster_arn/secret_arn → friendly unavailable, no call."""
    rds = _mock_rds()
    _patch_pg(monkeypatch, rds, cluster={"cluster_id": "prod-pg"})
    out = handler._live_activity("prod-pg")

    assert out["available"] is False
    assert "RDS Data API" in out["reason"]
    rds.execute_statement.assert_not_called()


def test_live_activity_throttle(monkeypatch):
    """Two rapid endpoint calls within the min-interval hit the target once —
    the second is served from the throttle cache (concurrent viewers can't
    multiply DB load)."""
    handler._LIVE_CACHE.clear()
    rds = _mock_rds()
    _patch_pg(monkeypatch, rds)
    # lambda_handler builds a cache-query via _rds_data() → also our mock; the
    # live-activity route never invokes it, so it doesn't touch the count.
    monkeypatch.setattr(handler, "_rds_data", lambda: rds)

    event = {
        "rawPath": "/api/dashboard/prod-pg/live-activity",
        "pathParameters": {"cluster_id": "prod-pg"},
        "queryStringParameters": {},
        "headers": {},
    }
    r1 = handler.lambda_handler(event, {})
    n1 = rds.execute_statement.call_count
    r2 = handler.lambda_handler(event, {})
    n2 = rds.execute_statement.call_count

    assert r1["statusCode"] == 200 and r2["statusCode"] == 200
    assert n1 == 3  # sessions + blocking + counters
    assert n2 == n1  # second call added zero target queries (throttled)
    body = json.loads(r2["body"])
    assert body["available"] is True

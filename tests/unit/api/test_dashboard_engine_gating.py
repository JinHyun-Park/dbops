"""Tests for engine-family gating on dashboard RDS-live + health-findings endpoints (Task 8).

Gated endpoints:
  - /topology          → _topology(cluster_id)        — calls rds.describe_db_clusters
  - /backups           → _backups(cluster_id)          — calls rds.describe_db_clusters
  - /capacity-forecast → _capacity_forecast(query, ..) — uses Aurora-cache SQL, not RDS-live
  - /health-findings   → _health_findings(query, ..)   — SQL query against cache DB findings table

Non-relational (e.g. dynamodb) clusters must:
  - Never trigger rds.describe_db_clusters for topology/backups.
  - Return not_applicable=True for topology/backups.
  - Return empty findings for health-findings.
  - Return not_applicable=True for capacity-forecast.

Relational (aurora-postgresql) clusters must reach the normal path.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module loading — push api/dashboard on sys.path so engine_family resolves
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_eg", _PATH)
handler = importlib.util.module_from_spec(_spec)

# Stub env vars needed at module import time
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec.loader.exec_module(handler)

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def _make_query_fn(rows=None):
    """Return a dummy query callable that always returns `rows`."""
    def _q(sql, params=None):
        return rows or []
    return _q


# ---------------------------------------------------------------------------
# Helper: build a minimal RDS describe_db_clusters mock response
# ---------------------------------------------------------------------------

def _rds_describe_response():
    return {
        "DBClusters": [{
            "DBClusterIdentifier": "prod-pg",
            "Engine": "aurora-postgresql",
            "EngineVersion": "15.4",
            "Status": "available",
            "DBClusterMembers": [],
            "Endpoint": "prod-pg.cluster-xxx.ap-northeast-2.rds.amazonaws.com",
            "ReaderEndpoint": "prod-pg.cluster-ro-xxx.ap-northeast-2.rds.amazonaws.com",
            "MultiAZ": True,
        }]
    }


# ===========================================================================
# 1. Topology — non-relational (dynamodb) → not_applicable, RDS never called
# ===========================================================================

def test_topology_dynamodb_returns_not_applicable_no_rds_call(monkeypatch):
    """_topology for a dynamodb cluster must return not_applicable=True without
    calling rds.describe_db_clusters."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_rds = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._topology("ddb-abc123")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    mock_rds.describe_db_clusters.assert_not_called()


# ===========================================================================
# 2. Backups — non-relational (dynamodb) → not_applicable, RDS never called
# ===========================================================================

def test_backups_dynamodb_returns_not_applicable_no_rds_call(monkeypatch):
    """_backups for a dynamodb cluster must return not_applicable=True without
    calling rds.describe_db_clusters."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_rds = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._backups("ddb-abc123")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    mock_rds.describe_db_clusters.assert_not_called()


# ===========================================================================
# 3. Capacity-forecast — non-relational → not_applicable, no SQL executed
# ===========================================================================

def test_capacity_forecast_dynamodb_returns_not_applicable(monkeypatch):
    """_capacity_forecast for a dynamodb cluster must return not_applicable=True."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return []

    result = handler._capacity_forecast(_spy_query, "ddb-abc123", "storage_bytes", 30)

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    # The SQL regression query must NOT have been executed
    assert not query_called, "SQL query should not be executed for non-relational clusters"


# ===========================================================================
# 4. Health-findings — non-relational → empty findings, no error
# ===========================================================================

def test_health_findings_dynamodb_returns_empty(monkeypatch):
    """_health_findings for a dynamodb cluster must return empty findings list
    without querying the DB."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return [{"id": "x", "check_type": "health", "severity": "warning",
                 "subject": "s", "value_str": "v", "threshold_str": "t",
                 "recommendation": "r", "details": {}, "snapshot_time": "2026-06-01T00:00:00Z"}]

    result = handler._health_findings(_spy_query, "ddb-abc123")

    assert result["findings"] == []
    assert result["counts"] == {"critical": 0, "warning": 0, "info": 0}
    assert result["snapshot_time"] is None
    assert result["cluster_id"] == "ddb-abc123"
    # SQL must NOT have been executed for non-relational
    assert not query_called, "SQL query should not be executed for non-relational clusters"


# ===========================================================================
# 5. Relational (aurora-postgresql) — all endpoints reach normal path
# ===========================================================================

def test_topology_relational_calls_describe_db_clusters(monkeypatch):
    """_topology for an aurora-postgresql cluster must call rds.describe_db_clusters."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = _rds_describe_response()
    mock_rds.describe_db_instances.return_value = {"DBInstances": []}

    mock_cw = MagicMock()
    mock_cw.get_metric_statistics.return_value = {"Datapoints": []}

    def _mock_client(service, **kwargs):
        if service == "rds":
            return mock_rds
        if service == "cloudwatch":
            return mock_cw
        return MagicMock()

    mock_session = MagicMock()
    mock_session.client.side_effect = _mock_client
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._topology("prod-pg")

    assert result.get("not_applicable") is None or result.get("not_applicable") is False
    mock_rds.describe_db_clusters.assert_called_once()


def test_backups_relational_calls_describe_db_clusters(monkeypatch):
    """_backups for an aurora-postgresql cluster must call rds.describe_db_clusters."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = _rds_describe_response()
    mock_rds.describe_db_cluster_snapshots.return_value = {"DBClusterSnapshots": []}

    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._backups("prod-pg")

    assert result.get("not_applicable") is None or result.get("not_applicable") is False
    mock_rds.describe_db_clusters.assert_called_once()


def test_capacity_forecast_relational_executes_sql(monkeypatch):
    """_capacity_forecast for an aurora-postgresql cluster must execute SQL."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return [{"slope": 0.0, "latest": 1000.0, "first_ts": None, "last_ts": None, "samples": 10}]

    result = handler._capacity_forecast(_spy_query, "prod-pg", "storage_bytes", 30)

    assert result.get("not_applicable") is None or result.get("not_applicable") is False
    assert len(query_called) > 0, "SQL query must be executed for relational clusters"


def test_health_findings_relational_returns_data(monkeypatch):
    """_health_findings for an aurora-postgresql cluster must execute SQL and return rows."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    fake_row = {
        "id": "f1", "check_type": "health", "severity": "warning",
        "subject": "bloat", "value_str": "40%", "threshold_str": "30%",
        "recommendation": "VACUUM", "details": {}, "snapshot_time": "2026-06-01T00:00:00Z",
    }

    def _spy_query(sql, params=None):
        return [fake_row]

    result = handler._health_findings(_spy_query, "prod-pg")

    assert len(result["findings"]) == 1
    assert result["counts"]["warning"] == 1
    assert result["snapshot_time"] == "2026-06-01T00:00:00Z"


# ===========================================================================
# 6. _registry_engine returns None (registry lookup failure) → fail closed
# ===========================================================================

def test_topology_registry_unavailable_fail_closed(monkeypatch):
    """When _registry_engine returns None (lookup failure), _topology must
    fail closed: return not_applicable + registry_unavailable, NO RDS client."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_rds = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._topology("some-cluster")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_rds.describe_db_clusters.assert_not_called()


def test_backups_registry_unavailable_fail_closed(monkeypatch):
    """When _registry_engine returns None, _backups must fail closed."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_rds = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._backups("some-cluster")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_rds.describe_db_clusters.assert_not_called()


def test_capacity_forecast_registry_unavailable_fail_closed(monkeypatch):
    """When _registry_engine returns None, _capacity_forecast must fail closed."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return []

    result = handler._capacity_forecast(_spy_query, "some-cluster", "storage_bytes", 30)

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    assert not query_called


def test_health_findings_registry_unavailable_fail_closed(monkeypatch):
    """When _registry_engine returns None, _health_findings must fail closed (empty)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return []

    result = handler._health_findings(_spy_query, "some-cluster")

    assert result["findings"] == []
    assert not query_called


# ===========================================================================
# 7. _schema_graph / _redundant_indexes / _table_indexes / _log_insights
#    — non-relational (dynamodb) → not_applicable, no rds-data/logs client
# ===========================================================================

def test_schema_graph_dynamodb_not_applicable(monkeypatch):
    """_schema_graph for a dynamodb cluster must return not_applicable without
    creating an rds-data client."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._schema_graph("ddb-abc123", "public")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    assert result.get("tables") == []
    assert result.get("edges") == []
    # rds-data client must NOT have been created
    mock_boto3.client.assert_not_called()


def test_schema_graph_registry_unavailable_fail_closed(monkeypatch):
    """_schema_graph with registry_unavailable must also return not_applicable."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._schema_graph("some-cluster", "public")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_boto3.client.assert_not_called()


def test_redundant_indexes_dynamodb_not_applicable(monkeypatch):
    """_redundant_indexes for a dynamodb cluster must return not_applicable."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._redundant_indexes("ddb-abc123")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    assert result.get("candidates") == []
    mock_boto3.client.assert_not_called()


def test_redundant_indexes_registry_unavailable_fail_closed(monkeypatch):
    """_redundant_indexes with registry_unavailable must fail closed."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._redundant_indexes("some-cluster")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_boto3.client.assert_not_called()


def test_table_indexes_dynamodb_not_applicable(monkeypatch):
    """_table_indexes for a dynamodb cluster must return not_applicable."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._table_indexes("ddb-abc123", "main", "orders")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    assert result.get("indexes") == []
    mock_boto3.client.assert_not_called()


def test_table_indexes_registry_unavailable_fail_closed(monkeypatch):
    """_table_indexes with registry_unavailable must fail closed."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._table_indexes("some-cluster", "main", "orders")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_boto3.client.assert_not_called()


def test_log_insights_dynamodb_not_applicable(monkeypatch):
    """_log_insights for a dynamodb cluster must return not_applicable without
    creating a CloudWatch Logs client or building any log-group paths."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._log_insights("ddb-abc123", 1, "error")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    assert result.get("entries") == []
    mock_boto3.client.assert_not_called()


def test_log_insights_registry_unavailable_fail_closed(monkeypatch):
    """_log_insights with registry_unavailable must fail closed."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", mock_boto3)

    result = handler._log_insights("some-cluster", 1, "error")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_boto3.client.assert_not_called()

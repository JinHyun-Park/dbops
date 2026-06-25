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
# 2. Backups — non-relational now returns read-only posture (no RDS call)
#    (backup-visibility-multiengine spec: dynamodb=PITR/on-demand, docdb=snapshots)
# ===========================================================================

def test_backups_dynamodb_returns_pitr_and_ondemand_no_rds(monkeypatch):
    """_backups for a dynamodb cluster reads PITR + on-demand backups via the
    dynamodb client (NOT rds), using the registry resource_name as the table."""
    from datetime import datetime, timezone

    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")
    monkeypatch.setattr(
        handler, "_lookup_cluster",
        lambda cid: {"resource_name": "orders-table", "region": "ap-northeast-2"},
    )

    mock_ddb = MagicMock()
    mock_ddb.describe_continuous_backups.return_value = {
        "ContinuousBackupsDescription": {
            "PointInTimeRecoveryDescription": {
                "PointInTimeRecoveryStatus": "ENABLED",
                "EarliestRestorableDateTime": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "LatestRestorableDateTime": datetime(2026, 6, 2, tzinfo=timezone.utc),
            }
        }
    }
    mock_ddb.list_backups.return_value = {
        "BackupSummaries": [{
            "BackupName": "manual-1", "BackupStatus": "AVAILABLE",
            "BackupCreationDateTime": datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
            "BackupSizeBytes": 1024, "BackupType": "USER",
        }]
    }
    mock_rds = MagicMock()

    def _client(service, **kwargs):
        return mock_ddb if service == "dynamodb" else mock_rds

    mock_session = MagicMock()
    mock_session.client.side_effect = _client
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._backups("ddb-abc123")

    assert result.get("engine_family") == "dynamodb"
    assert result.get("not_applicable") is not True
    assert result["pitr_enabled"] is True
    assert result["table_name"] == "orders-table"
    assert result["on_demand_count"] == 1
    assert result["on_demand_backups"][0]["name"] == "manual-1"
    mock_ddb.describe_continuous_backups.assert_called_once()
    mock_rds.describe_db_clusters.assert_not_called()


def test_backups_documentdb_returns_snapshots_via_docdb_client(monkeypatch):
    """_backups for a documentdb cluster reads snapshots + window via the docdb
    client (mirrors the RDS cluster-snapshot API), not the rds client."""
    from datetime import datetime, timezone

    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "docdb")

    mock_docdb = MagicMock()
    mock_docdb.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Engine": "docdb", "Status": "available",
            "BackupRetentionPeriod": 1, "PreferredBackupWindow": "15:05-15:35",
            "EarliestRestorableTime": datetime(2026, 6, 11, 12, tzinfo=timezone.utc),
            "LatestRestorableTime": datetime(2026, 6, 12, 2, tzinfo=timezone.utc),
        }]
    }
    mock_docdb.describe_db_cluster_snapshots.return_value = {
        "DBClusterSnapshots": [{
            "DBClusterSnapshotIdentifier": "rds:dbops-docdb-test-2026-06-11-15-06",
            "SnapshotType": "automated", "Status": "available",
            "SnapshotCreateTime": datetime(2026, 6, 11, 15, 7, tzinfo=timezone.utc),
            "EngineVersion": "5.0.0",
        }]
    }
    mock_rds = MagicMock()

    def _client(service, **kwargs):
        return mock_docdb if service == "docdb" else mock_rds

    mock_session = MagicMock()
    mock_session.client.side_effect = _client
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._backups("dbops-docdb-test")

    assert result.get("engine_family") == "documentdb"
    assert result.get("not_applicable") is not True
    assert result["backup_retention_days"] == 1
    assert result["preferred_backup_window"] == "15:05-15:35"
    assert result["snapshot_count"] == 1
    assert result["snapshots"][0]["type"] == "automated"
    mock_docdb.describe_db_cluster_snapshots.assert_called_once()
    mock_rds.describe_db_clusters.assert_not_called()


# ===========================================================================
# 3. Capacity-forecast — non-relational → not_applicable, no SQL executed
# ===========================================================================

def test_capacity_forecast_dynamodb_invalid_metric_returns_not_applicable(monkeypatch):
    """_capacity_forecast for a dynamodb cluster asked for a metric OUTSIDE its
    engine family (storage_bytes is relational/docdb-only) must return
    not_applicable=True without executing any SQL — the metric is not valid
    for DynamoDB, whose capacity forecasts are consumed_rcu/consumed_wcu."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return []

    result = handler._capacity_forecast(_spy_query, "ddb-abc123", "storage_bytes", 30)

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    # The SQL regression query must NOT have been executed for an invalid metric
    assert not query_called, "SQL query should not run for a metric outside the engine family"


# ===========================================================================
# 4. Health-findings — dynamodb now has findings capability → returns data
# ===========================================================================

def test_health_findings_dynamodb_returns_data(monkeypatch):
    """_health_findings for a dynamodb cluster must now execute SQL and return
    ddb_* findings because CAPABILITIES["dynamodb"]["findings"] = {"ddb"}.

    This test was previously 'returns_empty' — updated when DynamoDB findings
    support was added (Part B capability flag spec)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    fake_row = {
        "id": "x", "check_type": "ddb_throttling", "severity": "warning",
        "subject": "orders", "value_str": "12/min", "threshold_str": "0",
        "recommendation": "Increase RCU.", "details": {}, "snapshot_time": "2026-06-01T00:00:00Z",
    }

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return [fake_row]

    result = handler._health_findings(_spy_query, "ddb-abc123")

    # DynamoDB now has findings — SQL must be executed and data returned.
    assert len(result["findings"]) == 1
    assert result["findings"][0]["check_type"] == "ddb_throttling"
    assert result["counts"]["warning"] == 1
    assert result["snapshot_time"] == "2026-06-01T00:00:00Z"
    assert result["cluster_id"] == "ddb-abc123"
    assert len(query_called) > 0, "SQL must be executed for dynamodb (findings capability enabled)"


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


def test_topology_documentdb_uses_docdb_client(monkeypatch):
    """_topology for a documentdb cluster reads members via the docdb client
    (DocDB mirrors the RDS cluster-member API), not rds."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "docdb")

    mock_docdb = MagicMock()
    mock_docdb.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Engine": "docdb", "Status": "available",
            "EngineVersion": "5.0.0",
            "DBClusterMembers": [
                {"IsClusterWriter": True, "DBInstanceIdentifier": "dbops-docdb-test-1",
                 "PromotionTier": 0},
            ],
        }]
    }
    mock_docdb.describe_db_instances.return_value = {
        "DBInstances": [{
            "DBInstanceIdentifier": "dbops-docdb-test-1",
            "DBInstanceClass": "db.t3.medium", "DBInstanceStatus": "available",
            "EngineVersion": "5.0.0", "AvailabilityZone": "ap-northeast-2a",
        }]
    }
    mock_rds = MagicMock()
    mock_cw = MagicMock()

    def _client(service, **kwargs):
        if service == "docdb":
            return mock_docdb
        if service == "cloudwatch":
            return mock_cw
        return mock_rds

    mock_session = MagicMock()
    mock_session.client.side_effect = _client
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._topology("dbops-docdb-test")

    assert result.get("engine_family") == "documentdb"
    assert result.get("not_applicable") is not True
    assert result["members_count"] == 1
    assert result["members"][0]["is_writer"] is True
    assert result["members"][0]["instance_class"] == "db.t3.medium"
    mock_docdb.describe_db_clusters.assert_called_once()
    mock_rds.describe_db_clusters.assert_not_called()


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


def test_capacity_forecast_documentdb_db_connections_resolves_limit_from_metric(monkeypatch):
    """_capacity_forecast for a documentdb cluster forecasting db_connections must
    resolve the limit from the LATEST db_connections_limit metric (NOT
    cluster_settings.max_connections, which DocDB has no row for)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "docdb")

    seen = {}

    def _spy_query(sql, params=None):
        if "REGR_SLOPE" in sql:
            # Growing connections, well below the limit.
            return [{"slope": 1.0, "latest": 100.0, "first_ts": None,
                     "last_ts": None, "samples": 30}]
        if "db_connections_limit" in sql:
            seen["limit_query"] = sql
            # DocDB DatabaseConnectionsLimit (latest datapoint).
            return [{"value": 1700.0}]
        # max_connections must NOT be consulted for documentdb.
        seen["unexpected"] = sql
        return []

    result = handler._capacity_forecast(_spy_query, "docdb-abc123", "db_connections", 30)

    assert result.get("not_applicable") is not True
    assert result["engine_family"] == "documentdb"
    assert result["limit"] == 1700.0
    assert result["current"] == 100.0
    assert "limit_query" in seen, "db_connections_limit metric must be queried"
    assert "max_connections" not in seen.get("limit_query", "")
    assert "unexpected" not in seen, "cluster_settings must not be consulted for docdb"


def test_capacity_forecast_dynamodb_consumed_wcu_resolves_limit_from_provisioned(monkeypatch):
    """_capacity_forecast for a dynamodb cluster forecasting consumed_wcu must
    resolve the limit as the LATEST provisioned_wcu × 60 (per-minute ceiling)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    def _spy_query(sql, params=None):
        if "REGR_SLOPE" in sql:
            return [{"slope": 5.0, "latest": 1000.0, "first_ts": None,
                     "last_ts": None, "samples": 30}]
        # Limit-resolution query is parameterized (metric_type = :pm).
        if (params or {}).get("pm") == "provisioned_wcu":
            return [{"value": 50.0}]  # 50 WCU/s provisioned
        return []

    result = handler._capacity_forecast(_spy_query, "ddb-abc123", "consumed_wcu", 30)

    assert result.get("not_applicable") is not True
    assert result["engine_family"] == "dynamodb"
    assert result["limit"] == 50.0 * 60.0  # 3000 WCU/min
    assert result["current"] == 1000.0


def test_capacity_forecast_dynamodb_ondemand_no_provisioned_not_applicable(monkeypatch):
    """_capacity_forecast for a dynamodb cluster with NO provisioned_* datapoint
    (on-demand table) must return not_applicable — there is no ceiling to
    forecast toward."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    def _spy_query(sql, params=None):
        if "REGR_SLOPE" in sql:
            return [{"slope": 5.0, "latest": 1000.0, "first_ts": None,
                     "last_ts": None, "samples": 30}]
        # provisioned_rcu query returns no rows → on-demand
        return []

    result = handler._capacity_forecast(_spy_query, "ddb-abc123", "consumed_rcu", 30)

    assert result.get("not_applicable") is True
    assert result["engine_family"] == "dynamodb"
    assert "on-demand" in result.get("reason", "")


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


# ===========================================================================
# 8. _overview cold-resource registry fallback
# ===========================================================================

def test_overview_cold_resource_falls_back_to_registry(monkeypatch):
    """When cluster_meta has no row but the registry has the cluster,
    _overview must return a cluster stub with engine and engine_family
    from the registry so the frontend can gate on engine_family correctly."""
    # Stub _lookup_cluster to return a registry row
    monkeypatch.setattr(
        handler,
        "_lookup_cluster",
        lambda cid: {"cluster_id": cid, "engine": "dynamodb"},
    )

    # query always returns empty — simulates no cluster_meta row and no metrics
    def _empty_query(sql, params=None):
        return []

    result = handler._overview(_empty_query, "ddb-cold-123")

    cluster = result["cluster"]
    assert cluster is not None, "cluster must not be None when registry row exists"
    assert cluster["engine"] == "dynamodb"
    assert cluster["engine_family"] == "dynamodb"
    assert cluster["cluster_id"] == "ddb-cold-123"
    # Other fields should still be present and empty
    assert result["metrics"] == []
    assert result["top_queries"] == []
    assert result["events"] == []


def test_overview_cold_resource_no_registry_returns_none(monkeypatch):
    """When cluster_meta has no row AND the registry has nothing,
    _overview must return cluster=None (unchanged behaviour)."""
    monkeypatch.setattr(handler, "_lookup_cluster", lambda cid: {})

    def _empty_query(sql, params=None):
        return []

    result = handler._overview(_empty_query, "ghost-cluster")

    assert result["cluster"] is None


def test_overview_hot_resource_relational_unchanged(monkeypatch):
    """When cluster_meta HAS a row, _overview must return that row verbatim
    regardless of registry content — relational path must be unchanged."""
    # Make sure registry is never consulted when meta has data
    lookup_called = []
    monkeypatch.setattr(
        handler,
        "_lookup_cluster",
        lambda cid: lookup_called.append(cid) or {},
    )

    fake_meta_row = {
        "cluster_id": "prod-pg",
        "engine": "aurora-postgresql",
        "status": "available",
    }

    def _query(sql, params=None):
        if "cluster_meta" in sql:
            return [fake_meta_row]
        return []

    result = handler._overview(_query, "prod-pg")

    assert result["cluster"] == fake_meta_row
    # _lookup_cluster must NOT have been called (meta row was found)
    assert lookup_called == []


# ===========================================================================
# 9. _health_findings capability-driven gating (Part B spec)
# ===========================================================================

def test_health_findings_dynamodb_returns_findings_via_capability(monkeypatch):
    """_health_findings for a dynamodb cluster must now return findings because
    CAPABILITIES["dynamodb"]["findings"] = {"ddb"} is non-empty.
    SQL must be executed and rows returned."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    fake_row = {
        "id": "d1", "check_type": "ddb_throttling", "severity": "warning",
        "subject": "orders-table", "value_str": "throttled=12/min",
        "threshold_str": "0", "recommendation": "Increase RCU/WCU provisioned capacity.",
        "details": {}, "snapshot_time": "2026-06-12T00:00:00Z",
    }

    def _spy_query(sql, params=None):
        return [fake_row]

    result = handler._health_findings(_spy_query, "ddb-abc123")

    assert len(result["findings"]) == 1
    assert result["findings"][0]["check_type"] == "ddb_throttling"
    assert result["counts"]["warning"] == 1
    assert result["snapshot_time"] == "2026-06-12T00:00:00Z"
    assert result["cluster_id"] == "ddb-abc123"


def test_health_findings_documentdb_returns_findings_via_capability(monkeypatch):
    """_health_findings for a documentdb cluster must return findings because
    spec #2 (DocumentDB diagnosis) set CAPABILITIES["documentdb"]["findings"]
    = {"docdb"} (non-empty). Mirrors the dynamodb path above."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "docdb")

    fake_row = {
        "id": "x", "check_type": "docdb_connection_saturation", "severity": "warning",
        "subject": "connections", "value_str": "85%", "threshold_str": "80%",
        "recommendation": "Use connection pooling / raise instance class.",
        "details": {}, "snapshot_time": "2026-06-12T00:00:00Z",
    }

    def _spy_query(sql, params=None):
        return [fake_row]

    result = handler._health_findings(_spy_query, "docdb-abc123")

    assert len(result["findings"]) == 1
    assert result["findings"][0]["check_type"] == "docdb_connection_saturation"
    assert result["counts"]["warning"] == 1
    assert result["snapshot_time"] == "2026-06-12T00:00:00Z"
    assert result["cluster_id"] == "docdb-abc123"


def test_health_findings_relational_unchanged_with_capability(monkeypatch):
    """_health_findings for relational clusters must still work as before —
    the capability-driven gate must not break the existing relational path."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    fake_row = {
        "id": "f1", "check_type": "txid_age", "severity": "critical",
        "subject": "public.orders", "value_str": "2.1B", "threshold_str": "2.0B",
        "recommendation": "Run VACUUM FREEZE.", "details": {}, "snapshot_time": "2026-06-12T01:00:00Z",
    }

    def _spy_query(sql, params=None):
        return [fake_row]

    result = handler._health_findings(_spy_query, "prod-pg")

    assert len(result["findings"]) == 1
    assert result["counts"]["critical"] == 1
    assert result["snapshot_time"] == "2026-06-12T01:00:00Z"


def test_health_findings_registry_unavailable_returns_registry_unavailable_flag(monkeypatch):
    """When _registry_engine returns None, _health_findings must return
    registry_unavailable=True and empty findings (fail-closed)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    query_called = []

    def _spy_query(sql, params=None):
        query_called.append(sql)
        return []

    result = handler._health_findings(_spy_query, "some-cluster")

    assert result["findings"] == []
    assert result.get("registry_unavailable") is True
    assert not query_called


# ===========================================================================
# 10. _engine_config — engine-level config panel (read-only)
#     DocumentDB cluster settings + DynamoDB table settings the overview
#     panels don't already show. Relational → not_applicable (has SettingsPanel).
# ===========================================================================

def test_engine_config_documentdb_returns_cluster_settings_via_docdb_client(monkeypatch):
    """_engine_config for a documentdb cluster reads maintenance window,
    deletion protection, encryption, parameter group, and retention via the
    docdb client (mirrors the RDS cluster API), not the rds client."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "docdb")

    mock_docdb = MagicMock()
    mock_docdb.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Engine": "docdb", "Status": "available",
            "PreferredMaintenanceWindow": "sun:18:00-sun:18:30",
            "DeletionProtection": True,
            "StorageEncrypted": True,
            "DBClusterParameterGroup": "default.docdb5.0",
            "BackupRetentionPeriod": 7,
        }]
    }
    mock_rds = MagicMock()

    def _client(service, **kwargs):
        return mock_docdb if service == "docdb" else mock_rds

    mock_session = MagicMock()
    mock_session.client.side_effect = _client
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("dbops-docdb-test")

    assert result.get("engine_family") == "documentdb"
    assert result.get("not_applicable") is not True
    assert result["preferred_maintenance_window"] == "sun:18:00-sun:18:30"
    assert result["deletion_protection"] is True
    assert result["storage_encrypted"] is True
    assert result["db_cluster_parameter_group"] == "default.docdb5.0"
    assert result["backup_retention_period"] == 7
    mock_docdb.describe_db_clusters.assert_called_once()
    mock_rds.describe_db_clusters.assert_not_called()


def test_engine_config_dynamodb_returns_ttl_stream_class_via_dynamodb_client(monkeypatch):
    """_engine_config for a dynamodb cluster reads table class, deletion
    protection, SSE, streams, and TTL via the dynamodb client (describe_table +
    describe_time_to_live), using the registry resource_name as the table."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")
    monkeypatch.setattr(
        handler, "_lookup_cluster",
        lambda cid: {"resource_name": "orders-table", "region": "ap-northeast-2"},
    )

    mock_ddb = MagicMock()
    mock_ddb.describe_table.return_value = {
        "Table": {
            "TableClassSummary": {"TableClass": "STANDARD_INFREQUENT_ACCESS"},
            "DeletionProtectionEnabled": True,
            "SSEDescription": {"SSEType": "KMS", "Status": "ENABLED"},
            "StreamSpecification": {
                "StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES",
            },
        }
    }
    mock_ddb.describe_time_to_live.return_value = {
        "TimeToLiveDescription": {
            "TimeToLiveStatus": "ENABLED", "AttributeName": "expires_at",
        }
    }
    mock_rds = MagicMock()

    def _client(service, **kwargs):
        return mock_ddb if service == "dynamodb" else mock_rds

    mock_session = MagicMock()
    mock_session.client.side_effect = _client
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("ddb-abc123")

    assert result.get("engine_family") == "dynamodb"
    assert result.get("not_applicable") is not True
    assert result["table_name"] == "orders-table"
    assert result["table_class"] == "STANDARD_INFREQUENT_ACCESS"
    assert result["deletion_protection_enabled"] is True
    assert result["sse_type"] == "KMS"
    assert result["sse_status"] == "ENABLED"
    assert result["stream_enabled"] is True
    assert result["stream_view_type"] == "NEW_AND_OLD_IMAGES"
    assert result["ttl_status"] == "ENABLED"
    assert result["ttl_attribute_name"] == "expires_at"
    mock_ddb.describe_table.assert_called_once()
    mock_ddb.describe_time_to_live.assert_called_once()
    mock_rds.describe_db_clusters.assert_not_called()


def test_engine_config_dynamodb_defaults_table_class_when_absent(monkeypatch):
    """_engine_config for a dynamodb table with no TableClassSummary must
    default to STANDARD (AWS omits the summary for the default class)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")
    monkeypatch.setattr(
        handler, "_lookup_cluster",
        lambda cid: {"resource_name": "orders-table"},
    )

    mock_ddb = MagicMock()
    mock_ddb.describe_table.return_value = {"Table": {}}
    mock_ddb.describe_time_to_live.return_value = {
        "TimeToLiveDescription": {"TimeToLiveStatus": "DISABLED"}
    }

    mock_session = MagicMock()
    mock_session.client.return_value = mock_ddb
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("ddb-abc123")

    assert result["table_class"] == "STANDARD"
    assert result["deletion_protection_enabled"] is False
    assert result["stream_enabled"] is False
    assert result["ttl_status"] == "DISABLED"


def test_engine_config_relational_returns_not_applicable(monkeypatch):
    """_engine_config for a relational cluster must return not_applicable
    (relational has the SettingsPanel) without calling any AWS client."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("prod-pg")

    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "relational"
    mock_rds.describe_db_clusters.assert_not_called()


def test_engine_config_registry_unavailable_fail_closed(monkeypatch):
    """When _registry_engine returns None, _engine_config must fail closed:
    not_applicable + registry_unavailable, NO AWS client."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_session = MagicMock()
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("some-cluster")

    assert result.get("not_applicable") is True
    assert result.get("registry_unavailable") is True
    mock_session.client.assert_not_called()


def test_engine_config_documentdb_no_raw_boto_leak_on_error(monkeypatch):
    """_engine_config for a documentdb cluster whose describe raises a generic
    boto error must return a friendly Korean message, not the raw fault."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "docdb")

    mock_docdb = MagicMock()
    mock_docdb.describe_db_clusters.side_effect = RuntimeError(
        "botocore.exceptions.ClientError: AccessDenied raw secret leak"
    )

    mock_session = MagicMock()
    mock_session.client.return_value = mock_docdb
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("dbops-docdb-test")

    assert result.get("engine_family") == "documentdb"
    assert "raw secret leak" not in result.get("error", "")
    assert "AccessDenied" not in result.get("error", "")
    assert "구성" in result.get("error", "")


def test_engine_config_dynamodb_no_raw_boto_leak_on_error(monkeypatch):
    """_engine_config for a dynamodb table whose describe_table raises a generic
    boto error must return a friendly Korean message, not the raw fault."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")
    monkeypatch.setattr(
        handler, "_lookup_cluster",
        lambda cid: {"resource_name": "orders-table"},
    )

    mock_ddb = MagicMock()
    mock_ddb.describe_table.side_effect = RuntimeError(
        "botocore.exceptions.ClientError: AccessDenied raw secret leak"
    )

    mock_session = MagicMock()
    mock_session.client.return_value = mock_ddb
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("ddb-abc123")

    assert result.get("engine_family") == "dynamodb"
    assert "raw secret leak" not in result.get("error", "")
    assert "AccessDenied" not in result.get("error", "")
    assert "구성" in result.get("error", "")


# ===========================================================================
# Engine-config — ElastiCache (replication group): parameter group + key
# params (eviction policy) + maintenance/snapshot/encryption/auth/failover.
# ===========================================================================

def test_engine_config_elasticache_replication_group(monkeypatch):
    """_engine_config for an ElastiCache replication group surfaces RG posture +
    reads the maintenance window / parameter group from a MEMBER NODE (not the
    RG id) + the key parameter values (maxmemory-policy), filtering to the
    allowlist."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "redis")
    monkeypatch.setattr(
        handler, "_lookup_cluster",
        lambda cid: {"resource_name": "my-valkey", "region": "ap-northeast-2"},
    )

    mock_ec = MagicMock()
    mock_ec.describe_replication_groups.return_value = {
        "ReplicationGroups": [{
            "ReplicationGroupId": "my-valkey",
            "SnapshotRetentionLimit": 3,
            "SnapshotWindow": "03:00-04:00",
            "AtRestEncryptionEnabled": False,  # legacy flag false…
            "StorageEncryptionType": "sse-elasticache",  # …but encrypted by type
            "TransitEncryptionEnabled": False,
            "AuthTokenEnabled": False,  # no token…
            "UserGroupIds": ["dbops-rbac"],  # …but authenticated via RBAC
            "AutomaticFailover": "enabled",
            "MultiAZ": "enabled",
            "MemberClusters": ["my-valkey-001", "my-valkey-002"],
        }]
    }
    mock_ec.describe_cache_clusters.return_value = {
        "CacheClusters": [{
            "CacheClusterId": "my-valkey-001",
            "PreferredMaintenanceWindow": "sun:05:00-sun:06:00",
            "CacheParameterGroup": {"CacheParameterGroupName": "default.valkey8"},
        }]
    }
    mock_ec.get_paginator.return_value.paginate.return_value = [{
        "Parameters": [
            {"ParameterName": "maxmemory-policy", "ParameterValue": "volatile-lru"},
            {"ParameterName": "timeout", "ParameterValue": "0"},
            {"ParameterName": "not-in-allowlist", "ParameterValue": "x"},
        ]
    }]

    mock_session = MagicMock()
    mock_session.client.return_value = mock_ec
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("my-valkey")

    assert result.get("engine_family") == "elasticache"
    assert result.get("not_applicable") is not True
    assert result["parameter_group"] == "default.valkey8"
    assert result["preferred_maintenance_window"] == "sun:05:00-sun:06:00"
    assert result["snapshot_retention_limit"] == 3
    # StorageEncryptionType drives at-rest posture even when the legacy boolean
    # flag is False (Codex finding) — and the type itself is surfaced.
    assert result["at_rest_encryption_enabled"] is True
    assert result["storage_encryption_type"] == "sse-elasticache"
    assert result["transit_encryption_enabled"] is False
    # RBAC user groups count as authenticated even without a legacy auth token.
    assert result["auth_enabled"] is False
    assert result["rbac_enabled"] is True
    assert result["automatic_failover"] == "enabled"
    assert result["multi_az"] == "enabled"
    assert result["parameters"]["maxmemory-policy"] == "volatile-lru"
    assert result["parameters"]["timeout"] == "0"
    assert "not-in-allowlist" not in result["parameters"]
    # CloudWatch-dimension lesson, again: maintenance window / PG come from the
    # MEMBER NODE (my-valkey-001), never the replication-group id.
    mock_ec.describe_cache_clusters.assert_called_once_with(CacheClusterId="my-valkey-001")


def test_engine_config_elasticache_not_found_is_friendly(monkeypatch):
    """A missing/unauthorized ElastiCache cluster returns a friendly notice
    (info=True), never a raw boto3 fault string."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "redis")
    monkeypatch.setattr(
        handler, "_lookup_cluster",
        lambda cid: {"resource_name": "ghost", "region": "ap-northeast-2"},
    )
    mock_ec = MagicMock()
    mock_ec.describe_replication_groups.side_effect = Exception(
        "ReplicationGroupNotFoundFault: not found"
    )
    mock_ec.describe_cache_clusters.side_effect = Exception(
        "CacheClusterNotFoundFault: not found"
    )
    mock_session = MagicMock()
    mock_session.client.return_value = mock_ec
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._engine_config("ghost")

    assert result.get("engine_family") == "elasticache"
    assert result.get("info") is True
    assert "NotFoundFault" not in result.get("error", "")
    assert "구성" in result.get("error", "")

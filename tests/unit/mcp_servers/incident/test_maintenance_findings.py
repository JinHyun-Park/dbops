import json
from unittest.mock import MagicMock

import pytest
from mcp_servers.incident.tools.maintenance_findings import get_maintenance_findings_impl
from mcp_servers.shared.models import QueryResult


def _make_row(check_type, severity, subject, value_str, threshold_str, recommendation):
    return {
        "check_type": check_type,
        "severity": severity,
        "subject": subject,
        "value_str": value_str,
        "threshold_str": threshold_str,
        "recommendation": recommendation,
    }


def test_get_maintenance_findings_mixed():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["check_type", "severity", "subject", "value_str", "threshold_str", "recommendation"],
        rows=[
            _make_row("ddb_throttling", "critical", "ReadThrottleEvents", "1250", "0", "Increase RCU or enable auto-scaling"),
            _make_row("ddb_hot_partition", "warning", "PartitionKey: user_id", "850 rps", "500 rps", "Use composite partition key"),
        ],
        row_count=2,
    )
    result = get_maintenance_findings_impl(mock_cache, cluster_id="my-ddb-table")

    assert result["cluster_id"] == "my-ddb-table"
    assert len(result["findings"]) == 2
    assert result["counts"]["critical"] == 1
    assert result["counts"]["warning"] == 1
    assert result["counts"]["info"] == 0

    critical_row = result["findings"][0]
    assert critical_row["check_type"] == "ddb_throttling"
    assert critical_row["severity"] == "critical"
    assert critical_row["recommendation"] == "Increase RCU or enable auto-scaling"

    mock_cache.execute.assert_called_once()
    sql, params = mock_cache.execute.call_args[0]
    assert ":cid" in sql
    assert params["cid"] == "my-ddb-table"


def test_get_maintenance_findings_empty():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=[], rows=[], row_count=0
    )
    result = get_maintenance_findings_impl(mock_cache, cluster_id="no-findings-cluster")

    assert result["cluster_id"] == "no-findings-cluster"
    assert result["findings"] == []
    assert result["counts"] == {"critical": 0, "warning": 0, "info": 0}


def test_get_maintenance_findings_info_only():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["check_type", "severity", "subject", "value_str", "threshold_str", "recommendation"],
        rows=[
            _make_row("pg_vacuum_due", "info", "orders", "dead_tuples=120", "dead_tuples=500", "Schedule VACUUM ANALYZE"),
        ],
        row_count=1,
    )
    result = get_maintenance_findings_impl(mock_cache, cluster_id="aurora-pg-prod")

    assert result["counts"]["info"] == 1
    assert result["counts"]["critical"] == 0
    assert result["counts"]["warning"] == 0
    assert result["findings"][0]["check_type"] == "pg_vacuum_due"


def test_get_maintenance_findings_sql_contains_max_snapshot():
    """SQL must query the latest snapshot only."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    get_maintenance_findings_impl(mock_cache, cluster_id="c1")
    sql, _ = mock_cache.execute.call_args[0]
    assert "MAX(snapshot_time)" in sql
    assert "cluster_health_findings" in sql

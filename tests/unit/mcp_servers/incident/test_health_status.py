import json
from unittest.mock import MagicMock

from mcp_servers.incident.tools.health_status import get_health_status_impl
from mcp_servers.shared.models import QueryResult


def test_health_status_available_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "instance_class"],
            rows=[{"cluster_id": "prod-pg-1", "status": "available", "engine": "aurora-postgresql", "instance_class": "db.r6g.xlarge"}],
            row_count=1,
        ),
        QueryResult(
            columns=["metric_type", "avg_val", "max_val"],
            rows=[
                {"metric_type": "cpu", "avg_val": 25.0, "max_val": 40.0},
                {"metric_type": "connections", "avg_val": 50.0, "max_val": 80.0},
            ],
            row_count=2,
        ),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["health"] == "healthy"
    assert result["cluster_id"] == "prod-pg-1"
    assert len(result["current_metrics"]) == 2
    assert mock_cache.execute.call_count == 2


def test_health_status_modifying_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status"],
            rows=[{"cluster_id": "prod-pg-1", "status": "modifying"}],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["health"] == "warning"


def test_health_status_unknown_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(columns=[], rows=[], row_count=0),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="unknown-cluster")
    assert result["health"] == "critical"


# --- engine-aware extension tests ---

def test_health_status_dynamodb_engine_includes_resource_details():
    resource_details_payload = json.dumps({
        "billing_mode": "PAY_PER_REQUEST",
        "table_status": "ACTIVE",
        "gsi_count": 2,
    })
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "resource_details"],
            rows=[{
                "cluster_id": "my-ddb-table",
                "status": "available",
                "engine": "dynamodb",
                "resource_details": resource_details_payload,
            }],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="my-ddb-table")

    assert result["health"] == "healthy"
    assert result["engine"] == "dynamodb"
    assert isinstance(result["resource_details"], dict)
    assert result["resource_details"]["billing_mode"] == "PAY_PER_REQUEST"
    assert result["resource_details"]["gsi_count"] == 2


def test_health_status_documentdb_engine_includes_resource_details():
    resource_details_payload = json.dumps({
        "num_instances": 3,
        "engine_version": "5.0.0",
    })
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "resource_details"],
            rows=[{
                "cluster_id": "docdb-prod",
                "status": "available",
                "engine": "docdb",
                "resource_details": resource_details_payload,
            }],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="docdb-prod")

    assert result["engine"] == "docdb"
    assert result["resource_details"]["num_instances"] == 3


def test_health_status_relational_engine_unchanged_shape():
    """Aurora clusters must NOT get engine/resource_details injected."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "instance_class"],
            rows=[{
                "cluster_id": "aurora-pg",
                "status": "available",
                "engine": "aurora-postgresql",
                "instance_class": "db.r6g.xlarge",
            }],
            row_count=1,
        ),
        QueryResult(
            columns=["metric_type", "avg_val", "max_val"],
            rows=[{"metric_type": "cpu", "avg_val": 10.0, "max_val": 20.0}],
            row_count=1,
        ),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="aurora-pg")

    assert result["health"] == "healthy"
    assert "engine" not in result
    assert "resource_details" not in result


def test_health_status_resource_details_null_is_safe():
    """resource_details=None must not crash for non-relational engines."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "resource_details"],
            rows=[{
                "cluster_id": "ddb-no-details",
                "status": "available",
                "engine": "dynamodb",
                "resource_details": None,
            }],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="ddb-no-details")

    assert result["engine"] == "dynamodb"
    assert result.get("resource_details") is None

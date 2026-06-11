"""Tests: engine-family guard in execute_sql_impl blocks non-relational clusters.

Non-relational registry rows (DynamoDB, DocumentDB) must return
{"status": "unsupported_engine", ...} and must NOT attempt to build an RDS
Data API target or call rds-data.
"""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DYNAMODB_ROW = {
    "cluster_id": "ddb-abc",
    "engine": "dynamodb",
    "engine_family": "dynamodb",
    # Intentionally NO cluster_arn / secret_arn — a non-relational row never
    # has these. The guard must fire before any target-resolution check.
}

_DOCDB_ROW = {
    "cluster_id": "docdb-xyz",
    "engine": "docdb",
    # engine_family absent — must be derived from engine string
    "cluster_arn": "arn:aws:rds:us-east-1:123456789012:cluster:docdb-xyz",
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:docdb-xyz",
}

_AURORA_ROW = {
    "cluster_id": "prod-pg-1",
    "engine": "aurora-postgresql",
    "engine_family": "relational",
    "cluster_arn": "arn:aws:rds:us-east-1:123456789012:cluster:prod-pg-1",
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-pg-1",
    "db_name": "mydb",
}


# ---------------------------------------------------------------------------
# engine_family helper unit tests
# ---------------------------------------------------------------------------

def test_engine_family_dynamodb():
    from mcp_servers.shared.engine_family import engine_family
    assert engine_family("dynamodb") == "dynamodb"
    assert engine_family("DynamoDB") == "dynamodb"


def test_engine_family_docdb():
    from mcp_servers.shared.engine_family import engine_family
    assert engine_family("docdb") == "documentdb"
    assert engine_family("documentdb") == "documentdb"


def test_engine_family_relational():
    from mcp_servers.shared.engine_family import engine_family
    assert engine_family("aurora-postgresql") == "relational"
    assert engine_family("aurora-mysql") == "relational"
    assert engine_family("") == "relational"


# ---------------------------------------------------------------------------
# execute_sql_impl guard tests
# ---------------------------------------------------------------------------

@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_execute_sql_dynamodb_blocked(mock_lookup, mock_boto3):
    """DynamoDB registry row → unsupported_engine, no rds-data call."""
    mock_lookup.return_value = _DYNAMODB_ROW
    from mcp_servers.operations.tools.execute_sql import execute_sql_impl

    result = execute_sql_impl(MagicMock(), cluster_id="ddb-abc", sql="SELECT 1")

    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == "dynamodb"
    assert "message" in result
    # rds-data client must NOT have been created
    mock_boto3.client.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_execute_sql_docdb_blocked(mock_lookup, mock_boto3):
    """DocumentDB registry row → unsupported_engine, no rds-data call."""
    mock_lookup.return_value = _DOCDB_ROW
    from mcp_servers.operations.tools.execute_sql import execute_sql_impl

    result = execute_sql_impl(MagicMock(), cluster_id="docdb-xyz", sql="SELECT 1")

    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == "documentdb"
    mock_boto3.client.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_execute_sql_relational_passes_guard(mock_lookup, mock_boto3):
    """Relational (Aurora) registry row clears the engine guard and proceeds
    to the normal RDS Data API path (no unsupported_engine returned)."""
    mock_lookup.return_value = _AURORA_ROW

    # Stub rds-data client so the actual AWS call doesn't fire
    mock_rds_data = MagicMock()
    mock_rds_data.execute_statement.return_value = {
        "columnMetadata": [{"name": "?column?"}],
        "records": [[{"longValue": 1}]],
    }
    mock_boto3.client.return_value = mock_rds_data

    from mcp_servers.operations.tools.execute_sql import execute_sql_impl

    result = execute_sql_impl(MagicMock(), cluster_id="prod-pg-1", sql="SELECT 1")

    assert result["status"] == "executed"
    assert result["status"] != "unsupported_engine"
    mock_boto3.client.assert_called_once_with("rds-data")


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_execute_sql_engine_family_derived_from_engine_string(mock_lookup, mock_boto3):
    """When engine_family field is absent, derive it from the engine string."""
    row_without_family = {
        "cluster_id": "ddb-no-family",
        "engine": "DynamoDB",
        # no engine_family key
    }
    mock_lookup.return_value = row_without_family
    from mcp_servers.operations.tools.execute_sql import execute_sql_impl

    result = execute_sql_impl(MagicMock(), cluster_id="ddb-no-family", sql="SELECT 1")

    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == "dynamodb"
    mock_boto3.client.assert_not_called()

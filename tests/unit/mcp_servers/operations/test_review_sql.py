from unittest.mock import MagicMock

from mcp_servers.operations.tools.review_sql import review_sql_impl


def test_review_sql_select_is_safe():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="SELECT * FROM users")
    assert result["risk_level"] == "safe"
    assert result["issues"] == []
    assert result["recommendation"] == "safe to execute"


def test_review_sql_delete_without_where():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="DELETE FROM users")
    assert result["risk_level"] == "high"
    assert any("DELETE without WHERE" in i for i in result["issues"])


def test_review_sql_update_without_where():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="UPDATE users SET name='test'")
    assert result["risk_level"] == "medium"
    assert any("UPDATE without WHERE" in i for i in result["issues"])


def test_review_sql_drop_is_critical():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="DROP TABLE users")
    assert result["risk_level"] == "critical"
    assert any("irreversible" in i for i in result["issues"])


def test_review_sql_truncate_is_critical():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="TRUNCATE TABLE users")
    assert result["risk_level"] == "critical"
    assert any("irreversible" in i for i in result["issues"])


def test_review_sql_alter_add_column_rollback():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="ALTER TABLE users ADD COLUMN email VARCHAR(255)")
    assert result["risk_level"] == "high"
    assert result["rollback_sql"] is not None
    assert "DROP COLUMN" in result["rollback_sql"]


def test_review_sql_insert_is_low_risk():
    mock_cache = MagicMock()
    result = review_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="INSERT INTO users (name) VALUES ('alice')")
    assert result["risk_level"] == "low"
    assert result["issues"] == []

from unittest.mock import MagicMock

from mcp_servers.operations.tools.audit_permissions import audit_permissions_impl
from mcp_servers.shared.models import QueryResult


def test_audit_permissions_postgresql():
    mock_cache = MagicMock()
    # audit_permissions_impl runs the role-listing SQL against the TARGET
    # cluster, not the cache DB — so the mock has to stub `execute_on_target`
    # (the cross-account Data API path) rather than `execute`.
    mock_cache.execute_on_target.return_value = QueryResult(
        columns=["username", "is_superuser", "can_create_db", "can_create_role", "can_login"],
        rows=[
            {"username": "admin", "is_superuser": True, "can_create_db": True, "can_create_role": True, "can_login": True},
            {"username": "app_user", "is_superuser": False, "can_create_db": False, "can_create_role": False, "can_login": True},
        ],
        row_count=2,
    )
    result = audit_permissions_impl(mock_cache, cluster_id="prod-pg-1", engine="postgresql")
    assert result["total_users"] == 2
    assert result["superuser_count"] == 1
    assert len(result["warnings"]) == 1
    assert "admin" in result["warnings"][0]


def test_audit_permissions_mysql():
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = QueryResult(
        columns=["username", "host", "is_superuser", "can_grant"],
        rows=[
            {"username": "root", "host": "localhost", "is_superuser": "true", "can_grant": "true"},
            {"username": "app", "host": "%", "is_superuser": "false", "can_grant": "false"},
        ],
        row_count=2,
    )
    result = audit_permissions_impl(mock_cache, cluster_id="prod-mysql-1", engine="mysql")
    assert result["total_users"] == 2
    assert result["superuser_count"] == 1
    assert result["engine"] == "mysql"


def test_audit_permissions_no_superusers():
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = QueryResult(
        columns=["username", "is_superuser", "can_create_db", "can_create_role", "can_login"],
        rows=[
            {"username": "app_user", "is_superuser": False, "can_create_db": False, "can_create_role": False, "can_login": True},
        ],
        row_count=1,
    )
    result = audit_permissions_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["superuser_count"] == 0
    assert result["warnings"] == []


def test_audit_permissions_unreachable_cluster():
    """If the cluster isn't registered or Data API can't reach it, the impl
    returns an explicit `error` field with empty results — not an exception."""
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = audit_permissions_impl(mock_cache, cluster_id="unknown", engine="postgresql")
    assert result["total_users"] == 0
    assert result["superuser_count"] == 0
    assert result["users"] == []
    assert "error" in result and "register" in result["error"]

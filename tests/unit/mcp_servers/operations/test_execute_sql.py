import base64
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.execute_sql import _decode_field, execute_sql_impl


def test_execute_sql_dangerous_blocked():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="DROP TABLE users")
    assert result["status"] == "blocked"
    assert "force=true" in result["reason"]


def test_execute_sql_non_select_requires_approval():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="UPDATE users SET name='test'")
    assert result["status"] == "approval_required"


def test_execute_sql_truncate_blocked():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="TRUNCATE TABLE users")
    assert result["status"] == "blocked"


def test_execute_sql_delete_blocked():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="DELETE FROM users WHERE id=1")
    assert result["status"] == "blocked"


@patch.dict("os.environ", {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"})
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_execute_sql_select_executes(mock_boto3):
    mock_rds_data = MagicMock()
    mock_boto3.client.return_value = mock_rds_data
    mock_rds_data.execute_statement.return_value = {
        "columnMetadata": [{"name": "id"}, {"name": "name"}],
        "records": [[{"longValue": 1}, {"stringValue": "alice"}]],
    }
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="SELECT * FROM users")
    assert result["status"] == "executed"
    assert result["columns"] == ["id", "name"]
    assert result["row_count"] == 1


def test_execute_sql_explain_is_safe():
    """EXPLAIN statements should be treated as safe (no approval needed)."""
    mock_cache = MagicMock()
    # EXPLAIN needs boto3, so we just verify it doesn't return approval_required or blocked
    # by checking that it attempts execution (which will fail without AWS, but the logic path is correct)
    # Instead, test the pattern matching indirectly
    import re

    from mcp_servers.operations.tools.execute_sql import SAFE_PATTERNS
    assert any(re.match(p, "EXPLAIN SELECT * FROM users") for p in SAFE_PATTERNS)


def test_execute_sql_write_approved_without_id_rejected():
    """A write statement with bare `approved=True` (no approval_id) must
    be rejected by the guard — agent cannot bypass DBA approval by just
    flipping the boolean."""
    with patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"}, clear=True):
        mock_cache = MagicMock()
        result = execute_sql_impl(
            mock_cache,
            cluster_id="prod-pg-1",
            sql="UPDATE users SET name='test'",
            approved=True,
        )
        assert result["status"] == "approval_denied"
        assert "approval_id missing" in result["reason"]


# ===== Side-effecting "read" SQL must not take the no-approval fast-path =====


def test_select_calling_pg_terminate_backend_needs_approval():
    """`SELECT pg_terminate_backend(pid)` reads like a SELECT but kills a
    session — it must require approval, not execute silently."""
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1", sql="SELECT pg_terminate_backend(12345)"
    )
    assert out["status"] == "approval_required"
    assert "side-effecting" in out["reason"]


def test_explain_analyze_needs_approval():
    """EXPLAIN ANALYZE actually executes the underlying statement, so even on a
    plain SELECT it must require approval (not the read fast-path)."""
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="EXPLAIN ANALYZE SELECT * FROM big_table",
    )
    assert out["status"] == "approval_required"
    assert "side-effecting" in out["reason"]


def test_explain_analyze_of_delete_is_blocked_as_dangerous():
    """EXPLAIN ANALYZE of a DELETE would run the DELETE — DELETE FROM trips the
    dangerous-pattern block first, requiring force=true."""
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="EXPLAIN ANALYZE DELETE FROM users WHERE id=1",
    )
    assert out["status"] == "blocked"


def test_explain_analyze_with_options_needs_approval():
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM t",
    )
    assert out["status"] == "approval_required"


def test_select_into_needs_approval():
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT * INTO new_table FROM users",
    )
    assert out["status"] == "approval_required"


def test_select_for_update_needs_approval():
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT * FROM jobs WHERE state='queued' FOR UPDATE",
    )
    assert out["status"] == "approval_required"


def test_stacked_statements_need_approval():
    """A SELECT carrying a second statement must not be waved through."""
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT 1; UPDATE users SET name='x'",
    )
    assert out["status"] == "approval_required"
    assert "Multiple SQL statements" in out["reason"]


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_benign_select_with_semicolon_and_into_in_literal_still_safe(mock_boto3):
    """Literals must not trigger false positives: a semicolon and the word
    INTO inside a string must NOT force approval."""
    rds = MagicMock()
    mock_boto3.client.return_value = rds
    rds.execute_statement.return_value = {"columnMetadata": [{"name": "note"}], "records": []}
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT note FROM payments WHERE note = 'paid into acct; ref 9'",
    )
    assert out["status"] == "executed"


def test_comment_marker_inside_string_cannot_hide_stacked_stmt():
    """Regression: a `--` inside a string literal must NOT be treated as a
    comment that erases a following stacked statement. `SELECT '--'; UPDATE...`
    is genuinely multi-statement and must require approval."""
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT '--'; UPDATE t SET x = 1",
    )
    assert out["status"] == "approval_required"
    assert "Multiple SQL statements" in out["reason"]


def test_set_config_needs_approval():
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT set_config('work_mem', '1GB', false)",
    )
    assert out["status"] == "approval_required"


def test_pg_try_advisory_lock_needs_approval():
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT pg_try_advisory_lock(42)",
    )
    assert out["status"] == "approval_required"


def test_pg_advisory_unlock_needs_approval():
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1", sql="SELECT pg_advisory_unlock_all()"
    )
    assert out["status"] == "approval_required"


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_block_comment_with_semicolon_still_safe(mock_boto3):
    """A block comment containing a semicolon must not look multi-statement."""
    rds = MagicMock()
    mock_boto3.client.return_value = rds
    rds.execute_statement.return_value = {"columnMetadata": [{"name": "c"}], "records": []}
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT count(*) AS c /* careful; do not drop */ FROM orders",
    )
    assert out["status"] == "executed"


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_dollar_quoted_literal_with_semicolon_still_safe(mock_boto3):
    """A dollar-quoted literal containing a semicolon is data, not a second
    statement."""
    rds = MagicMock()
    mock_boto3.client.return_value = rds
    rds.execute_statement.return_value = {"columnMetadata": [{"name": "v"}], "records": []}
    out = execute_sql_impl(
        MagicMock(), cluster_id="prod-pg-1",
        sql="SELECT $$a; b$$ AS v",
    )
    assert out["status"] == "executed"


def test_decode_field_handles_full_type_set():
    """RDS Data API fields beyond the four scalars must decode correctly:
    NULL, bytea, arrays, and decimals (returned as stringValue)."""
    assert _decode_field({"isNull": True}) is None
    assert _decode_field({"stringValue": "x"}) == "x"
    assert _decode_field({"longValue": 7}) == 7
    assert _decode_field({"booleanValue": False}) is False
    # NUMERIC/DECIMAL arrive as stringValue — exact precision preserved
    assert _decode_field({"stringValue": "123.4500"}) == "123.4500"
    # bytea -> base64 string (JSON-safe), round-trips to original bytes
    blob = b"\x00\x01\xfe"
    decoded = _decode_field({"blobValue": blob})
    assert base64.b64decode(decoded) == blob
    # array of scalars
    assert _decode_field({"arrayValue": {"longValues": [1, 2, 3]}}) == [1, 2, 3]
    # nested array
    assert _decode_field(
        {"arrayValue": {"arrayValues": [{"stringValues": ["a"]}, {"stringValues": ["b"]}]}}
    ) == [["a"], ["b"]]


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_execute_sql_decodes_null_and_array_rows(mock_boto3):
    mock_rds_data = MagicMock()
    mock_boto3.client.return_value = mock_rds_data
    mock_rds_data.execute_statement.return_value = {
        "columnMetadata": [{"name": "id"}, {"name": "tags"}, {"name": "deleted_at"}],
        "records": [[
            {"longValue": 1},
            {"arrayValue": {"stringValues": ["a", "b"]}},
            {"isNull": True},
        ]],
    }
    result = execute_sql_impl(MagicMock(), cluster_id="prod-pg-1", sql="SELECT * FROM t")
    assert result["rows"][0] == {"id": 1, "tags": ["a", "b"], "deleted_at": None}

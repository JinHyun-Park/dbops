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


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_execute_sql_rds_instance_other_engine_unsupported(mock_boto3, mock_lookup):
    """An rds_instance engine that is neither MySQL nor SQL Server (both direct
    paths now shipped) returns a generic unsupported_engine message — no stale
    "R-4" wording — and, even with legacy TARGET_* env fallbacks set (the exact
    condition that let this slip through to the wrong cluster before), must
    never reach the RDS Data API."""
    mock_rds_data = MagicMock()
    mock_boto3.client.return_value = mock_rds_data
    mock_lookup.return_value = {
        "engine": "oracle-ee",
        "engine_family": "rds_instance",
        "cluster_arn": "arn:test",
        "secret_arn": "arn:secret",
    }
    result = execute_sql_impl(MagicMock(), cluster_id="rds-oracle-1", sql="SELECT * FROM dual")
    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == "rds_instance"
    assert "R-4" not in result["message"]
    mock_rds_data.execute_statement.assert_not_called()


# ===== R-3: rds_instance MySQL direct-TCP path =====

_MYSQL_ROW = {
    "engine": "mysql",
    "engine_family": "rds_instance",
    "endpoint": "rds-mysql-1.abc.us-east-1.rds.amazonaws.com",
    "port": 3306,
    "db_name": "appdb",
    "db_secret_arn": "arn:db-read",
}


def _fake_sm(secret_string='{"username": "u", "password": "p"}'):
    """Mock secretsmanager client returning the given SecretString."""
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": secret_string}
    return sm


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_safe_select_executes(mock_lookup, mock_cfc, mock_md, mock_boto3):
    """Safe SELECT on an rds_instance MySQL row runs over the direct-TCP adapter,
    decoding rows by REAL column name, using the READ secret, never touching
    the RDS Data API."""
    mock_lookup.return_value = dict(_MYSQL_ROW)
    sm = _fake_sm()
    mock_cfc.return_value = sm
    adapter = MagicMock()
    adapter.execute_statement.return_value = {
        "columnMetadata": [{"name": "id"}, {"name": "email"}],
        "records": [[{"longValue": 7}, {"stringValue": "a@b.c"}]],
    }
    mock_md.MySQLDataApiAdapter.return_value = adapter

    result = execute_sql_impl(MagicMock(), cluster_id="rds-mysql-1", sql="SELECT * FROM users")

    assert result["status"] == "executed"
    assert result["columns"] == ["id", "email"]
    assert result["rows"] == [{"id": 7, "email": "a@b.c"}]
    # read path used the READ secret
    mock_cfc.assert_called_once_with("rds-mysql-1", "secretsmanager")
    sm.get_secret_value.assert_called_once_with(SecretId="arn:db-read")
    # RDS Data API must NOT be touched for a direct-TCP instance
    mock_boto3.client.assert_not_called()
    # audit marker prepended to the executed statement
    assert adapter.execute_statement.call_args.kwargs["sql"].startswith(
        "/* source=dbops-agent */ "
    )


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_approved_write_without_write_secret_fails_closed(
    mock_lookup, mock_cfc, mock_md, mock_boto3, mock_verify
):
    """An approved write on a MySQL row with NO db_write_secret_arn fails closed
    with a static message — no secret fetch, no connect."""
    mock_verify.return_value = {"ok": True}
    mock_lookup.return_value = dict(_MYSQL_ROW)  # has db_secret_arn, no write secret

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mysql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "unsupported_engine"
    assert "db_write_secret_arn" in result["reason"]
    mock_cfc.assert_not_called()
    mock_md.connect.assert_not_called()
    # R-5: the missing-secret guard now precedes the consume (via the pre-flight),
    # so a rejected write must NOT burn the single-use approval.
    mock_verify.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_approved_write_executes_with_write_secret(
    mock_lookup, mock_cfc, mock_md, mock_boto3, mock_verify
):
    """An approved write with db_write_secret_arn set executes via the WRITE
    secret and surfaces numberOfRecordsUpdated."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MYSQL_ROW)
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    sm = _fake_sm()
    mock_cfc.return_value = sm
    adapter = MagicMock()
    adapter.execute_statement.return_value = {
        "records": [],
        "columnMetadata": [],
        "numberOfRecordsUpdated": 3,
    }
    mock_md.MySQLDataApiAdapter.return_value = adapter

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mysql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "executed"
    assert result["rows_affected"] == 3
    # write path used the WRITE secret. R-5: the pre-flight probe + the execute
    # branch may each fetch/connect, so every call must use the write secret and
    # connect must run at least once (double-connect is the accepted probe cost).
    assert sm.get_secret_value.call_args_list
    for c in sm.get_secret_value.call_args_list:
        assert c.kwargs["SecretId"] == "arn:db-write"
    mock_md.connect.assert_called()
    mock_boto3.client.assert_not_called()


# ===== R-3 fix: system-schema default must not leak into direct writes =====


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_safe_select_no_db_name_defaults_to_mysql_schema(
    mock_lookup, mock_cfc, mock_md, mock_boto3
):
    """A safe read with no db_name set connects with database='mysql' — the
    system schema is harmless for SELECT/SHOW/performance_schema reads."""
    row = dict(_MYSQL_ROW)
    row.pop("db_name")
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()
    adapter = MagicMock()
    adapter.execute_statement.return_value = {"columnMetadata": [], "records": []}
    mock_md.MySQLDataApiAdapter.return_value = adapter

    execute_sql_impl(MagicMock(), cluster_id="rds-mysql-1", sql="SELECT 1")

    assert mock_md.connect.call_args.kwargs["database"] == "mysql"


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_approved_write_no_db_name_connects_with_none(
    mock_lookup, mock_cfc, mock_md, mock_boto3, mock_verify
):
    """An approved write with no db_name set must get database=None — NOT the
    'mysql' system schema fallback (RDS denies unqualified writes there,
    error 1044, live-verified — and burns the single-use approval)."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MYSQL_ROW)
    row.pop("db_name")
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()
    adapter = MagicMock()
    adapter.execute_statement.return_value = {"columnMetadata": [], "records": [], "numberOfRecordsUpdated": 1}
    mock_md.MySQLDataApiAdapter.return_value = adapter

    execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mysql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )

    assert mock_md.connect.call_args.kwargs["database"] is None


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_approved_write_with_db_name_connects_with_it(
    mock_lookup, mock_cfc, mock_md, mock_boto3, mock_verify
):
    """An approved write on a cluster WITH db_name set connects with that
    schema — the fix must not disturb the configured case."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MYSQL_ROW)  # db_name="appdb"
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()
    adapter = MagicMock()
    adapter.execute_statement.return_value = {"columnMetadata": [], "records": [], "numberOfRecordsUpdated": 1}
    mock_md.MySQLDataApiAdapter.return_value = adapter

    execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mysql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )

    assert mock_md.connect.call_args.kwargs["database"] == "appdb"


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_execution_failure_returns_actionable_static_message(
    mock_lookup, mock_cfc, mock_md, mock_boto3
):
    """The execution-failure branch must return a static, actionable hint —
    never str(e) — and must not leak the underlying exception text."""
    mock_lookup.return_value = dict(_MYSQL_ROW)
    mock_cfc.return_value = _fake_sm()
    mock_md.connect.side_effect = Exception("secret internal detail 1044")

    result = execute_sql_impl(MagicMock(), cluster_id="rds-mysql-1", sql="SELECT 1")

    assert result["status"] == "execution_failed"
    assert "secret internal detail" not in result["reason"]
    assert "db_name" in result["reason"]


# ===== R-4: rds_instance SQL Server direct-TCP path =====

_MSSQL_ROW = {
    "engine": "sqlserver-ex",
    "engine_family": "rds_instance",
    "endpoint": "rds-mssql-1.abc.us-east-1.rds.amazonaws.com",
    "port": 1433,
    "db_name": "appdb",
    "db_secret_arn": "arn:db-read",
}


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_safe_select_executes(mock_lookup, mock_cfc, mock_ms, mock_boto3):
    """Safe SELECT on an rds_instance SQL Server row runs over the direct-TCP
    adapter, decoding rows by REAL column name, using the READ secret, never
    touching the RDS Data API."""
    mock_lookup.return_value = dict(_MSSQL_ROW)
    sm = _fake_sm()
    mock_cfc.return_value = sm
    adapter = MagicMock()
    adapter.execute_statement.return_value = {
        "columnMetadata": [{"name": "id"}, {"name": "email"}],
        "records": [[{"longValue": 7}, {"stringValue": "a@b.c"}]],
    }
    mock_ms.MSSQLDataApiAdapter.return_value = adapter

    result = execute_sql_impl(MagicMock(), cluster_id="rds-mssql-1", sql="SELECT * FROM users")

    assert result["status"] == "executed"
    assert result["columns"] == ["id", "email"]
    assert result["rows"] == [{"id": 7, "email": "a@b.c"}]
    # read path used the READ secret
    mock_cfc.assert_called_once_with("rds-mssql-1", "secretsmanager")
    sm.get_secret_value.assert_called_once_with(SecretId="arn:db-read")
    # RDS Data API must NOT be touched for a direct-TCP instance
    mock_boto3.client.assert_not_called()
    # audit marker prepended to the executed statement
    assert adapter.execute_statement.call_args.kwargs["sql"].startswith(
        "/* source=dbops-agent */ "
    )


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_stacked_batch_needs_approval_not_executed(
    mock_lookup, mock_cfc, mock_ms, mock_boto3
):
    """R-4 C1: `SELECT 1; SHUTDOWN` on a SQL Server row must be classified as
    multi-statement → approval_required, and the direct-TCP adapter must never
    be built or connected. pytds runs the whole batch, so a dangerous verb the
    old keyword allowlist did not list was auto-executing without approval."""
    mock_lookup.return_value = dict(_MSSQL_ROW)

    result = execute_sql_impl(
        MagicMock(), cluster_id="rds-mssql-1", sql="SELECT 1; SHUTDOWN"
    )

    assert result["status"] == "approval_required"
    mock_cfc.assert_not_called()
    mock_ms.MSSQLDataApiAdapter.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_approved_write_without_write_secret_fails_closed(
    mock_lookup, mock_cfc, mock_ms, mock_boto3, mock_verify
):
    """An approved write on a SQL Server row with NO db_write_secret_arn fails
    closed with a static message — no secret fetch, no connect."""
    mock_verify.return_value = {"ok": True}
    mock_lookup.return_value = dict(_MSSQL_ROW)  # has db_secret_arn, no write secret

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mssql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "unsupported_engine"
    assert "db_write_secret_arn" in result["reason"]
    mock_cfc.assert_not_called()
    mock_ms.connect.assert_not_called()
    # R-5: rejected before the consume (pre-flight) — approval not burned.
    mock_verify.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_approved_write_executes_with_write_secret(
    mock_lookup, mock_cfc, mock_ms, mock_boto3, mock_verify
):
    """An approved write with db_write_secret_arn set executes via the WRITE
    secret and surfaces numberOfRecordsUpdated."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MSSQL_ROW)
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    sm = _fake_sm()
    mock_cfc.return_value = sm
    adapter = MagicMock()
    adapter.execute_statement.return_value = {
        "records": [],
        "columnMetadata": [],
        "numberOfRecordsUpdated": 3,
    }
    mock_ms.MSSQLDataApiAdapter.return_value = adapter

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mssql-1",
        sql="UPDATE appdb.dbo.users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "executed"
    assert result["rows_affected"] == 3
    # write path used the WRITE secret. R-5: pre-flight probe + execute may each
    # fetch/connect, so every fetch must use the write secret and connect runs
    # at least once.
    assert sm.get_secret_value.call_args_list
    for c in sm.get_secret_value.call_args_list:
        assert c.kwargs["SecretId"] == "arn:db-write"
    mock_ms.connect.assert_called()
    mock_boto3.client.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_approved_write_no_db_name_fails_closed(
    mock_lookup, mock_cfc, mock_ms, mock_verify
):
    """An approved SQL Server write with a write secret but NO db_name must fail
    closed — SQL Server would otherwise silently write to the master system DB
    (unlike MySQL, whose database=None errors out). No connect."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MSSQL_ROW)
    row.pop("db_name")
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mssql-1",
        sql="CREATE TABLE Orders(id INT)",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "unsupported_engine"
    assert "db_name" in result["reason"]
    mock_ms.connect.assert_not_called()
    # R-5: master-write guard now precedes the consume (pre-flight) — not burned.
    mock_verify.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_row_still_routes_to_mysql_not_mssql(
    mock_lookup, mock_cfc, mock_md, mock_ms, mock_boto3
):
    """Regression: a MySQL row must keep routing to mysql_direct — never the
    new SQL Server adapter."""
    mock_lookup.return_value = dict(_MYSQL_ROW)
    mock_cfc.return_value = _fake_sm()
    adapter = MagicMock()
    adapter.execute_statement.return_value = {"columnMetadata": [], "records": []}
    mock_md.MySQLDataApiAdapter.return_value = adapter

    execute_sql_impl(MagicMock(), cluster_id="rds-mysql-1", sql="SELECT 1")

    mock_md.connect.assert_called_once()
    mock_ms.connect.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_side_effecting_tsql_on_mssql_row_needs_approval(mock_lookup, mock_cfc, mock_ms):
    """A side-effecting T-SQL read (OPENQUERY reaches a remote server) on a SQL
    Server row must require approval — not auto-execute over the direct path."""
    mock_lookup.return_value = dict(_MSSQL_ROW)
    out = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mssql-1",
        sql="SELECT * FROM OPENQUERY(remote, 'SELECT 1')",
    )
    assert out["status"] == "approval_required"
    assert "side-effecting" in out["reason"]
    mock_ms.connect.assert_not_called()


# ===== direct-path no-leak contract: a driver exception must NOT reach the
# caller's response (only a static Korean reason; detail goes to the log). =====

_LEAK = "host=internal-db.corp user=svc password=hunter2 pwd-in-stacktrace"


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_error_does_not_leak_exception_text(
    mock_lookup, mock_cfc, mock_md, mock_boto3
):
    """A MySQL direct-TCP connect/execute failure must return a static reason —
    the raw exception (host/creds/internal detail) must never appear in ANY
    field of the response. Same no-str(e)-leak contract as the secret-fetch path."""
    mock_lookup.return_value = dict(_MYSQL_ROW)
    mock_cfc.return_value = _fake_sm()
    mock_md.connect.side_effect = Exception(_LEAK)

    result = execute_sql_impl(MagicMock(), cluster_id="rds-mysql-1", sql="SELECT 1")

    assert result["status"] == "execution_failed"
    assert _LEAK not in str(result) and "hunter2" not in str(result)


@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_error_does_not_leak_exception_text(
    mock_lookup, mock_cfc, mock_ms, mock_boto3
):
    """Same no-leak contract on the SQL Server direct-TCP branch."""
    mock_lookup.return_value = dict(_MSSQL_ROW)
    mock_cfc.return_value = _fake_sm()
    mock_ms.connect.side_effect = Exception(_LEAK)

    result = execute_sql_impl(MagicMock(), cluster_id="rds-mssql-1", sql="SELECT 1")

    assert result["status"] == "execution_failed"
    assert _LEAK not in str(result) and "hunter2" not in str(result)


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_aurora_data_api_failure_does_not_leak_exception_text(mock_boto3):
    """The Aurora RDS-Data-API path must return a STATIC reason on failure —
    never the raw boto exception (no str(e) leak, project-wide contract). Detail
    goes to the CloudWatch print, and the old `error` key is gone."""
    rds = MagicMock()
    mock_boto3.client.return_value = rds
    leak = "BadRequestException: secret=arn:aws:secret:LEAK creds=topsecret-value"
    rds.execute_statement.side_effect = Exception(leak)

    result = execute_sql_impl(MagicMock(), cluster_id="prod-pg-1", sql="SELECT 1")

    assert result["status"] == "execution_failed"
    assert "error" not in result  # the str(e)-carrying key was removed
    assert leak not in str(result) and "topsecret-value" not in str(result)


@patch.dict(
    "os.environ",
    {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"},
)
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_aurora_http_endpoint_disabled_gives_enable_hint_without_leak(mock_boto3):
    """The HttpEndpoint-disabled case still yields the enable-data-api guidance
    (the branch reads the LOCAL err), while never leaking the raw exception."""
    rds = MagicMock()
    mock_boto3.client.return_value = rds
    rds.execute_statement.side_effect = Exception(
        "BadRequestException: HttpEndpoint is not enabled for resource"
    )

    result = execute_sql_impl(MagicMock(), cluster_id="prod-pg-1", sql="SELECT 1")

    assert result["status"] == "execution_failed"
    assert "enable-http-endpoint" in result["reason"] or "enable_data_api" in result["reason"]
    assert "error" not in result


# ===== R-5 backlog: metadata pre-check BEFORE consuming the write approval =====
# The single-use approval must NOT be burned by a metadata-detectable reject
# (unsupported engine / missing write secret / SQL Server master-write): those
# are checked BEFORE verify_approval, with NO secret fetch and NO connect (so an
# unauthorized request triggers zero privileged resource access before authz).
# A genuine connect/execute failure still happens AFTER the consume — detecting
# it pre-consume would require connecting before authz — an accepted trade-off.


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_write_connect_failure_after_consume_is_accepted_tradeoff(
    mock_lookup, mock_cfc, mock_md, mock_boto3, mock_verify
):
    """A GENUINE connect failure on an approved MySQL write happens in the
    execute branch AFTER the single consume (the metadata pre-check passed:
    write secret present, engine ok). Detecting it pre-consume would require
    connecting before authz — a worse posture — so this rare burn is the accepted
    trade-off: verify_approval WAS called. Still: static reason, no str(e) leak."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MYSQL_ROW)
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()
    mock_md.connect.side_effect = Exception(_LEAK)

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mysql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "execution_failed"
    mock_verify.assert_called_once()  # consumed (accepted): connect fail is post-authz
    assert _LEAK not in str(result) and "hunter2" not in str(result)


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.mssql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mssql_master_write_no_db_does_not_consume_approval(
    mock_lookup, mock_cfc, mock_ms, mock_verify
):
    """Pre-flight guard: an approved SQL Server write with no db_name is rejected
    (master-write fail-closed) BEFORE verify_approval — approval not consumed,
    no connect."""
    row = dict(_MSSQL_ROW)
    row.pop("db_name")
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mssql-1",
        sql="CREATE TABLE Orders(id INT)",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "unsupported_engine"
    assert "db_name" in result["reason"]
    mock_verify.assert_not_called()
    mock_ms.connect.assert_not_called()


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql.mysql_direct")
@patch("mcp_servers.operations.tools.execute_sql.client_for_cluster")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_direct_mysql_write_happy_path_consumes_once_then_executes(
    mock_lookup, mock_cfc, mock_md, mock_boto3, mock_verify
):
    """Happy path: metadata pre-check passes → verify_approval consumed EXACTLY
    once → single connect (no probe) → adapter executes. The invariant is that
    the single consume ran and the write executed."""
    mock_verify.return_value = {"ok": True}
    row = dict(_MYSQL_ROW)
    row["db_write_secret_arn"] = "arn:db-write"
    mock_lookup.return_value = row
    mock_cfc.return_value = _fake_sm()
    adapter = MagicMock()
    adapter.execute_statement.return_value = {
        "records": [],
        "columnMetadata": [],
        "numberOfRecordsUpdated": 2,
    }
    mock_md.MySQLDataApiAdapter.return_value = adapter

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="rds-mysql-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "executed"
    assert result["rows_affected"] == 2
    mock_verify.assert_called_once()
    assert mock_md.connect.call_count == 1  # single connect — no pre-check probe
    adapter.execute_statement.assert_called_once()


@patch("mcp_servers.operations.tools.execute_sql.verify_approval")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
def test_aurora_data_api_approved_write_consumes_before_execute(
    mock_lookup, mock_boto3, mock_verify
):
    """Aurora (Data API) write path is UNCHANGED: verify_approval is still
    consumed before the Data API execute — no pre-flight connect for the
    data_api path, so its consume position is exactly as before."""
    mock_verify.return_value = {"ok": True}
    mock_lookup.return_value = {
        "engine": "aurora-postgresql",
        "engine_family": "relational",
        "cluster_arn": "arn:aurora",
        "secret_arn": "arn:sec",
        "db_name": "appdb",
    }
    rds = MagicMock()
    mock_boto3.client.return_value = rds
    rds.execute_statement.return_value = {
        "columnMetadata": [],
        "records": [],
        "numberOfRecordsUpdated": 5,
    }

    result = execute_sql_impl(
        MagicMock(),
        cluster_id="aurora-pg-1",
        sql="UPDATE users SET name='x' WHERE id=1",
        approved=True,
        approval_id="appr-1",
    )
    assert result["status"] == "executed"
    assert result["rows_affected"] == 5
    mock_verify.assert_called_once()
    rds.execute_statement.assert_called_once()

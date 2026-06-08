from unittest.mock import MagicMock

from mcp_servers.operations.tools.review_sql import review_sql_impl


def _review(sql):
    return review_sql_impl(MagicMock(), cluster_id="prod-pg-1", sql=sql)


def test_review_sql_select_is_safe():
    result = _review("SELECT * FROM users")
    assert result["risk_level"] == "safe"
    assert result["issues"] == []
    assert result["recommendation"] == "safe to execute"


def test_review_sql_delete_without_where():
    result = _review("DELETE FROM users")
    assert result["risk_level"] == "high"
    assert any("DELETE without WHERE" in i for i in result["issues"])


def test_review_sql_update_without_where():
    result = _review("UPDATE users SET name='test'")
    assert result["risk_level"] == "medium"
    assert any("UPDATE without WHERE" in i for i in result["issues"])


def test_review_sql_drop_is_critical():
    result = _review("DROP TABLE users")
    assert result["risk_level"] == "critical"
    assert any("irreversible" in i for i in result["issues"])


def test_review_sql_truncate_is_critical():
    result = _review("TRUNCATE TABLE users")
    assert result["risk_level"] == "critical"
    assert any("irreversible" in i for i in result["issues"])


def test_review_sql_insert_is_low_risk():
    result = _review("INSERT INTO users (name) VALUES ('alice')")
    assert result["risk_level"] == "low"
    assert result["issues"] == []


# --- safety fix: ADD COLUMN must NOT suggest a DROP COLUMN "rollback" ---------


def test_add_column_does_not_suggest_drop_column_rollback():
    """The old behavior suggested DROP COLUMN as the rollback for ADD COLUMN —
    data loss if the column was written to. Now: no auto-rollback, a note."""
    result = _review("ALTER TABLE users ADD COLUMN email VARCHAR(255)")
    assert result["risk_level"] == "high"
    assert result["rollback_sql"] is None
    assert "rollback_note" in result
    assert "DROP COLUMN" in result["rollback_note"]


def test_create_index_has_safe_drop_index_rollback():
    result = _review("CREATE INDEX idx_users_email ON users (email)")
    assert result["rollback_sql"] == "DROP INDEX idx_users_email"


def test_create_index_concurrently_rollback_is_concurrent():
    result = _review("CREATE INDEX CONCURRENTLY idx_u ON users (x)")
    assert result["rollback_sql"] == "DROP INDEX CONCURRENTLY idx_u"


def test_rename_column_reverses():
    result = _review("ALTER TABLE users RENAME COLUMN a TO b")
    assert result["rollback_sql"] == "ALTER TABLE users RENAME COLUMN b TO a"


# --- structure-not-data classification ----------------------------------------


def test_literal_drop_is_not_flagged_as_drop():
    result = _review("SELECT id FROM audit WHERE action = 'DROP the prod table'")
    assert result["risk_level"] == "safe"
    assert result["issues"] == []


def test_alter_drop_column_escalates_to_critical():
    result = _review("ALTER TABLE users DROP COLUMN email")
    assert result["risk_level"] == "critical"


def test_multi_statement_flagged():
    result = _review("UPDATE users SET x=1 WHERE id=1; DROP TABLE users")
    assert any("multiple statements" in i for i in result["issues"])

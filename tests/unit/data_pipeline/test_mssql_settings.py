"""sys.configurations -> cluster_settings (E-3).

Every row shape below was MEASURED live on dbops-demo-mssql (sqlserver-ex
15.00.4470.1.v1) through the deployed operations MCP Lambda, including the one
case that decides the wording: `min server memory (MB)` is CONFIGURED 0 but
RUNNING 16 with is_dynamic = 1. Calling that "restart required" would be wrong,
so the divergence wording branches on is_dynamic and this file pins both sides.

IDENTIFIER PINNING, and WHICH HALF this file gets. The T-SQL half is pinned by
identifier only: CI has no SQL Server, so `_Fake` asserts the view, the columns
and the curated option literals the statement depends on (with word boundaries,
because the earlier `"sys.configurations" in sql` assertion was satisfied by
`sys.configurationsZZZ`, and MEASURED: `CAST(is_dynamic AS INT)` and the whole
cluster_settings upsert could be mutated with this file staying green). The
statement itself WAS executed against the live engine: driven read-only through
the deployed operations MCP execute_sql path against dbops-demo-mssql
(sqlserver-ex 15.00.4470.1.v1), 84 sys.configurations rows total and 23 after the
curated filter. The PostgreSQL half is EXECUTED, not pinned: the same
UPSERT_SETTING_SQL runs against a real server with the real cache schema in
tests/unit/test_mysql_tier_cache_sql_real_pg.py.
"""

import importlib.util
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "rds_direct_collector"


def _load(name, rel):
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ms = _load("mssql_settings", "mssql_settings.py")


def _f(v):
    return {"longValue": v} if isinstance(v, int) else {"stringValue": v}


# Every identifier the T-SQL read depends on, word-boundary anchored so a
# renamed view or column cannot be satisfied by a prefix.
_TSQL_REQUIRED = (
    r"/\* source=dbops-etl \*/",
    r"\bFROM\s+sys\.configurations\b",
    # Pinned WITH the aliases and IN ORDER: the reader is positional
    # (rec[0]=name, rec[1]=configured, rec[2]=running, rec[3]=is_dynamic), and
    # `name` also appears in the WHERE / ORDER BY, so bare column patterns are
    # satisfiable by a statement whose projection has already been broken.
    r"SELECT\s+RTRIM\(name\)\s+AS\s+name\s*,\s*"
    r"CAST\(value\s+AS\s+VARCHAR\(64\)\)\s+AS\s+configured\s*,\s*"
    r"CAST\(value_in_use\s+AS\s+VARCHAR\(64\)\)\s+AS\s+running\s*,\s*"
    r"CAST\(is_dynamic\s+AS\s+INT\)\s+AS\s+is_dynamic\b",
    r"\bWHERE\s+name\s+IN\s*\(",
)
# The cache write, pinned here and EXECUTED for real in the real-PG test.
_UPSERT_REQUIRED = (
    r"\bINSERT\s+INTO\s+cluster_settings\s*\(\s*cluster_id,\s*name,\s*value,\s*unit,\s*updated_at\s*\)",
    r"\bON\s+CONFLICT\s*\(\s*cluster_id,\s*name\s*\)\s+DO\s+UPDATE\b",
)


def _require(sql, patterns, what):
    for pat in patterns:
        assert re.search(pat, sql, re.S | re.I), (
            f"{what} no longer names {pat!r}. A canned row must not stand in for a "
            f"statement whose identifiers changed:\n{sql}")


class _Fake:
    """Data-API-shaped adapter double + cache_execute capture.

    Both legs VALIDATE the statement before answering, so every test in this file
    (not just the dedicated one) fails if an identifier moves.
    """

    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.writes = []

    def execute_statement(self, **kw):
        self.sql = kw["sql"]
        _require(self.sql, _TSQL_REQUIRED, "the sys.configurations read")
        return {"records": [[_f(a), _f(b), _f(c), _f(d)] for a, b, c, d in self.rows]}

    def cache_execute(self, sql, params):
        _require(sql, _UPSERT_REQUIRED, "the cluster_settings upsert")
        assert set(params) == {"cluster_id", "name", "value", "unit"}, params
        self.writes.append(params)


def _run(rows):
    fake = _Fake(rows)
    result = ms.collect_mssql_settings(
        fake, fake.cache_execute, "", "", "dbops-demo-mssql", "master")
    return fake, result


# MEASURED rows: (name, configured, running, is_dynamic)
_IN_SYNC = ("max server memory (MB)", "1576", "1576", 1)
_ENGINE_ADJUSTED = ("min server memory (MB)", "0", "16", 1)
_NEEDS_RESTART = ("user connections", "0", "40", 0)


def test_running_value_is_stored_not_configured():
    """cluster_settings.value must be what the server is RUNNING.

    A DBA comparing the dashboard against `sp_configure` sees value_in_use, so
    storing the configured value would disagree with the server itself.
    """
    fake, result = _run([_ENGINE_ADJUSTED])
    (row,) = fake.writes
    assert row["name"] == "min server memory (MB)"
    assert row["value"] == "16"          # running, MEASURED
    assert result["settings_upserted"] == 1
    assert result["diverging_from_configured"] == 1


def test_in_sync_setting_carries_no_divergence_marker():
    fake, _ = _run([_IN_SYNC])
    (row,) = fake.writes
    assert row["value"] == "1576"
    assert row["unit"] == ""


def test_dynamic_divergence_is_not_called_restart_required():
    """is_dynamic=1 + value != value_in_use means the ENGINE adjusted it.

    No restart will apply the configured value, so the marker must not promise
    one. This is the exact fixture case (min server memory 0 -> 16).
    """
    fake, _ = _run([_ENGINE_ADJUSTED])
    (row,) = fake.writes
    assert "설정값 0" in row["unit"]
    assert "재시작" not in row["unit"]


def test_static_divergence_says_restart():
    fake, _ = _run([_NEEDS_RESTART])
    (row,) = fake.writes
    assert row["value"] == "40"
    assert "설정값 0" in row["unit"]
    assert "재시작" in row["unit"]


def test_statement_pins_server_scoped_view_and_the_curated_names():
    """The collector must read sys.configurations (SERVER-scoped, hence correct
    from the `master` session) and only the curated option list.

    IDENTIFIER HALF: the view, all four projected columns and the option literals
    are pinned here (word-boundary anchored: `sys.configurationsZZZ` no longer
    satisfies `sys.configurations`). The statement was separately EXECUTED against
    dbops-demo-mssql, read-only, returning 84 rows / 23 after this filter."""
    fake, _ = _run([_IN_SYNC])
    _require(fake.sql, _TSQL_REQUIRED, "the sys.configurations read")
    # Every curated name must reach the statement as an exact quoted literal:
    # a name with no row is simply absent from the result, never invented.
    for name in ms._TRACKED:
        assert f"'{name}'" in fake.sql
    assert len(ms._TRACKED) == 23


def test_empty_dmv_writes_nothing_and_says_so():
    """No rows must NOT be reported as "all settings in sync"."""
    fake, result = _run([])
    assert fake.writes == []
    assert result == {"cluster_id": "dbops-demo-mssql", "settings_upserted": 0,
                      "diverging_from_configured": 0}


def test_upsert_targets_cluster_settings_on_conflict():
    """Re-running must update in place, not accumulate duplicate rows.

    Anchored, because `INSERT INTO cluster_settingsZZZ` satisfied the old
    substring assertion (MEASURED: that mutation left this file green). The same
    statement is EXECUTED against a real PostgreSQL server, on the real cache
    schema, in tests/unit/test_mysql_tier_cache_sql_real_pg.py, which is what
    proves the ON CONFLICT target matches the table's actual primary key."""
    _require(ms.UPSERT_SETTING_SQL, _UPSERT_REQUIRED, "the cluster_settings upsert")

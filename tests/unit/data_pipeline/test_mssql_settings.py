"""sys.configurations -> cluster_settings (E-3).

Every row shape below was MEASURED live on dbops-demo-mssql (sqlserver-ex
15.00.4470.1.v1) through the deployed operations MCP Lambda, including the one
case that decides the wording: `min server memory (MB)` is CONFIGURED 0 but
RUNNING 16 with is_dynamic = 1. Calling that "restart required" would be wrong,
so the divergence wording branches on is_dynamic and this file pins both sides.
"""

import importlib.util
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


class _Fake:
    """Data-API-shaped adapter double + cache_execute capture."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.writes = []

    def execute_statement(self, **kw):
        self.sql = kw["sql"]
        return {"records": [[_f(a), _f(b), _f(c), _f(d)] for a, b, c, d in self.rows]}

    def cache_execute(self, sql, params):
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
    from the `master` session) and only the curated option list."""
    fake, _ = _run([_IN_SYNC])
    assert "sys.configurations" in fake.sql
    assert "/* source=dbops-etl */" in fake.sql
    # A name with no row is simply absent, never invented.
    for name in ("max server memory (MB)", "max degree of parallelism",
                 "cost threshold for parallelism", "blocked process threshold (s)"):
        assert f"'{name}'" in fake.sql
    # value/value_in_use are sql_variant; the statement must CAST or the adapter
    # hands back a type the row reader has no field for.
    assert "CAST(value AS VARCHAR" in fake.sql
    assert "CAST(value_in_use AS VARCHAR" in fake.sql


def test_empty_dmv_writes_nothing_and_says_so():
    """No rows must NOT be reported as "all settings in sync"."""
    fake, result = _run([])
    assert fake.writes == []
    assert result == {"cluster_id": "dbops-demo-mssql", "settings_upserted": 0,
                      "diverging_from_configured": 0}


def test_upsert_targets_cluster_settings_on_conflict():
    """Re-running must update in place, not accumulate duplicate rows."""
    assert "INSERT INTO cluster_settings" in ms.UPSERT_SETTING_SQL
    assert "ON CONFLICT (cluster_id, name) DO UPDATE" in ms.UPSERT_SETTING_SQL

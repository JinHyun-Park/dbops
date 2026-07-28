"""MySQL Maintenance Health checks (E-2).

pg_health_checks runs ONLY on the "postgresql" branch, so the MySQL branch had no
health-check collector. These tests pin what the MySQL one emits AND, just as
importantly, what it refuses to emit: no dead_tuples (it would be the identical
DATA_FREE number under a PG name), no index_unused (no per-index granularity in
the cache), and nothing at all when the source is empty.

The fragmentation numbers used below are the MEASURED live values from the Aurora
MySQL demo cluster's cache on 2026-07-28: products 963,662 live / 108,473 free
rows (11.26%), sales 1,284,750 / 119,156 (9.27%), and the 17 cluster_settings
rows mysql_locks wrote for it.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load(mod_name, rel):
    import sys
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_load("instance_specs", "collectors/instance_specs.py")
_load("mysql_param_fitness", "collectors/mysql_param_fitness.py")
hc = _load("mysql_health_checks", "collectors/mysql_health_checks.py")

# The live cluster's settings, verbatim (only the names the checks read matter).
_LIVE_SETTINGS = {
    "innodb_buffer_pool_size": "294387712",
    "innodb_flush_log_at_trx_commit": "1",
    "innodb_io_capacity": "200",
    "log_bin": "OFF",
    "long_query_time": "1.000000",
    "max_connections": "90",
    "slow_query_log": "ON",
}

# The live cluster's two tables, as the cache SQL returns them.
_LIVE_TABLES = [
    {"schema_name": "sampledb", "table_name": "products", "n_live_tup": 963662,
     "n_dead_tup": 108473, "total_bytes": 56180736},
    {"schema_name": "sampledb", "table_name": "sales", "n_live_tup": 1284750,
     "n_dead_tup": 119156, "total_bytes": 57229312},
]


def _run(tables, settings):
    """Run the collector with a mocked _execute, returning the inserted findings."""
    emitted = []

    def fake(rds, arn, secret, db, sql, params=None):
        if sql.strip().upper().startswith("INSERT"):
            emitted.append(params)
            return []
        if "FROM table_stats" in sql:
            return list(tables)
        if "FROM cluster_settings" in sql:
            return [{"name": n, "value": v} for n, v in settings.items()]
        return []

    with patch.object(hc, "_execute", side_effect=fake):
        result = hc.collect_mysql_health_checks(
            MagicMock(), "arn", "secret", "db", "c1",
            snapshot_ts="2026-07-28T00:00:00Z",
        )
    return emitted, result


def test_live_cluster_emits_nothing_because_everything_is_actually_fine():
    """The honest outcome on the only live Aurora MySQL target: 11.26% and 9.27%
    reclaimable space is ordinary InnoDB free-list churn, and slow_query_log=ON /
    long_query_time=1s / innodb_flush_log_at_trx_commit=1 are all correct. A
    threshold tuned to make a finding appear here would be a manufactured alert."""
    emitted, result = _run(_LIVE_TABLES, _LIVE_SETTINGS)
    assert emitted == []
    assert result["tables_examined"] == 2
    # Both live tables clear the min-rows gate, so the PERCENTAGE is the only
    # thing keeping them silent.
    assert result["tables_over_min_rows"] == 2
    assert result["settings_read"] == 7
    assert result["findings_emitted"] == 0


def test_log_bin_off_is_not_flagged():
    """Aurora MySQL ships binlog OFF and replicates through the storage layer.
    Flagging it would be wrong for most clusters, and it is exactly the kind of
    vanilla-MySQL best practice that must not be applied here."""
    emitted, _ = _run(_LIVE_TABLES, _LIVE_SETTINGS)
    assert not any(p["subject"] == "log_bin" for p in emitted)


def test_fragmentation_fires_above_threshold_and_recommends_optimize_table():
    tables = [{"schema_name": "app", "table_name": "purge_log", "n_live_tup": 1_000_000,
               "n_dead_tup": 350_000, "total_bytes": 900_000_000}]
    emitted, result = _run(tables, _LIVE_SETTINGS)
    assert len(emitted) == 1
    f = emitted[0]
    assert f["check_type"] == "mysql_fragmentation"
    assert f["severity"] == "warning"  # 35% is between 25 and 40
    assert f["subject"] == "app.purge_log"
    assert "OPTIMIZE TABLE" in f["recommendation"]
    # It must not borrow PG vocabulary for a thing InnoDB does not have.
    assert "VACUUM" not in f["recommendation"]
    assert "dead tuple" not in f["recommendation"].lower()
    assert result["findings_emitted"] == 1


def test_fragmentation_escalates_to_critical():
    tables = [{"schema_name": "app", "table_name": "t", "n_live_tup": 1_000_000,
               "n_dead_tup": 600_000, "total_bytes": 1}]
    emitted, _ = _run(tables, _LIVE_SETTINGS)
    assert [f["severity"] for f in emitted] == ["critical"]


def test_fragmentation_skips_small_tables():
    """90% fragmentation on a 1,000-row table is a few pages and a fast rebuild;
    a finding there is noise that buries the real ones."""
    tables = [{"schema_name": "app", "table_name": "tiny", "n_live_tup": 1000,
               "n_dead_tup": 900, "total_bytes": 100}]
    emitted, result = _run(tables, _LIVE_SETTINGS)
    assert emitted == []
    assert result["tables_examined"] == 1
    assert result["tables_over_min_rows"] == 0


def test_no_table_stats_rows_emits_nothing_rather_than_an_all_clear():
    """An empty source means the deep-read collector did not run. Silence is the
    only honest answer; a "0% fragmentation" finding would be a false all-clear."""
    emitted, result = _run([], _LIVE_SETTINGS)
    assert emitted == []
    assert result["tables_examined"] == 0


def test_slow_query_log_off_is_flagged():
    settings = dict(_LIVE_SETTINGS, slow_query_log="OFF")
    emitted, _ = _run(_LIVE_TABLES, settings)
    assert len(emitted) == 1
    f = emitted[0]
    assert f["check_type"] == "setting_misconfigured"
    assert f["subject"] == "slow_query_log"
    assert f["severity"] == "warning"


def test_default_long_query_time_is_flagged_only_when_the_log_is_on():
    """MySQL's default 10s means the slow log records almost nothing useful. But
    if the log is OFF, long_query_time is moot: report the cause, not both."""
    on = dict(_LIVE_SETTINGS, slow_query_log="ON", long_query_time="10.000000")
    emitted, _ = _run(_LIVE_TABLES, on)
    assert [f["subject"] for f in emitted] == ["long_query_time"]

    off = dict(_LIVE_SETTINGS, slow_query_log="OFF", long_query_time="10.000000")
    emitted, _ = _run(_LIVE_TABLES, off)
    assert [f["subject"] for f in emitted] == ["slow_query_log"]


def test_relaxed_commit_durability_is_flagged():
    settings = dict(_LIVE_SETTINGS, innodb_flush_log_at_trx_commit="2")
    emitted, _ = _run(_LIVE_TABLES, settings)
    assert [f["subject"] for f in emitted] == ["innodb_flush_log_at_trx_commit"]


def test_missing_settings_are_skipped_never_assumed():
    """A setting absent from cluster_settings has NOT been read. Comparing against
    a default we did not observe would invent a misconfiguration."""
    emitted, result = _run(_LIVE_TABLES, {})
    assert emitted == []
    assert result["settings_read"] == 0


def test_all_findings_share_the_handler_run_timestamp():
    """Every finding in one ETL cycle must carry the shared run_ts, or the
    dashboard's MAX(snapshot_time) batch shows only the last collector's rows."""
    tables = [{"schema_name": "app", "table_name": "t", "n_live_tup": 1_000_000,
               "n_dead_tup": 600_000, "total_bytes": 1}]
    settings = dict(_LIVE_SETTINGS, slow_query_log="OFF",
                    innodb_flush_log_at_trx_commit="0")
    emitted, _ = _run(tables, settings)
    assert len(emitted) == 3
    assert {f["ts"] for f in emitted} == {"2026-07-28T00:00:00Z"}


def test_does_not_emit_pg_only_check_types():
    """The PG collector has 7 check_types. Five must never appear here: three are
    inherently non-applicable to InnoDB, dead_tuples/table_bloat would be the same
    DATA_FREE number reported twice, and index_unused has no per-index source in
    the cache."""
    tables = [{"schema_name": "app", "table_name": "t", "n_live_tup": 1_000_000,
               "n_dead_tup": 600_000, "total_bytes": 1}]
    settings = dict(_LIVE_SETTINGS, slow_query_log="OFF")
    emitted, _ = _run(tables, settings)
    banned = {"dead_tuples", "table_bloat", "index_unused", "txid_age",
              "vacuum_overdue", "extension_missing"}
    assert not banned & {f["check_type"] for f in emitted}


def test_every_emitted_check_type_has_a_frontend_label_and_tab():
    """A check_type with no CHECK_LABELS entry renders ONLY under the "All" tab,
    and a label whose category is missing from TABS_MYSQL is equally invisible.
    That has already hidden a shipped finding once (docdb_mongo_*)."""
    panel = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "components"
             / "dashboard" / "maintenance-health-panel.tsx").read_text()
    import re
    labels = dict(re.findall(r"^\s*(\w+):\s*\"([^\"]+)\",", panel, re.MULTILINE))
    tabs_block = re.search(r"const TABS_MYSQL = \[(.*?)\] as const;", panel, re.S).group(1)
    tabs = set(re.findall(r'"([^"]+)"', tabs_block))

    tables = [{"schema_name": "app", "table_name": "t", "n_live_tup": 1_000_000,
               "n_dead_tup": 600_000, "total_bytes": 1}]
    settings = dict(_LIVE_SETTINGS, slow_query_log="OFF",
                    innodb_flush_log_at_trx_commit="0")
    emitted, _ = _run(tables, settings)
    assert emitted, "이 테스트는 실제로 emit된 check_type을 검사해야 한다"
    for f in emitted:
        ct = f["check_type"]
        assert ct in labels, f"CHECK_LABELS에 {ct} 없음 → All 탭 밖에서 안 보인다"
        assert labels[ct] in tabs, f"TABS_MYSQL에 {labels[ct]} 탭 없음 → {ct} 숨김"

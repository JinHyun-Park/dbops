"""The APM migration must be idempotent DDL that the schema_migrator picks up."""
import re
from pathlib import Path

MIG = Path(__file__).resolve().parents[2] / "data-pipeline/schema_migrator/sql/schema_v28.sql"


def test_migration_file_exists_and_is_v28():
    assert MIG.exists(), "schema_v28.sql must exist"


def test_creates_three_apm_tables_idempotently():
    sql = MIG.read_text()
    for table in ("apm_target_meta", "apm_metric_snapshots", "apm_log_level_counts"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", sql), f"{table} missing/ not idempotent"


def test_metric_lookup_index_and_log_unique_present():
    sql = MIG.read_text()
    assert "CREATE INDEX IF NOT EXISTS idx_apm_metric_lookup" in sql
    assert "UNIQUE (target_id, ts, log_group, level)" in sql


def test_no_raw_log_text_column():
    # Contract: raw log lines are never stored; only per-level counts.
    sql = MIG.read_text().lower()
    assert "message" not in sql and "raw_log" not in sql

"""ASH sampler — relational filter + Data API row parse."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_H = Path(__file__).resolve().parents[3] / "data-pipeline/ash_sampler/handler.py"
_spec = importlib.util.spec_from_file_location("ash_sampler", _H)
ash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ash)


def test_scan_relational_filters_to_pg_mysql_with_arns():
    table = MagicMock()
    table.scan.return_value = {"Items": [
        {"cluster_id": "pg1", "engine": "aurora-postgresql", "cluster_arn": "a", "secret_arn": "s", "db_name": "app"},
        {"cluster_id": "my1", "engine": "aurora-mysql", "cluster_arn": "a2", "secret_arn": "s2"},
        {"cluster_id": "ddb1", "engine": "dynamodb", "cluster_arn": "a3", "secret_arn": "s3"},  # not relational
        {"cluster_id": "pg2", "engine": "aurora-postgresql"},  # no ARNs → skipped
        {"cluster_id": "ec1", "engine": "redis", "cluster_arn": "a4", "secret_arn": "s4"},  # not relational
    ]}
    targets = ash._scan_relational(table)
    ids = {t["cluster_id"] for t in targets}
    assert ids == {"pg1", "my1"}
    pg = next(t for t in targets if t["cluster_id"] == "pg1")
    assert pg["db"] == "app"  # db_name carried
    my = next(t for t in targets if t["cluster_id"] == "my1")
    assert my["db"] == "sampledb"  # default when db_name absent


def test_scan_relational_paginates():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"cluster_id": "pg1", "engine": "aurora-postgresql", "cluster_arn": "a", "secret_arn": "s"}],
         "LastEvaluatedKey": {"cluster_id": "pg1"}},
        {"Items": [{"cluster_id": "pg2", "engine": "aurora-postgresql", "cluster_arn": "b", "secret_arn": "t"}]},
    ]
    targets = ash._scan_relational(table)
    assert {t["cluster_id"] for t in targets} == {"pg1", "pg2"}
    assert table.scan.call_count == 2


def test_first_row_parses_data_api_shape():
    rds = MagicMock()
    rds.execute_statement.return_value = {
        "columnMetadata": [{"name": "active"}, {"name": "top_wait"}, {"name": "top_wait_count"}],
        "records": [[{"longValue": 5}, {"stringValue": "Lock:tuple"}, {"longValue": 3}]],
    }
    row = ash._first_row(rds, "arn", "sec", "db", "SELECT 1")
    assert row == {"active": 5, "top_wait": "Lock:tuple", "top_wait_count": 3}


def test_first_row_empty_returns_none():
    rds = MagicMock()
    rds.execute_statement.return_value = {"columnMetadata": [], "records": []}
    assert ash._first_row(rds, "a", "s", "d", "SELECT 1") is None

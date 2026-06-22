import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_inst", _PATH)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


def test_instances_reads_cluster_meta():
    rows = [{"instances": json.dumps([{"id": "w1", "role": "writer", "class": "db.r6g.large"}])}]
    out = h._instances(lambda sql, params=None: rows, "c1")
    assert out["instances"][0]["id"] == "w1"
    assert out["instances"][0]["role"] == "writer"


def test_instances_empty_when_absent():
    out = h._instances(lambda sql, params=None: [], "c1")
    assert out == {"instances": []}


def test_batch_timeseries_instance_filter_in_sql():
    captured = {}
    def query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params or {}
        return []
    h._batch_timeseries(query, "c1", ["cpu"], 1, instance="r1")
    assert "jsonb_exists" in captured["sql"]
    assert captured["params"].get("inst") == "r1"


def test_batch_timeseries_excludes_instance_rows_by_default():
    captured = {}
    def query(sql, params=None):
        captured["sql"] = sql
        return []
    h._batch_timeseries(query, "c1", ["cpu"], 1)  # no instance
    assert "NOT jsonb_exists(dimensions, 'instance')" in captured["sql"]

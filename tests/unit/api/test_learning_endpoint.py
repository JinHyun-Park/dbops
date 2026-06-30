"""Task 8: /api/learning overview endpoint.

Tests that _learning_overview groups fleet rows (cluster_id='*') vs
per-cluster rows and returns recent resolved/persisted cases.
"""

import importlib.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading — push api/dashboard on sys.path so sibling imports resolve
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_HANDLER_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler", _HANDLER_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

dash = _mod


def teardown_module(_module):
    if str(_DASHBOARD_DIR) in sys.path:
        sys.path.remove(str(_DASHBOARD_DIR))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_learning_overview_groups_fleet_and_clusters():
    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [
                {
                    "cluster_id": "*",
                    "symptom_class": "anomaly:cpu",
                    "action_class": "manual",
                    "successes": 3,
                    "attempts": 4,
                    "last_outcome": "resolved",
                },
                {
                    "cluster_id": "c1",
                    "symptom_class": "finding:query_regression",
                    "action_class": "index_add",
                    "successes": 2,
                    "attempts": 2,
                    "last_outcome": "resolved",
                },
            ]
        if "FROM remediation_cases" in sql:
            return [
                {
                    "cluster_id": "c1",
                    "symptom_class": "finding:query_regression",
                    "action_class": "index_add",
                    "status": "resolved",
                    "evaluated_at": "t",
                }
            ]
        return []

    body = dash._learning_overview(query)
    assert len(body["fleet"]) == 1
    assert body["fleet"][0]["symptom_class"] == "anomaly:cpu"
    assert "c1" in body["clusters"]
    assert len(body["clusters"]["c1"]) == 1
    assert body["recent"][0]["status"] == "resolved"


def test_learning_overview_empty():
    """No rows → empty collections, no crash."""
    body = dash._learning_overview(lambda sql, params=None: [])
    assert body == {"fleet": [], "clusters": {}, "recent": []}

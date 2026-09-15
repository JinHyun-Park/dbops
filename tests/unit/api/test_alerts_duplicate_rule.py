"""Uniqueness guard on POST /api/alert-rules.

Measured before this existed: the one cluster in the fleet that had any
alert_rules row had `cpu > 80` twice, so every breach raised, notified and
deduped two identical alarms.

The guard is a single guarded INSERT (`WHERE NOT EXISTS`), so rows that are
ALREADY duplicated block a third copy instead of crashing the endpoint, which a
UNIQUE index could not do without deleting an operator's rules first.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_ALERTS_DIR = ROOT / "api" / "alerts"
sys.path.insert(0, str(_ALERTS_DIR))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
os.environ.setdefault("ALERT_TOPIC_ARN", "")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_HANDLER_PATH = _ALERTS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("alerts_handler_dup", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
sys.modules["alerts_handler_dup"] = handler
_spec.loader.exec_module(handler)


class _FakeRules:
    """alert_rules double that honours the guarded INSERT's NOT EXISTS.

    It does not parse SQL: it asserts the statement IS the guarded form and then
    applies the documented key, which is the behaviour under test. A double that
    always returns a row would pass with the guard deleted."""

    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]
        self.statements = []

    def key(self, row):
        return (row["cluster_id"], row["metric_type"], row["comparison"], row["threshold"])

    def query(self, sql, params=None):
        self.statements.append(sql)
        if "INSERT INTO alert_rules" not in sql:
            return []
        assert "NOT EXISTS" in sql, "create-rule INSERT is not guarded"
        candidate = {"cluster_id": params["cid"], "metric_type": params["metric"],
                     "comparison": params["comp"], "threshold": params["threshold"],
                     "conditions": None}
        existing = [r for r in self.rows
                    if r.get("conditions") is None and self.key(r) == self.key(candidate)]
        if existing:
            return []  # WHERE NOT EXISTS matched nothing -> zero rows inserted
        candidate["id"] = len(self.rows) + 1
        candidate["name"] = params["name"]
        candidate["enabled"] = params["enabled"]
        self.rows.append(candidate)
        return [dict(candidate)]


def _body(**over):
    b = {"cluster_id": "prod-aurora-pg", "metric_type": "cpu",
         "comparison": ">", "threshold": 80}
    b.update(over)
    return b


def test_first_rule_is_created():
    db = _FakeRules()
    resp = handler._create_rule(db.query, _body())
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["rule"]["metric_type"] == "cpu"
    assert len(db.rows) == 1


def test_identical_rule_is_rejected_not_duplicated():
    db = _FakeRules()
    assert handler._create_rule(db.query, _body())["statusCode"] == 201
    resp = handler._create_rule(db.query, _body())
    assert resp["statusCode"] == 409
    assert json.loads(resp["body"])["error"] == "duplicate_rule"
    assert len(db.rows) == 1, "the same (cluster, metric, comparison, threshold) was inserted twice"


def test_existing_duplicates_are_tolerated():
    """The row pair that is already in the live table must not crash the guard."""
    dup = {"cluster_id": "prod-aurora-pg", "metric_type": "cpu", "comparison": ">",
           "threshold": 80.0, "conditions": None}
    db = _FakeRules([dup, dict(dup)])
    resp = handler._create_rule(db.query, _body())
    assert resp["statusCode"] == 409
    assert len(db.rows) == 2


def test_different_threshold_or_metric_still_creates():
    db = _FakeRules()
    assert handler._create_rule(db.query, _body())["statusCode"] == 201
    assert handler._create_rule(db.query, _body(threshold=90))["statusCode"] == 201
    assert handler._create_rule(db.query, _body(metric_type="db_connections"))["statusCode"] == 201
    assert handler._create_rule(db.query, _body(comparison="<"))["statusCode"] == 201
    assert handler._create_rule(db.query, _body(cluster_id="prod-mysql"))["statusCode"] == 201
    assert len(db.rows) == 5


def test_guard_ignores_compound_rules():
    """A compound rule stores a denormalised copy of its FIRST operand in the
    legacy columns, so the guard must not treat it as the same rule."""
    compound = {"cluster_id": "prod-aurora-pg", "metric_type": "cpu", "comparison": ">",
                "threshold": 80.0, "conditions": {"logic": "and", "operands": []}}
    db = _FakeRules([compound])
    resp = handler._create_rule(db.query, _body())
    assert resp["statusCode"] == 201
    assert "conditions IS NULL" in db.statements[-1]

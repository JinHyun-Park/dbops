"""P2-⑦: alert_evaluator must not fire a snoozed rule.

The evaluator excludes snoozed rules via a WHERE guard on the rules-eligibility
query (real filtering happens in Postgres — see AGENTS.md's "no fixture
divorced from producer shape" lesson, so the guard clause itself is asserted
directly here, same pattern as the existing ::timestamptz-cast test in
test_evaluator.py). The row-handling tests then simulate what the DB returns
once that guard has (or hasn't) excluded a row, and confirm the trigger count
comes out right either way.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "data-pipeline" / "alert_evaluator" / "handler.py"


def _load(module_name):
    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _rds_rows(rows, cols):
    records = []
    for r in rows:
        rec = []
        for c in cols:
            v = r.get(c)
            if v is None:
                rec.append({"isNull": True})
            elif isinstance(v, bool):
                rec.append({"booleanValue": v})
            elif isinstance(v, int):
                rec.append({"longValue": v})
            elif isinstance(v, float):
                rec.append({"doubleValue": v})
            else:
                rec.append({"stringValue": str(v)})
        records.append(rec)
    return {"columnMetadata": [{"name": c} for c in cols], "records": records}


def _stub_env(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")
    monkeypatch.delenv("ALERT_TOPIC_ARN", raising=False)
    monkeypatch.delenv("ALERT_SNS_TOPIC_ARN", raising=False)


def test_rules_query_excludes_future_snoozed_rows(monkeypatch):
    """Regression guard: the eligibility SQL must carry the snooze predicate,
    otherwise a snoozed rule would be fetched and fire again."""
    _stub_env(monkeypatch)
    h = _load("alert_evaluator_snooze_sql")
    captured = {}
    mock_rds = MagicMock()

    def fake_execute(**kwargs):
        captured.setdefault("first_sql", kwargs["sql"])
        return _rds_rows([], ["id"])

    mock_rds.execute_statement.side_effect = fake_execute
    monkeypatch.setattr(h, "boto3", MagicMock(**{"client.return_value": mock_rds}))

    h.lambda_handler({}, None)
    assert "snooze_until IS NULL OR snooze_until <= NOW()" in captured["first_sql"]


def test_no_rules_fetched_means_nothing_triggers(monkeypatch):
    """Simulates the DB having excluded a future-snoozed rule: the rules
    query returns zero rows, so the evaluator triggers nothing."""
    _stub_env(monkeypatch)
    h = _load("alert_evaluator_snooze_empty")
    mock_rds = MagicMock()

    def fake_execute(**kwargs):
        return _rds_rows([], ["id", "cluster_id", "name", "metric_type", "comparison", "threshold", "conditions_json"])

    mock_rds.execute_statement.side_effect = fake_execute
    monkeypatch.setattr(h, "boto3", MagicMock(**{"client.return_value": mock_rds}))

    result = json.loads(h.lambda_handler({}, None)["body"])
    assert result["rules_evaluated"] == 0
    assert result["triggered"] == 0


def test_rule_fires_when_db_returns_it_as_eligible(monkeypatch):
    """Simulates a rule with snooze_until NULL/in the past: the DB returns
    it, and — given a metric value that would otherwise match — it fires."""
    _stub_env(monkeypatch)
    h = _load("alert_evaluator_snooze_fires")
    mock_rds = MagicMock()
    rule_cols = ["id", "cluster_id", "name", "metric_type", "comparison", "threshold", "conditions_json"]
    rule_row = {"id": 1, "cluster_id": "c1", "name": "cpu-rule", "metric_type": "cpu",
                "comparison": ">", "threshold": 80.0, "conditions_json": None}

    def fake_execute(**kwargs):
        sql = kwargs["sql"]
        if "FROM alert_rules" in sql:
            return _rds_rows([rule_row], rule_cols)
        if "FROM metric_snapshots" in sql:
            return _rds_rows([{"latest_value": 95.0}], ["latest_value"])
        if "alert_subscribers_managed" in sql:
            return _rds_rows([], ["id"])
        return _rds_rows([], [])

    mock_rds.execute_statement.side_effect = fake_execute
    monkeypatch.setattr(h, "boto3", MagicMock(**{"client.return_value": mock_rds}))

    result = json.loads(h.lambda_handler({}, None)["body"])
    assert result["rules_evaluated"] == 1
    assert result["triggered"] == 1
    assert result["skipped"] == 0

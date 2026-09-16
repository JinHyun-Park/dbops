"""An auto-RCA needs a TARGET, so an unresolvable alarm must not open one.

Measured on the live deployment before this guard: 4 of 21 auto-RCA tasks in
agent-tasks carried `cluster_id: "unknown"`, every one of them triggered by
`event:alarm_alarm`. A CloudWatch alarm whose configuration carries no
DBClusterIdentifier or DBInstanceIdentifier dimension falls back to that
literal, and there is no registered cluster called "unknown", so
diagnose_root_cause has no signals to read.

What those reports contained: exactly ONE candidate, the very event that
triggered the investigation. The narrative said so itself, unprompted, in
task 18066407: "클러스터 식별자가 'unknown'으로 수집되어...". A report whose only
finding is its own trigger is worse than no report, because it presents a
tautology as a ranked root cause.

Both directions are pinned. The event row must still be written for an
unresolvable alarm, because the alarm did fire, and the enqueue must still
happen for a resolvable one, so the guard cannot be "stop enqueuing".
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DIR = Path(__file__).resolve().parents[3] / "data-pipeline" / "event_processor"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("event_processor_handler", _DIR / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:1:cluster:cache")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:1:secret:cache")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")
    monkeypatch.delenv("ALERT_TOPIC_ARN", raising=False)


def _alarm_event(dimensions):
    """A real CloudWatch alarm-state-change shape. `dimensions` is the dict the
    handler digs out of configuration.metrics[0].metricStat.metric."""
    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": "dbops-cpu-high",
            "state": {"value": "ALARM", "reason": "Threshold Crossed: 1 datapoint [96.0]"},
            "configuration": {
                "metrics": [
                    {"metricStat": {"metric": {"namespace": "AWS/RDS",
                                               "name": "CPUUtilization",
                                               "dimensions": dimensions}}}
                ]
            },
        },
    }


def _run(event):
    """Returns (written_event_rows, enqueue_mock)."""
    rds = MagicMock()
    enq = MagicMock()
    fake_task_enqueue = MagicMock(enqueue_auto_rca=enq)
    with patch.object(handler.boto3, "client", return_value=rds), \
         patch.dict(sys.modules, {"task_enqueue": fake_task_enqueue}):
        resp = handler.lambda_handler(event, None)
    rows = [c for c in rds.execute_statement.call_args_list
            if "INSERT INTO event_log" in c.kwargs["sql"]]
    return resp, rows, enq


def _param(row, name):
    for p in row.kwargs["parameters"]:
        if p["name"] == name:
            return p["value"]["stringValue"]
    raise AssertionError(f"{name} not bound")


def test_an_alarm_with_a_cluster_dimension_opens_an_rca():
    """The control. Without this the guard below could pass by never enqueuing."""
    resp, rows, enq = _run(_alarm_event({"DBClusterIdentifier": "pg-prod-1"}))

    assert resp["statusCode"] == 200
    assert len(rows) == 1
    assert _param(rows[0], "cluster_id") == "pg-prod-1"
    assert enq.call_count == 1, "a resolvable alarm must still open an RCA"
    assert enq.call_args.args[0] == "pg-prod-1"


def test_an_instance_dimension_also_resolves():
    _, rows, enq = _run(_alarm_event({"DBInstanceIdentifier": "mysql-standalone-1"}))
    assert _param(rows[0], "cluster_id") == "mysql-standalone-1"
    assert enq.call_count == 1


def test_an_alarm_with_no_cluster_dimension_opens_no_rca():
    """The defect. An RCA against "unknown" can only rediscover its own trigger."""
    resp, rows, enq = _run(_alarm_event({"QueueName": "not-a-database"}))

    assert resp["statusCode"] == 200
    # The alarm DID fire, so the event is still recorded.
    assert len(rows) == 1, "the event row must still be written"
    assert _param(rows[0], "cluster_id") == "unknown"
    assert _param(rows[0], "severity") == "critical"
    # But no investigation is opened.
    assert enq.call_count == 0, "no RCA may be opened for an unresolvable cluster"


def test_an_empty_dimension_set_opens_no_rca():
    """A composite alarm or a metric-math alarm carries no dimensions at all."""
    _, rows, enq = _run(_alarm_event({}))
    assert len(rows) == 1
    assert enq.call_count == 0


def test_a_non_rca_worthy_event_still_opens_nothing():
    """Guards the guard: the new branch must not accidentally enqueue for an
    alarm_ok recovery, which is not an incident."""
    ev = _alarm_event({"DBClusterIdentifier": "pg-prod-1"})
    ev["detail"]["state"]["value"] = "OK"
    _, rows, enq = _run(ev)
    assert len(rows) == 1
    assert _param(rows[0], "event_type") == "alarm_ok"
    assert enq.call_count == 0

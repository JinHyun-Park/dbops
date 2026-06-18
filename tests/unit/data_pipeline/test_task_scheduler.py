"""task_scheduler — enqueue due recurring tasks.

Due detection lives in SQL (mocked here); these assert the loop: a due row
enqueues one pending agent-task + stamps last_run_at, no due rows enqueue
nothing, and a missing table is a safe no-op.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "data-pipeline" / "task_scheduler" / "handler.py"
_spec = importlib.util.spec_from_file_location("task_scheduler", PATH)
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


def _resource_returning(table):
    res = MagicMock()
    res.Table.return_value = table
    return res


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("AGENT_TASKS_TABLE", "t")


def test_enqueues_due_and_stamps():
    table = MagicMock()
    with patch.object(ts, "_query") as mq, \
         patch.object(ts.boto3, "resource", return_value=_resource_returning(table)):
        # call 1: DUE_SQL -> one due row; call 2: the last_run_at UPDATE
        mq.side_effect = [[{"id": 1, "cluster_id": "c1", "kind": "scheduled_report"}], []]
        out = ts.lambda_handler({}, None)
    assert out["enqueued"] == 1
    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["kind"] == "scheduled_report"
    assert item["trigger"] == "schedule:1"
    assert item["status"] == "pending"
    assert item["record_type"] == "task"
    # DUE query + last_run_at UPDATE both issued
    assert mq.call_count == 2


def test_no_due_no_enqueue():
    table = MagicMock()
    with patch.object(ts, "_query", return_value=[]), \
         patch.object(ts.boto3, "resource", return_value=_resource_returning(table)):
        out = ts.lambda_handler({}, None)
    assert out["enqueued"] == 0
    table.put_item.assert_not_called()


def test_noop_without_table(monkeypatch):
    monkeypatch.delenv("AGENT_TASKS_TABLE", raising=False)
    with patch.object(
        ts, "_query", return_value=[{"id": 1, "cluster_id": "c1", "kind": "scheduled_report"}]
    ):
        out = ts.lambda_handler({}, None)
    assert out["enqueued"] == 0

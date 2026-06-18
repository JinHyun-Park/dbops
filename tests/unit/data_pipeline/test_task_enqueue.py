"""auto-RCA enqueue (alert_evaluator) — dedupe + payload + fail-safe.

The enqueue must: skip when a recent auto_rca already exists for the cluster
(no duplicate RCAs on flapping alerts), write a well-formed pending row
otherwise, no-op when the table isn't configured, and never raise into the
alerting path.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "data-pipeline" / "alert_evaluator" / "task_enqueue.py"
_spec = importlib.util.spec_from_file_location("task_enqueue", PATH)
te = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(te)


def _resource_returning(table):
    res = MagicMock()
    res.Table.return_value = table
    return res


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_TABLE", "t")


def test_skips_when_recent_exists():
    table = MagicMock()
    table.query.return_value = {"Items": [{"task_id": "old"}]}
    with patch.object(te.boto3, "resource", return_value=_resource_returning(table)):
        out = te.enqueue_auto_rca("c1", 42)
    assert out is None
    table.put_item.assert_not_called()


def test_puts_when_none_recent():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    with patch.object(te.boto3, "resource", return_value=_resource_returning(table)):
        out = te.enqueue_auto_rca("c1", 42, title="boom")
    assert out  # returns the new task_id
    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["kind"] == "auto_rca"
    assert item["cluster_id"] == "c1"
    assert item["status"] == "pending"
    assert item["trigger"] == "alert:42"
    assert item["record_type"] == "task"
    assert item["title"] == "boom"
    assert "ttl" in item and "created_at" in item


def test_noop_without_table(monkeypatch):
    monkeypatch.delenv("AGENT_TASKS_TABLE", raising=False)
    assert te.enqueue_auto_rca("c1", 42) is None


def test_noop_without_cluster():
    assert te.enqueue_auto_rca("", 42) is None


def test_swallows_errors():
    with patch.object(te.boto3, "resource", side_effect=RuntimeError("ddb down")):
        # must not raise into the alert path
        assert te.enqueue_auto_rca("c1", 42) is None

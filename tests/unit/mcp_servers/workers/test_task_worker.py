"""task_worker — stream-driven agent task executor.

Covers the contract that keeps the single processing path correct:
  - INSERT-only (the worker's own running/done MODIFYs must not re-trigger work)
  - pending-only (a row already past pending is ignored)
  - idempotent claim (lost claim => no work runs)
  - auto_rca dispatch via the deterministic diagnose_root_cause tool (mocked)
  - unknown kind => row marked failed (not left hanging at running)
"""

from unittest.mock import MagicMock, patch

import mcp_servers.workers.task_worker as tw
import pytest
from botocore.exceptions import ClientError


def _insert(task_id="t1", kind="auto_rca", cluster_id="c1", status="pending"):
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "task_id": {"S": task_id},
                "kind": {"S": kind},
                "cluster_id": {"S": cluster_id},
                "status": {"S": status},
            }
        },
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_TABLE", "t")
    # No WS configured => _broadcast is a no-op; keeps tests offline.
    monkeypatch.delenv("WS_CONNECTIONS_TABLE", raising=False)
    monkeypatch.delenv("WS_MGMT_ENDPOINT", raising=False)


def test_skips_non_insert():
    rec = _insert()
    rec["eventName"] = "MODIFY"
    with patch.object(tw, "_table") as mt:
        out = tw.lambda_handler({"Records": [rec]}, None)
    assert out["processed"] == 0
    mt.assert_not_called()


def test_skips_non_pending():
    with patch.object(tw, "_table") as mt:
        out = tw.lambda_handler({"Records": [_insert(status="running")]}, None)
    assert out["processed"] == 0
    mt.assert_not_called()


def test_auto_rca_happy_path():
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "CPU spike 3x", "category": "metric_spike"}]}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca) as mrca, \
         patch.object(tw, "_broadcast") as mbc:
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    mrca.assert_called_once()
    # claim (pending->running) + finish (->done) == 2 writes
    assert table.update_item.call_count == 2
    payload = mbc.call_args[0][0]
    assert payload["task_kind"] == "rca_ready"
    assert payload["cluster_id"] == "c1"
    assert "CPU spike 3x" in payload["title"]


def test_lost_claim_runs_nothing():
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "diagnose_root_cause_impl") as mrca:
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 0
    assert out["skipped"] == 1
    mrca.assert_not_called()


def test_unknown_kind_marked_failed():
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert(kind="bogus_kind")]}, None)
    assert out["processed"] == 0
    # claim succeeded then work raised => finish(failed)
    assert table.update_item.call_count == 2
    last = table.update_item.call_args_list[-1].kwargs
    assert last["ExpressionAttributeValues"][":s"] == "failed"


def test_scheduled_report_happy_path():
    table = MagicMock()
    # health_status returns `health` as a STRING + cluster meta + current_metrics.
    health = {
        "health": "warning",
        "cluster": {"status": "available", "engine": "aurora-postgresql"},
        "current_metrics": [{"metric_type": "cpu", "avg_val": 42.0, "max_val": 88.0}],
    }
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "get_health_status_impl", return_value=health), \
         patch.object(tw, "_broadcast") as mbc:
        out = tw.lambda_handler(
            {"Records": [_insert(kind="scheduled_report")]}, None
        )
    assert out["processed"] == 1
    written = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"][":r"]
    assert written["report_kind"] == "health_digest"
    labels = {line["label"] for line in written["lines"]}
    assert "헬스" in labels and "cpu" in labels  # overall + per-metric lines
    payload = mbc.call_args[0][0]
    assert payload["task_kind"] == "report_ready"
    assert "리포트 준비됨" in payload["title"]


def test_float_scores_persisted_as_decimal():
    """diagnose_root_cause returns float scores/ratios; the worker MUST convert
    them to Decimal before writing or the DynamoDB resource rejects the result
    ("Float types are not supported") and the task fails."""
    from decimal import Decimal

    table = MagicMock()
    rca = {
        "status": "ok",
        "candidates": [{"summary": "spike", "score": 3.14, "score_breakdown": {"recency": 0.8}}],
    }
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    written = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"][":r"]

    def _no_floats(v):
        if isinstance(v, float):
            return False
        if isinstance(v, dict):
            return all(_no_floats(x) for x in v.values())
        if isinstance(v, list):
            return all(_no_floats(x) for x in v)
        return True

    assert _no_floats(written), "result still contains raw floats — DynamoDB will reject it"
    assert written["candidates"][0]["score"] == Decimal("3.14")


def test_empty_candidates_still_completes():
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value={"status": "ok", "candidates": []}), \
         patch.object(tw, "_broadcast") as mbc:
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    # done with a "no clear cause" summary
    assert "원인" in mbc.call_args[0][0]["title"]

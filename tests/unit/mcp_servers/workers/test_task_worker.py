"""task_worker: stream-driven agent task executor.

Covers the contract that keeps the single processing path correct:
  - INSERT-only (the worker's own running/done MODIFYs must not re-trigger work)
  - pending-only (a row already past pending is ignored)
  - idempotent claim (lost claim => no work runs)
  - auto_rca dispatch via the deterministic diagnose_root_cause tool (mocked)
  - unknown kind => row marked failed (not left hanging at running)
"""

import json
import time
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


def test_rca_hybrid_narrative(monkeypatch):
    """With a model configured, the worker layers a Korean narrative +
    recommendations (one Bedrock call) onto the deterministic signals."""
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")
    table = MagicMock()
    rca = {
        "status": "ok",
        "candidates": [{"summary": "CPU spike", "category": "metric_spike", "score": 3.0, "when": "t"}],
        "signals_examined": {},
    }
    bedrock = MagicMock()
    bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": '{"narrative":"메모리 압박이 원인입니다", "recommendations":["work_mem 조정","느린 쿼리 최적화"]}'}]}}
    }
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw.boto3, "client", return_value=bedrock), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    written = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"][":r"]
    assert written["narrative"] == "메모리 압박이 원인입니다"
    assert "work_mem 조정" in written["recommendations"]


def test_rca_narrative_skipped_without_model():
    """No model configured => no Bedrock call; the task still completes with
    the raw ranked signals (narrative is best-effort)."""
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "x", "category": "event"}]}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw.boto3, "client") as mclient, \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    mclient.assert_not_called()  # no RCA_NARRATIVE_MODEL_ID => no bedrock-runtime client


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

    assert _no_floats(written), "result still contains raw floats, DynamoDB will reject it"
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


def test_ticket_url_stored_and_broadcast_when_provider_returns_url():
    """When the ticketing provider creates a ticket, the worker persists
    ticket_url on the task row and surfaces it in the WS push."""
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "x", "category": "event"}]}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "get_provider") as mgp, \
         patch.object(tw, "_broadcast") as mbc:
        mgp.return_value.create_ticket.return_value = "https://tickets.example/INC-1"
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    finish = table.update_item.call_args_list[-1].kwargs
    assert finish["ExpressionAttributeValues"][":turl"] == "https://tickets.example/INC-1"
    assert "ticket_url = :turl" in finish["UpdateExpression"]
    assert mbc.call_args[0][0]["ticket_url"] == "https://tickets.example/INC-1"


def test_no_ticket_url_when_disabled():
    """Default seam (provider 'none'): nothing created, no ticket_url written or
    pushed, behaviour identical to before the seam existed."""
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "x", "category": "event"}]}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "_broadcast") as mbc:
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    finish = table.update_item.call_args_list[-1].kwargs
    assert ":turl" not in finish["ExpressionAttributeValues"]
    assert "ticket_url" not in mbc.call_args[0][0]


def test_auto_rca_records_trace_and_duration():
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "x", "category": "event"}],
           "signals_examined": {"events": 3, "metric_spikes": 2}}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    finish = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"]
    trace = finish[":trace"]
    assert isinstance(trace, list) and any(s["tool"] == "diagnose_root_cause" for s in trace)
    assert ":dur" in finish  # duration recorded


def test_failed_task_still_records_trace_and_duration():
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", side_effect=RuntimeError("boom")), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 0
    finish = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"]
    assert finish[":s"] == "failed"
    assert ":dur" in finish  # duration recorded even on failure


def test_failed_task_error_field_is_static():
    """`error` is persisted and rendered verbatim in the Tasks UI, so the stored
    text must not be the exception message (a Data API failure there carries the
    cache cluster ARN, the secret ARN and SQL). The class stays in `summary`."""
    table = MagicMock()
    leak = ("BadRequestException: relation \"cluster_meta\" does not exist; secret "
            "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbops-cache-AbCdEf")
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", side_effect=RuntimeError(leak)), \
         patch.object(tw, "_broadcast"):
        tw.lambda_handler({"Records": [_insert()]}, None)
    finish = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"]
    assert finish[":s"] == "failed"
    stored = finish[":e"]
    assert "cluster_meta" not in stored and "secretsmanager" not in stored
    assert "123456789012" not in stored
    assert "서버 로그" in stored
    assert finish[":sum"] == "작업 실패: RuntimeError"  # failure class still visible


def test_ticket_provider_failure_does_not_break_completion():
    """A provider that raises must not fail the task: it completes 'done' with
    no ticket_url (the seam is isolated)."""
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "x", "category": "event"}]}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "get_provider") as mgp, \
         patch.object(tw, "_broadcast"):
        mgp.return_value.create_ticket.side_effect = RuntimeError("provider down")
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1  # still done, not failed
    finish = table.update_item.call_args_list[-1].kwargs
    assert finish["ExpressionAttributeValues"][":s"] == "done"
    assert ":turl" not in finish["ExpressionAttributeValues"]


def test_narrative_sends_no_sampling_params_the_claude_5_family_rejects(monkeypatch):
    """The RCA narrative must not pin `temperature` or `topP`.

    MEASURED 2026-09-15 in ap-northeast-2, per model, via bedrock-runtime converse:

        global.anthropic.claude-sonnet-5   maxTokens OK | temperature REJECT | topP REJECT
        global.anthropic.claude-opus-5     maxTokens OK | temperature REJECT | topP REJECT
        global.anthropic.claude-haiku-4-5  maxTokens OK | temperature OK     | topP OK

    The Claude 5 family answers ValidationException "`temperature` is deprecated for
    this model" (and the same for `top_p`), while 4.5 still accepts both. So the
    parameter is not universally safe and not universally broken: it depends on which
    model RCA_NARRATIVE_MODEL_ID happens to point at.

    WHY THIS TEST EXISTS RATHER THAN A COMMENT. This regression shipped and ran
    unnoticed for 12 days. RCA_NARRATIVE_MODEL_ID moved to claude-sonnet-5 on
    2026-09-03 while _narrative still sent temperature=0.2, so the call raised on every
    RCA, _narrative returned None, and the task still completed `done` with the trace
    line "모델 미설정/실패 - 스킵". Every RCA shipped ranked candidates with no narrative
    and no recommendations, and nothing reported a failure.

    test_rca_hybrid_narrative above could not catch it: it mocks the bedrock client, and
    a MagicMock accepts any kwargs, including ones the real service rejects. A mock can
    never observe a server-side parameter rejection. So this test asserts on the kwargs
    the code actually PASSES, which is the part a mock does expose.
    """
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "global.anthropic.claude-sonnet-5")
    bedrock = MagicMock()
    bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": '{"narrative": "n", "recommendations": ["r"]}'}]}}
    }
    monkeypatch.setattr(tw.boto3, "client", lambda *a, **k: bedrock)

    out = tw._narrative("c1", {"candidates": [{"category": "event", "score": 1.0}],
                                  "signals_examined": {"events": 1}})
    assert out and out.get("narrative"), "narrative should be produced for a healthy call"

    assert bedrock.converse.called, "converse was never called; the assertions below prove nothing"
    cfg = bedrock.converse.call_args.kwargs["inferenceConfig"]
    banned = sorted(k for k in ("temperature", "topP", "top_p") if k in cfg)
    assert banned == [], (
        f"inferenceConfig pins {banned}, which the Claude 5 family rejects with "
        "ValidationException. Only maxTokens is safe across every model "
        "RCA_NARRATIVE_MODEL_ID can name."
    )
    assert "maxTokens" in cfg, "maxTokens must stay: an unbounded narrative is a cost risk"


def test_the_rca_anchors_on_the_observed_incident_not_on_its_own_execution():
    """The task row's observed_at must reach diagnose_root_cause as around_time.

    Without it the RCA anchors on task-execution time, which is a different moment
    from the incident. alert_evaluator reads MAX(value) over a 10-minute lookback on a
    rate(5 minutes) poll, so the breach is routinely 10-15 minutes older than the
    anchor. Measured on the 2026-08-30 auto-RCA: the CPU breach was at 19:54:00 and
    19:56:00, the anchor landed at 19:59:30, and CPU was already back to 49.6 by
    19:59:00. The analysis described the recovery rather than the incident.
    """
    table = MagicMock()
    table.get_item.return_value = {"Item": {"task_id": "t1", "status": "pending"}}
    seen = {}

    def fake_diagnose(cache, cluster_id, **kwargs):
        seen.update(kwargs)
        return {"status": "ok", "candidates": [], "signals_examined": {}}

    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", side_effect=fake_diagnose), \
         patch.object(tw, "_claim", return_value=True), \
         patch.object(tw, "_broadcast", return_value=0), \
         patch.object(tw, "_narrative", return_value=None):
        tw.lambda_handler({"Records": [{
            "eventName": "INSERT",
            "dynamodb": {"NewImage": {
                "task_id": {"S": "t1"},
                "kind": {"S": "auto_rca"},
                "cluster_id": {"S": "c1"},
                "status": {"S": "pending"},
                "observed_at": {"S": "2026-08-30T19:54:00+00:00"},
            }},
        }]}, None)

    assert seen.get("around_time") == "2026-08-30T19:54:00+00:00", (
        f"observed_at must become around_time; got {seen!r}"
    )


def test_a_task_with_no_observed_at_still_anchors_on_now():
    """Negative control. A manual RCA carries no observed_at because the DBA is
    looking at the present, and an empty string is what diagnose_root_cause reads as
    'anchor on now'. Without this test a change that required observed_at would pass
    the test above and break every manual RCA."""
    table = MagicMock()
    table.get_item.return_value = {"Item": {"task_id": "t2", "status": "pending"}}
    seen = {}

    def fake_diagnose(cache, cluster_id, **kwargs):
        seen.update(kwargs)
        return {"status": "ok", "candidates": [], "signals_examined": {}}

    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", side_effect=fake_diagnose), \
         patch.object(tw, "_claim", return_value=True), \
         patch.object(tw, "_broadcast", return_value=0), \
         patch.object(tw, "_narrative", return_value=None):
        tw.lambda_handler({"Records": [{
            "eventName": "INSERT",
            "dynamodb": {"NewImage": {
                "task_id": {"S": "t2"},
                "kind": {"S": "manual_rca"},
                "cluster_id": {"S": "c1"},
                "status": {"S": "pending"},
            }},
        }]}, None)

    assert seen.get("around_time") == "", f"missing observed_at must mean now; got {seen!r}"


# ---------------------------------------------------------------------------
# _narrative response-shape robustness
# ---------------------------------------------------------------------------

def _rca():
    return {"status": "ok", "signals_examined": {},
            "candidates": [{"summary": "CPU spike", "category": "metric_spike",
                            "score": 3.0, "when": "t"}]}


def _bedrock(blocks, stop_reason="end_turn"):
    b = MagicMock()
    b.converse.return_value = {
        "output": {"message": {"content": blocks}},
        "stopReason": stop_reason,
    }
    return b


def test_the_narrative_is_read_from_a_text_block_at_any_position(monkeypatch):
    """content[0] is not reliably the text block.

    MEASURED LIVE on 2026-09-15 against global.anthropic.claude-opus-5: one RCA
    produced a narrative and the next died on `KeyError: 'text'` with the same code,
    the same model and the same prompt shape. A response that leads with a reasoning
    block (or anything else) used to cost the entire narrative and the
    recommendations, which are the half of an RCA a DBA acts on.

    This file already carries a 12-day outage caused by assuming something about a
    model family: a pinned `temperature` the Claude 5 generation retired.
    """
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")
    bedrock = _bedrock([
        {"reasoningContent": {"reasoningText": {"text": "thinking out loud"}}},
        {"text": '{"narrative":"스키마 변경이 원인입니다","recommendations":["롤백 검토"]}'},
    ])
    with patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw.boto3, "client", return_value=bedrock):
        out = tw._narrative("c1", _rca())
    assert out is not None, "a leading non-text block still loses the whole narrative"
    assert out["narrative"] == "스키마 변경이 원인입니다"
    assert out["recommendations"] == ["롤백 검토"]


def test_a_response_with_no_text_block_logs_what_did_come_back(capsys, monkeypatch):
    """`KeyError: 'text'` said nothing about what the response actually contained and
    cost a live debugging round. The block keys and the stop reason are the two facts
    that make the next occurrence diagnosable from the log alone."""
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")
    bedrock = _bedrock([{"reasoningContent": {}}], stop_reason="max_tokens")
    with patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw.boto3, "client", return_value=bedrock):
        out = tw._narrative("c1", _rca())
    assert out is None
    log = capsys.readouterr().out
    assert "no text block" in log and "reasoningContent" in log, log
    assert "max_tokens" in log, log


def test_truncation_at_max_tokens_is_logged_as_the_cause(capsys, monkeypatch):
    """A response cut at the cap leaves the JSON incomplete, so json.loads fails and
    the narrative disappears with no stated reason. Saying so turns a silent
    degradation into a log line that names the fix."""
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")
    bedrock = _bedrock([{"text": '{"narrative":"원인은 스키마 변'}], stop_reason="max_tokens")
    with patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw.boto3, "client", return_value=bedrock):
        out = tw._narrative("c1", _rca())
    assert out is None
    log = capsys.readouterr().out
    assert "truncated at maxTokens" in log, log


def test_the_token_budget_has_headroom_over_the_measured_need(monkeypatch):
    """900 was ON the limit, not under it.

    Two identical opus-5 calls with this worker's own prompt spent 861 and 900 output
    tokens, the second stopping at `max_tokens`. A budget that a normal response
    reaches produces a truncated JSON and therefore no narrative, so the guard is on
    the number itself: a mocked client cannot observe a cap being hit.
    """
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")
    bedrock = _bedrock([{"text": '{"narrative":"n","recommendations":[]}'}])
    with patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw.boto3, "client", return_value=bedrock):
        tw._narrative("c1", _rca())
    cfg = bedrock.converse.call_args.kwargs["inferenceConfig"]
    assert cfg["maxTokens"] >= 1500, (
        f"maxTokens={cfg['maxTokens']} leaves no room over the measured 861-900"
    )


# ---------------------------------------------------------------------------
# published_at: the marker that a REPORT BECAME READABLE.
#
# The operator's complaint was that there is no visibility into when the latest
# content arrived. "New" therefore has to mean "a usable report is available",
# not "a row changed status", so the marker is stamped on the write that makes
# the result readable through the read API and on no other write.
# ---------------------------------------------------------------------------


def _finish_vals(table):
    """ExpressionAttributeValues + UpdateExpression of the LAST write (the finish)."""
    kw = table.update_item.call_args_list[-1].kwargs
    return kw["ExpressionAttributeValues"], kw["UpdateExpression"]


def test_published_at_is_stamped_when_a_readable_result_is_written():
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "CPU spike", "category": "metric_spike"}]}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    vals, expr = _finish_vals(table)
    assert "published_at" in expr, expr
    assert vals[":s"] == "done"
    assert ":r" in vals  # the stamp rides the write that puts the result on the row
    # One clock: it reuses the write's own timestamp rather than reading the
    # time a second time, so published_at and completed_at cannot disagree.
    assert "published_at = :ts" in expr and "completed_at = :ts" in expr
    # The claim write (pending -> running) must NOT carry it: that row has no
    # readable content yet.
    claim_expr = table.update_item.call_args_list[0].kwargs["UpdateExpression"]
    assert "published_at" not in claim_expr, claim_expr


def test_published_at_is_not_stamped_when_a_failure_writes_no_result():
    """A failure is a notification, not a published report. Stamping it would put
    every crashed task at the top of a "newest content" list.

    This is the live failure path, and it carries no result at all, so BOTH
    conditions in _finish block the stamp here and neither one is measured on
    its own. That is exactly why the status gate gets its own test below, on the
    pair no handler path can produce."""
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", side_effect=RuntimeError("boom")), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 0
    vals, expr = _finish_vals(table)
    assert vals[":s"] == "failed"
    assert ":r" not in vals  # nothing readable was written
    assert "published_at" not in expr, expr
    assert "completed_at" in expr  # the row still completed, it just published nothing


def test_published_at_is_not_stamped_for_a_result_written_with_a_non_done_status():
    """The status gate, called directly, because no handler path produces this
    pair: the done finish passes a result and the failure finish passes none.

    It is not decoration. api/tasks._published_at, the only definition of "new"
    on that route, falls back to completed_at solely for a row that is `done`
    AND carries a result, so "published" already implies "done" on the read
    side. A future partial-result failure path (say a report that half
    generated) must not stamp a row the reader would then announce as a newly
    arrived report; the result itself is still persisted, the row simply has no
    publication instant."""
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table):
        tw._finish("t1", status="failed", result={"partial": True}, summary="half done")
    vals, expr = _finish_vals(table)
    assert vals[":s"] == "failed"
    assert vals[":r"] == {"partial": True}  # the gate is on the stamp, not the result
    assert "#r = :r" in expr
    assert "published_at" not in expr, expr


def test_published_at_is_stamped_when_the_rca_found_nothing():
    """"No clear cause, manual check advised" is a real outcome the DBA needs to
    read, so it publishes like any other report."""
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl",
                      return_value={"status": "ok", "candidates": []}), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    vals, expr = _finish_vals(table)
    assert "published_at" in expr, expr
    assert vals[":s"] == "done"


def test_published_at_sorts_against_an_existing_created_at_as_a_string():
    """The GSI sort key on this table is a STRING, and every existing timestamp
    (created_at from api/tasks and alert_evaluator, started_at, completed_at) is
    `str(int(time.time() * 1000))`. published_at has to be byte-comparable
    against those, so a reader can range-query "published since X" the way
    alert_evaluator already range-queries created_at. An ISO string or a mixed
    width would sort wrongly against the rows already in the table."""
    older_row_created_at = str(int(time.time() * 1000) - 24 * 60 * 60 * 1000)
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl",
                      return_value={"status": "ok", "candidates": []}), \
         patch.object(tw, "_broadcast"):
        tw.lambda_handler({"Records": [_insert()]}, None)
    vals, _ = _finish_vals(table)
    published_at = vals[":ts"]

    assert isinstance(published_at, str), type(published_at)
    assert published_at.isdigit(), published_at          # no ISO, no separators
    assert len(published_at) == len(older_row_created_at) == 13  # fixed width
    assert older_row_created_at < published_at           # string ordering
    assert int(older_row_created_at) < int(published_at)  # == numeric ordering
    newer = str(int(published_at) + 60_000)
    assert sorted([newer, published_at, older_row_created_at]) == \
        sorted([newer, published_at, older_row_created_at], key=int)


# ---------------------------------------------------------------------------
# Advice dedupe: the operator should not read the same instruction twice.
#
# SCOPE: the narrative model's own `recommendations` list. A candidate's
# `suggested_action` is written in English by diagnose_root_cause's collectors
# while `recommendations` come back in Korean, so the two lists are never
# compared: no lexical matcher can pair them, and approximating cross-language
# matching risks dropping a DIFFERENT instruction, which is worse than showing a
# near-duplicate. Every fixture below therefore carries the shapes the producers
# really emit, English action plus Korean recommendations.
# ---------------------------------------------------------------------------

# Verbatim from mcp_servers/incident/tools/diagnose_root_cause.py, the lock-wait
# collector's action. English, like all five of them.
_REAL_ACTION = ("Identify the blocking transaction; long blocking chains may need "
                "a terminated session or an index/query fix.")


def test_dedupe_drops_a_paraphrase_and_keeps_genuinely_different_advice():
    """The model paraphrases itself, so matching is on meaning, not on string
    equality. It is also deliberately conservative: showing a near-duplicate is
    cheaper than dropping advice the operator needed."""
    res = {
        "candidates": [{"category": "lock_wait", "suggested_action": _REAL_ACTION}],
        "recommendations": [
            "블로킹 트랜잭션을 식별하고 필요 시 세션을 종료하세요",
            # A paraphrase of the line above: same instruction, new word order
            # and a different verb ending.
            "블로킹 트랜잭션을 식별한 뒤, 필요하면 세션을 종료하세요.",
            # Different advice that happens to share vocabulary with it.
            "블로킹 트랜잭션을 식별하고 필요 시 인덱스를 추가하세요",
            # Different advice about a different knob.
            "work_mem을 늘려 정렬 스필을 줄이세요",
        ],
    }
    dropped = tw._dedupe_advice(res)
    assert dropped == 1, res["recommendations"]
    assert res["recommendations"] == [
        "블로킹 트랜잭션을 식별하고 필요 시 세션을 종료하세요",
        "블로킹 트랜잭션을 식별하고 필요 시 인덱스를 추가하세요",
        "work_mem을 늘려 정렬 스필을 줄이세요",
    ]
    # The per-signal action is untouched: it is bound to that signal's own
    # evidence row, and a signal with no next step is worse than a repeat.
    assert res["candidates"][0]["suggested_action"] == _REAL_ACTION


def test_dedupe_also_drops_a_recommendation_that_repeats_an_earlier_one():
    """The two lists are not the only source of repetition: the model restates
    itself across candidates of the same category."""
    res = {"candidates": [], "recommendations": [
        "work_mem 값을 낮추세요",
        "work_mem 값을 낮추십시오.",
        "max_connections 값을 낮추세요",
    ]}
    assert tw._dedupe_advice(res) == 1
    assert res["recommendations"] == ["work_mem 값을 낮추세요", "max_connections 값을 낮추세요"]


def test_dedupe_will_not_merge_two_parameters_behind_one_sentence():
    """Overlap alone is not enough to call two lines the same instruction: advice
    about two different knobs can be word-for-word identical apart from the
    parameter name (0.71 overlap here). The identifiers have to match exactly
    first, or the operator silently loses one of the two changes."""
    res = {"candidates": [], "recommendations": [
        "shared_buffers 값을 늘려 캐시 적중률을 높이세요",
        "work_mem 값을 늘려 캐시 적중률을 높이세요",
    ]}
    assert tw._dedupe_advice(res) == 0
    assert len(res["recommendations"]) == 2


def test_the_worker_dedupes_before_persisting_the_result(monkeypatch):
    """Deduped where the result is ASSEMBLED, so the API, the Tasks page and any
    later reader all get the trimmed list instead of each fixing it."""
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")
    table = MagicMock()
    rca = {
        "status": "ok",
        "signals_examined": {},
        "candidates": [{"summary": "Lock waits", "category": "lock_wait", "score": 5.0,
                        "when": "t", "suggested_action": _REAL_ACTION}],
    }
    bedrock = _bedrock([{"text": json.dumps({
        "narrative": "락 경합이 원인입니다",
        "recommendations": ["블로킹 트랜잭션을 식별하고 필요 시 세션을 종료하세요",
                            "블로킹 트랜잭션을 식별한 뒤, 필요하면 세션을 종료하세요.",
                            "느린 쿼리에 인덱스를 추가하세요"],
    }, ensure_ascii=False)}])
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw.boto3, "client", return_value=bedrock), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    vals, _ = _finish_vals(table)
    stored = vals[":r"]
    assert stored["recommendations"] == ["블로킹 트랜잭션을 식별하고 필요 시 세션을 종료하세요",
                                         "느린 쿼리에 인덱스를 추가하세요"]
    # The per-candidate action survives, in the English the producer emitted.
    assert stored["candidates"][0]["suggested_action"] == _REAL_ACTION
    trace = vals[":trace"]
    assert any("중복 권장 1건 제거" in s.get("detail", "") for s in trace), trace

"""GET /api/tasks as the fleet-wide RCA inbox.

Pins the four properties the inbox rework is built on, each of which was a real
defect before it:

1. The list is a SUMMARY. The full ``result`` used to ship per row (127 KB for
   14 tasks on a 5s poll), so the heavy fields must be ABSENT from the list and
   still PRESENT on the single-task read.
2. Fleet-wide by default: no ?cluster means every cluster the caller may see,
   newest first, over recency-index.
3. Tenancy runs BEFORE any row, count or mark leaves the Lambda. An invisible
   cluster must be missing from the rows AND from every number in the payload.
4. The publication high-water mark is the newest published report the caller
   may see, computed fleet-wide rather than inferred from the page returned.

Plus the governing constraint: the row headline is a hypothesis, so no score
and no confidence figure appears in a list row.
"""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "api" / "tasks" / "handler.py"
_spec = importlib.util.spec_from_file_location("tasks_handler_inbox", PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# ---------------------------------------------------------------------------
# Fixtures / harness
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_TABLE", "agent-tasks-stub")
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def _jwt(username="u-viewer", groups=("dbops-viewer",)):
    claims = {"cognito:username": username, "cognito:groups": list(groups)}
    b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method="GET", qsp=None, task_id=None, path="/api/tasks"):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "pathParameters": {"id": task_id} if task_id else {},
        "queryStringParameters": qsp or {},
        "headers": {"authorization": f"Bearer {_jwt()}"},
        "body": None,
    }


class _FakeTable:
    """Two-read router.

    The list route reads twice: the page (``_list``) and then the fleet window
    for the publication mark (``_high_water_mark``). They are separate reads on
    purpose, so the fake answers them separately and records both call kwargs.
    """

    def __init__(self, page_rows, fleet_rows=None):
        self.page_rows = page_rows
        self.fleet_rows = page_rows if fleet_rows is None else fleet_rows
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        rows = self.page_rows if len(self.calls) == 1 else self.fleet_rows
        return {"Items": rows}


def _call(event, table):
    with patch.object(handler, "_table", return_value=table):
        return handler.lambda_handler(event, None)


def _visible(monkeypatch, allowed):
    """allowed=None means admin (no filter), else the visible cluster_id set."""
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry", lambda ev: allowed)


# ---------------------------------------------------------------------------
# Row builders. The heavy parts are deliberately big: the payload assertions
# below are only meaningful if the full result actually costs something.
# ---------------------------------------------------------------------------

_QUERY_TEXT = "SELECT o.id, o.total FROM orders o JOIN line_items li ON li.order_id = o.id WHERE o.created_at > now() - interval '1 day' " * 12


def _heavy_result(anchor="2026-09-15T19:54:00+00:00"):
    return {
        "status": "ok",
        "cluster_id": "c-open",
        "engine_family": "relational",
        "anchor_time": anchor,
        "window_minutes": 30,
        "candidates": [
            {
                "rank": 1,
                "category": "schema_change",
                "summary": "orders 테이블에 인덱스가 추가되었습니다",
                "score": 4.75,
                "when": anchor,
                "score_breakdown": {"base_weight": 5.0, "recency": 0.95},
                "evidence": {"query_text": _QUERY_TEXT, "rows_examined": 91234},
                "suggested_action": "변경 시점 전후 쿼리 계획을 비교하세요",
            },
            {
                "rank": 2,
                "category": "slow_query",
                "summary": "느린 쿼리 평균 응답이 3.2배 상승했습니다",
                "score": 1.9,
                "when": anchor,
                "score_breakdown": {"base_weight": 2.0, "recency": 0.95},
                "evidence": {"query_text": _QUERY_TEXT, "mean_ms": 812},
            },
        ],
        "signals_examined": {"schema_changes": 2, "events": 1, "slow_queries": 7},
        "skipped_sources": [],
        "scoring_weights": {"schema_change": 5.0, "slow_query": 2.0, "event": 3.0},
        "scoring_note": "score = base_weight x recency x 카테고리 인자",
        "schema_observation": {"confirmed": True},
        "narrative": "인덱스 추가 직후 느린 쿼리가 늘었습니다",
        "recommendations": ["변경 전후 EXPLAIN 비교", "인덱스 사용률 확인"],
    }


def _done_row(task_id, cluster_id, published_ms, *, stamped=True, anchor="2026-09-15T19:54:00+00:00"):
    row = {
        "task_id": task_id,
        "record_type": "task",
        "cluster_id": cluster_id,
        "kind": "auto_rca",
        "trigger": "alert:cpu-high",
        "status": "done",
        "created_at": str(published_ms - 90_000),
        "started_at": str(published_ms - 80_000),
        "completed_at": str(published_ms),
        "duration_ms": 4200,
        "title": f"자동 RCA ({cluster_id})",
        "summary": "orders 테이블에 인덱스가 추가되었습니다",
        "ticket_url": "https://tickets.example.test/DB-1",
        "result": _heavy_result(anchor),
        "trace": [
            {"step": "진단", "tool": "diagnose_root_cause", "ms": 1900, "detail": "7개 소스 검사, 후보 2"},
            {"step": "서술 생성", "tool": "bedrock", "ms": 2300, "detail": "한국어 narrative+권장조치"},
        ],
    }
    if stamped:
        row["published_at"] = str(published_ms)
    return row


def _pending_row(task_id, cluster_id, created_ms):
    return {
        "task_id": task_id,
        "record_type": "task",
        "cluster_id": cluster_id,
        "kind": "manual_rca",
        "trigger": "manual:alice",
        "status": "pending",
        "created_at": str(created_ms),
        "title": f"수동 RCA ({cluster_id})",
    }


def _failed_row(task_id, cluster_id, completed_ms):
    return {
        "task_id": task_id,
        "record_type": "task",
        "cluster_id": cluster_id,
        "kind": "auto_rca",
        "trigger": "alert:cpu-high",
        "status": "failed",
        "created_at": str(completed_ms - 5_000),
        "completed_at": str(completed_ms),
        "summary": "작업 실패: ClientError",
        "error": "작업 실행 중 오류가 발생했습니다. 자세한 원인은 서버 로그(CloudWatch)를 확인하세요.",
        "duration_ms": 5_000,
    }


# ---------------------------------------------------------------------------
# 1. The list is a summary
# ---------------------------------------------------------------------------

def test_list_row_omits_the_heavy_fields(monkeypatch):
    _visible(monkeypatch, None)
    table = _FakeTable([_done_row("t1", "c-open", 1_757_000_000_000)])
    resp = _call(_event(), table)
    assert resp["statusCode"] == 200
    body = resp["body"]
    row = json.loads(body)["tasks"][0]

    for absent in ("result", "trace", "candidates", "evidence", "query_text",
                   "scoring_weights", "score_breakdown", "started_at",
                   "completed_at", "ticket_url"):
        assert absent not in row, f"{absent} must not ship in a list row"
    # Not just missing from the row dict: nowhere in the serialized payload.
    for absent in ("query_text", "scoring_weights", "score_breakdown",
                   "narrative", "recommendations", "signals_examined"):
        assert absent not in body

    assert row["task_id"] == "t1"
    assert row["cluster_id"] == "c-open"
    assert row["kind"] == "auto_rca"
    assert row["trigger"] == "alert:cpu-high"
    assert row["status"] == "done"
    assert row["created_at"] == str(1_757_000_000_000 - 90_000)
    assert row["published_at"] == "1757000000000"
    # A real JSON number, not the "4200" a Decimal turns into under default=str.
    assert row["duration_ms"] == 4200 and isinstance(row["duration_ms"], int)
    assert row["summary"]
    assert row["title"]
    assert row["error"] is None
    # The finding headline: top candidate's category and its own summary line.
    assert row["finding"] == {
        "category": "schema_change",
        "summary": "orders 테이블에 인덱스가 추가되었습니다",
        "candidate_count": 2,
    }
    # Report time and incident time are different clocks; both are on the row.
    assert row["anchor_time"] == "2026-09-15T19:54:00+00:00"
    assert row["anchor_time"] != row["published_at"]


def test_list_row_carries_no_score_or_confidence(monkeypatch):
    """A ranking score is never rendered as certainty, so it never leaves here."""
    _visible(monkeypatch, None)
    table = _FakeTable([_done_row("t1", "c-open", 1_757_000_000_000)])
    body = _call(_event(), table)["body"]
    assert '"score"' not in body
    assert "confidence" not in body
    assert "4.75" not in body


def test_list_payload_is_a_fraction_of_the_full_result(monkeypatch):
    """The measured defect: three rows of full result vs three summary rows."""
    _visible(monkeypatch, None)
    rows = [_done_row(f"t{i}", "c-open", 1_757_000_000_000 + i) for i in range(3)]
    table = _FakeTable(rows)
    list_body = _call(_event(), table)["body"]
    full_one = json.dumps(rows[0], default=str)
    assert len(list_body) < len(full_one) / 2, (
        f"three summary rows ({len(list_body)}B) should cost less than half of "
        f"ONE full task ({len(full_one)}B)"
    )


def test_single_task_read_still_returns_the_heavy_fields(monkeypatch):
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {})
    row = _done_row("t1", "c-open", 1_757_000_000_000)

    class _T:
        def get_item(self, **kwargs):
            return {"Item": row}

    resp = _call(_event(task_id="t1", path="/api/tasks/t1"), _T())
    assert resp["statusCode"] == 200
    item = json.loads(resp["body"])
    assert item["result"]["candidates"][0]["evidence"]["query_text"] == _QUERY_TEXT
    assert item["result"]["candidates"][0]["score"] == 4.75
    assert item["result"]["scoring_weights"]["schema_change"] == 5.0
    assert item["result"]["anchor_time"] == "2026-09-15T19:54:00+00:00"
    assert len(item["trace"]) == 2
    assert item["ticket_url"]
    # Same publication field the inbox sorted by, so the client compares one clock.
    assert item["published_at"] == "1757000000000"


def test_single_task_read_backfills_published_at_for_a_legacy_row(monkeypatch):
    """Rows written before the producer stamp shipped: the publication instant
    is the completed_at of the write that put the result on the row."""
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {})
    row = _done_row("t-legacy", "c-open", 1_756_000_000_000, stamped=False)
    assert "published_at" not in row

    class _T:
        def get_item(self, **kwargs):
            return {"Item": row}

    item = json.loads(_call(_event(task_id="t-legacy", path="/api/tasks/t-legacy"), _T())["body"])
    assert item["published_at"] == "1756000000000"


# ---------------------------------------------------------------------------
# 2. Fleet-wide by default
# ---------------------------------------------------------------------------

def test_fleet_default_spans_more_than_one_cluster(monkeypatch):
    _visible(monkeypatch, None)
    rows = [
        _done_row("t1", "c-aurora-pg", 1_757_000_003_000),
        _done_row("t2", "c-rds-mysql", 1_757_000_002_000),
        _done_row("t3", "c-docdb", 1_757_000_001_000),
    ]
    table = _FakeTable(rows)
    body = json.loads(_call(_event(), table)["body"])
    assert {r["cluster_id"] for r in body["tasks"]} == {"c-aurora-pg", "c-rds-mysql", "c-docdb"}
    assert body["count"] == 3
    # Newest first, straight off the recency GSI.
    assert [r["task_id"] for r in body["tasks"]] == ["t1", "t2", "t3"]
    page = table.calls[0]
    assert page["IndexName"] == "recency-index"
    assert page["ScanIndexForward"] is False


def test_cluster_param_still_uses_the_per_cluster_gsi(monkeypatch):
    _visible(monkeypatch, None)
    table = _FakeTable([_done_row("t1", "c-open", 1_757_000_000_000)])
    _call(_event(qsp={"cluster": "c-open"}), table)
    assert table.calls[0]["IndexName"] == "cluster-created-index"


# ---------------------------------------------------------------------------
# 3. Tenancy before counts
# ---------------------------------------------------------------------------

def test_invisible_cluster_is_absent_from_rows_and_from_every_count(monkeypatch):
    """The one that must fail if the visibility filter moves after the count.

    c-hidden is the NEWEST published row in the window, so a filter applied too
    late leaks it three ways at once: as a row, inside `count`, and as the
    publication mark.
    """
    _visible(monkeypatch, {"c-open", "c-teamA"})
    rows = [
        _done_row("t-hidden", "c-hidden", 1_757_000_009_000),
        _done_row("t-open", "c-open", 1_757_000_002_000),
        _done_row("t-teamA", "c-teamA", 1_757_000_001_000),
    ]
    table = _FakeTable(rows)
    resp = _call(_event(), table)
    body = resp["body"]
    payload = json.loads(body)

    assert [r["cluster_id"] for r in payload["tasks"]] == ["c-open", "c-teamA"]
    assert payload["count"] == 2, "count must be computed from the FILTERED rows"
    assert payload["published_high_water_mark"] == "1757000002000", \
        "the mark must not move because of a cluster the caller cannot see"
    assert "c-hidden" not in body
    assert "t-hidden" not in body


def test_limit_trims_after_the_visibility_filter(monkeypatch):
    """A caller who cannot see part of the fleet still gets a full page."""
    _visible(monkeypatch, {"c-open"})
    rows = [_done_row("t-hidden", "c-hidden", 1_757_000_009_000)] + [
        _done_row(f"t{i}", "c-open", 1_757_000_000_000 + i) for i in range(3)
    ]
    table = _FakeTable(rows)
    payload = json.loads(_call(_event(qsp={"limit": "2"}), table)["body"])
    assert payload["count"] == 2
    assert payload["limit"] == 2
    assert {r["cluster_id"] for r in payload["tasks"]} == {"c-open"}
    # The invariant that makes `count` answerable from the rows themselves: a
    # count that disagrees with the rows is describing something the caller is
    # not allowed to see.
    assert len(payload["tasks"]) == payload["count"]


def test_stats_badge_counts_exclude_an_invisible_cluster(monkeypatch):
    """/stats is nothing but counts, so it is the purest indirect leak."""
    _visible(monkeypatch, {"c-open"})
    rows = [
        _done_row("t-open", "c-open", 1_757_000_001_000),
        _done_row("t-hidden", "c-hidden", 1_757_000_002_000),
        _failed_row("t-hidden2", "c-hidden", 1_757_000_003_000),
    ]
    with patch.object(handler, "_recent_fleet", return_value=rows):
        resp = handler.lambda_handler(_event(path="/api/tasks/stats"), None)
    stats = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert stats["total"] == 1
    assert stats["by_status"] == {"done": 1}
    assert stats["recent_failures"] == 0


# ---------------------------------------------------------------------------
# 4. Publication high-water mark
# ---------------------------------------------------------------------------

def test_high_water_mark_is_the_newest_published_report(monkeypatch):
    _visible(monkeypatch, None)
    rows = [
        _done_row("t1", "c-open", 1_757_000_007_000),
        _done_row("t2", "c-teamA", 1_757_000_005_000),
        _done_row("t3", "c-teamB", 1_757_000_006_000),
    ]
    table = _FakeTable(rows)
    payload = json.loads(_call(_event(), table)["body"])
    newest = max(r["published_at"] for r in rows)
    assert payload["published_high_water_mark"] == newest == "1757000007000"
    assert payload["high_water_mark_window"] == handler.HWM_WINDOW


def test_queued_and_failed_rows_never_count_as_published(monkeypatch):
    """"New" means a readable report arrived, not a status flip."""
    _visible(monkeypatch, None)
    rows = [
        _pending_row("t1", "c-open", 1_757_000_008_000),
        _failed_row("t2", "c-open", 1_757_000_009_000),
    ]
    table = _FakeTable(rows)
    payload = json.loads(_call(_event(), table)["body"])
    assert payload["published_high_water_mark"] is None
    assert [r["published_at"] for r in payload["tasks"]] == [None, None]
    assert [r["finding"] for r in payload["tasks"]] == [None, None]
    # A failed row still explains itself on the row.
    assert payload["tasks"][1]["error"]


def test_high_water_mark_is_not_inferred_from_the_returned_page(monkeypatch):
    """A filtered page carries no fleet truth, so the mark is its own read.

    ?status=pending returns rows that have published nothing. If the mark were
    a max over the returned rows it would read null here and reset whatever the
    client had persisted as "seen".
    """
    _visible(monkeypatch, None)
    page = [_pending_row("t-pending", "c-open", 1_757_000_010_000)]
    fleet = [_done_row("t-done", "c-open", 1_757_000_004_000)]
    table = _FakeTable(page, fleet_rows=fleet)
    payload = json.loads(_call(_event(qsp={"status": "pending"}), table)["body"])
    assert [r["task_id"] for r in payload["tasks"]] == ["t-pending"]
    assert payload["published_high_water_mark"] == "1757000004000"
    assert table.calls[1]["Limit"] == handler.HWM_WINDOW


def test_high_water_mark_reads_a_legacy_unstamped_report(monkeypatch):
    _visible(monkeypatch, None)
    rows = [_done_row("t-legacy", "c-open", 1_756_000_000_000, stamped=False)]
    table = _FakeTable(rows)
    payload = json.loads(_call(_event(), table)["body"])
    assert payload["published_high_water_mark"] == "1756000000000"

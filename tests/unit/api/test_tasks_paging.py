"""GET /api/tasks under a fleet BIGGER than one page: tenancy and kind.

Two measured defects, each reproduced here as the fleet shape that caused it.
Both are about a cap applied at the wrong moment, so the fake table below
models the two DynamoDB behaviours the defects live in and nothing else:

  * ``Limit`` counts rows READ, before ``FilterExpression`` and long before
    anything this handler does with tenancy.
  * a query truncated by ``Limit`` comes back with ``LastEvaluatedKey``, and
    that is the only thing that says whether rows remain behind the page.

1. THE LIMIT RAN BEFORE THE TENANCY FILTER. A 150-row fleet whose newest 100
   rows belong to a noisier tenant returned 0 rows and a null publication mark
   to the tenant owning rows 101 to 150, and a null mark never initializes the
   client watermark, so every freshness badge is stuck on a first visit
   forever. Narrowing to their own cluster cannot rescue it: a cluster-filtered
   page deliberately never advances the fleet mark.

2. NO kind FILTER AND NO CURSOR. scheduled_report digests carry status "done"
   exactly like a report, so ?status cannot separate them: 100 digests in front
   of 50 RCA reports pushed every report off the one page a finished analysis
   lives on, with no way to page back to them.

The rule both fixes are held to: a page may be short, but a short page must
say so. ``next_cursor`` is None only when the index is genuinely exhausted.
"""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "api" / "tasks" / "handler.py"
_spec = importlib.util.spec_from_file_location("tasks_handler_paging", PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_TABLE", "agent-tasks-stub")
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def _jwt(username="u-viewer", groups=("dbops-viewer",)):
    claims = {"cognito:username": username, "cognito:groups": list(groups)}
    b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(qsp=None):
    return {
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": "/api/tasks",
        "pathParameters": {},
        "queryStringParameters": qsp or {},
        "headers": {"authorization": f"Bearer {_jwt()}"},
        "body": None,
    }


def _visible(monkeypatch, allowed):
    """allowed=None means admin (no filter), else the visible cluster_id set."""
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry", lambda ev: allowed)


_INDEX_KEYS = {
    "recency-index": ("record_type", "created_at", "task_id"),
    "cluster-created-index": ("cluster_id", "created_at", "task_id"),
}


def _matches(cond, row):
    """Evaluate a boto3 condition object against a plain dict row.

    Only the three operators this handler builds: ``=`` (key conditions and
    ?status), ``IN`` (?kind) and ``AND``. Anything else raises rather than
    quietly passing every row, because a fake that ignores the filter would
    make the kind test pass with the filter deleted.
    """
    if cond is None:
        return True
    expr = cond.get_expression()
    op, values = expr["operator"], expr["values"]
    if op == "AND":
        return all(_matches(v, row) for v in values)
    name = values[0].name
    actual = str(row.get(name, ""))
    if op == "=":
        return actual == values[1]
    if op == "IN":
        return actual in values[1]
    raise AssertionError(f"fake table does not model operator {op!r}")


class _FakeTable:
    """A newest-first GSI over `rows`, with a real Limit and a real cursor."""

    def __init__(self, rows):
        self.rows = rows  # newest first, as both GSIs return them
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["IndexName"] in _INDEX_KEYS
        keys = _INDEX_KEYS[kwargs["IndexName"]]
        rows = [r for r in self.rows if _matches(kwargs.get("KeyConditionExpression"), r)]

        start = 0
        esk = kwargs.get("ExclusiveStartKey")
        if esk:
            assert set(esk) == set(keys), f"bad ExclusiveStartKey {sorted(esk)}"
            hit = [i for i, r in enumerate(rows) if str(r["task_id"]) == esk["task_id"]]
            assert hit, f"cursor points at an unknown row {esk['task_id']}"
            start = hit[0] + 1

        limit = kwargs.get("Limit") or len(rows)
        assert limit > 0
        window = rows[start:start + limit]
        out = {"Items": [r for r in window if _matches(kwargs.get("FilterExpression"), r)]}
        if len(window) == limit:
            # Truncated by Limit. DynamoDB hands back the last EVALUATED key,
            # filtered-out rows included, which is exactly why a filtered page
            # can come back empty with rows still behind it.
            out["LastEvaluatedKey"] = {k: str(window[-1][k]) for k in keys}
        return out


def _get(table, qsp=None):
    with patch.object(handler, "_table", return_value=table):
        resp = handler.lambda_handler(_event(qsp), None)
    return resp, json.loads(resp["body"])


# ---------------------------------------------------------------------------
# Fleet builders. created_at descends with the index order, so rows[0] is the
# newest row and rows[-1] the oldest.
# ---------------------------------------------------------------------------

_T0 = 1_757_000_000_000


def _row(task_id, cluster_id, created_ms, kind="auto_rca", published=True):
    row = {
        "task_id": task_id,
        "record_type": "task",
        "cluster_id": cluster_id,
        "kind": kind,
        "trigger": "alert:cpu-high" if kind == "auto_rca" else "schedule:7",
        "status": "done",
        "created_at": str(created_ms),
        "completed_at": str(created_ms + 4_000),
        "duration_ms": 4_000,
        "title": f"{kind} ({cluster_id})",
        "summary": "orders 테이블에 인덱스가 추가되었습니다",
        "result": {"candidates": [{"category": "schema_change", "summary": "인덱스 추가"}]}
        if kind != "scheduled_report"
        else {"lines": ["느린 쿼리 상위 5건", "헬스 요약"]},
    }
    if published:
        row["published_at"] = str(created_ms + 4_000)
    return row


def _noisy_then_mine(noisy=100, mine=50):
    """`noisy` newest rows for c-noisy, then `mine` published reports for
    c-mine. Newest first, so the caller's own rows sit past the first page."""
    rows = [_row(f"n{i}", "c-noisy", _T0 - i * 1_000) for i in range(noisy)]
    rows += [_row(f"m{i}", "c-mine", _T0 - (noisy + i) * 1_000) for i in range(mine)]
    return rows


def _digests_then_reports(digests=100, reports=50):
    rows = [_row(f"d{i}", "c-open", _T0 - i * 1_000, kind="scheduled_report")
            for i in range(digests)]
    rows += [_row(f"r{i}", "c-open", _T0 - (digests + i) * 1_000, kind="auto_rca")
             for i in range(reports)]
    return rows


# ---------------------------------------------------------------------------
# 1. The limit must not run before the tenancy filter
# ---------------------------------------------------------------------------

def test_minority_tenant_gets_a_full_page_from_behind_the_window(monkeypatch):
    """The measured defect: rows returned 0, mark None, watermark bricked."""
    _visible(monkeypatch, {"c-mine"})
    rows = _noisy_then_mine()
    resp, body = _get(_FakeTable(rows))

    assert resp["statusCode"] == 200
    assert body["count"] == 50, "a full page, not a page capped before tenancy ran"
    assert len(body["tasks"]) == 50
    assert {r["cluster_id"] for r in body["tasks"]} == {"c-mine"}
    # Newest of the caller's own rows first, straight off the recency order.
    assert [r["task_id"] for r in body["tasks"]] == [f"m{i}" for i in range(50)]
    assert "c-noisy" not in resp["body"]


def test_minority_tenant_watermark_can_initialize(monkeypatch):
    """A null mark is what bricks the client: advancePublishedMark(null) never
    writes, so isNewPublication is false forever and every badge reads as a
    first visit. The mark must be the caller's OWN newest publication."""
    _visible(monkeypatch, {"c-mine"})
    rows = _noisy_then_mine()
    _, body = _get(_FakeTable(rows))

    mine = [r for r in rows if r["cluster_id"] == "c-mine"]
    assert body["published_high_water_mark"] == max(r["published_at"] for r in mine)
    assert body["published_high_water_mark"] is not None
    # And still tenancy-bound: c-noisy published LATER than every c-mine row,
    # so a mark computed before the filter would be one of those stamps.
    noisy_stamps = {r["published_at"] for r in rows if r["cluster_id"] == "c-noisy"}
    assert body["published_high_water_mark"] not in noisy_stamps
    assert max(noisy_stamps, key=int) > body["published_high_water_mark"]


def test_a_short_page_is_never_silently_short(monkeypatch):
    """Paging is bounded per request, so the walk can stop with rows behind it.
    When it does, the cursor says so, and following it reaches those rows."""
    _visible(monkeypatch, {"c-mine"})
    rows = _noisy_then_mine(noisy=260, mine=50)
    resp, first = _get(_FakeTable(rows))

    assert first["count"] < 50, "this fleet is deeper than one request's budget"
    assert first["next_cursor"], "a short page must admit that more rows exist"
    # The mark reads a deeper window than the page, so freshness still works
    # even when the first page of rows comes back empty.
    assert first["published_high_water_mark"] is not None

    _, second = _get(_FakeTable(rows), {"cursor": first["next_cursor"]})
    seen = [r["task_id"] for r in first["tasks"]] + [r["task_id"] for r in second["tasks"]]
    assert len(seen) == 50 and len(set(seen)) == 50
    assert set(seen) == {f"m{i}" for i in range(50)}


# ---------------------------------------------------------------------------
# 2. kind filter and cursor
# ---------------------------------------------------------------------------

def test_kind_filter_reaches_reports_buried_under_recurring_digests(monkeypatch):
    """Measured as an admin: 100 rows returned, 0 of them auto_rca."""
    _visible(monkeypatch, None)
    resp, body = _get(_FakeTable(_digests_then_reports()), {"kind": "auto_rca", "limit": "50"})

    assert resp["statusCode"] == 200
    assert body["count"] == 50
    assert {r["kind"] for r in body["tasks"]} == {"auto_rca"}
    assert [r["task_id"] for r in body["tasks"]] == [f"r{i}" for i in range(50)]
    assert "scheduled_report" not in resp["body"]


def test_kind_filter_takes_several_kinds(monkeypatch):
    """The inbox view the UI needs is "RCA reports", which is two kinds."""
    _visible(monkeypatch, None)
    rows = (
        [_row("d0", "c-open", _T0, kind="scheduled_report")]
        + [_row("a0", "c-open", _T0 - 1_000, kind="auto_rca")]
        + [_row("x0", "c-open", _T0 - 2_000, kind="manual_rca")]
    )
    _, body = _get(_FakeTable(rows), {"kind": "auto_rca,manual_rca"})
    assert [r["task_id"] for r in body["tasks"]] == ["a0", "x0"]


def test_unknown_kind_is_rejected_not_ignored(monkeypatch):
    """Dropping a typo silently hands back the digest-flooded page the filter
    exists to escape, which reads as "there are no RCA reports"."""
    _visible(monkeypatch, None)
    resp, body = _get(_FakeTable(_digests_then_reports()), {"kind": "auto_rca,rca"})
    assert resp["statusCode"] == 400
    assert "auto_rca" in body["error"] and "scheduled_report" in body["error"]


def test_cursor_pages_back_through_the_whole_history(monkeypatch):
    """Without a cursor the reports behind 100 digests are simply unreachable."""
    _visible(monkeypatch, None)
    rows = _digests_then_reports()
    table = _FakeTable(rows)

    seen, cursor, pages = [], None, 0
    while pages < 10:
        pages += 1
        _, body = _get(table, {"cursor": cursor} if cursor else None)
        seen += [r["task_id"] for r in body["tasks"]]
        cursor = body["next_cursor"]
        if not cursor:
            break

    assert not cursor, "paging must terminate on an exhausted index"
    assert len(seen) == len(set(seen)) == 150, "no row repeated, none skipped"
    assert set(seen) == {r["task_id"] for r in rows}
    assert [t for t in seen if t.startswith("r")] == [f"r{i}" for i in range(50)]


def test_cursor_holds_its_place_when_the_page_was_trimmed(monkeypatch):
    """A page that over-fetched must resume from the last row it RETURNED, not
    from how far the scan read, or the trimmed rows vanish from history."""
    _visible(monkeypatch, {"c-mine"})
    # One invisible row then three visible ones, repeating. Asking for 2 rows
    # reads a first page that yields 1 and a second that yields 3, so the walk
    # holds 3 rows for a 2-row page and MUST drop the third rather than let the
    # next page start past it.
    rows, clock = [], _T0
    for i in range(10):
        rows.append(_row(f"n{i}", "c-noisy", clock))
        clock -= 1_000
        for j in range(3):
            rows.append(_row(f"m{i * 3 + j}", "c-mine", clock))
            clock -= 1_000
    table = _FakeTable(rows)

    seen, cursor = [], None
    for _ in range(40):
        qsp = {"limit": "2"}
        if cursor:
            qsp["cursor"] = cursor
        _, body = _get(table, qsp)
        seen += [r["task_id"] for r in body["tasks"]]
        cursor = body["next_cursor"]
        if not cursor:
            break

    assert set(seen) == {f"m{i}" for i in range(30)}
    assert len(seen) == 30, "a trimmed row must not be skipped by the next page"


def test_malformed_cursor_is_a_400_with_no_exception_text(monkeypatch):
    _visible(monkeypatch, None)
    resp, body = _get(_FakeTable(_digests_then_reports()), {"cursor": "not-a-cursor"})
    assert resp["statusCode"] == 400
    assert "cursor" in body["error"]
    for leak in ("Traceback", "Error(", "base64", "json"):
        assert leak not in resp["body"]


def test_a_fleet_cursor_is_rejected_on_a_cluster_page(monkeypatch):
    """The two GSIs take different key shapes, so a cursor minted on one is
    rejected here instead of reaching DynamoDB as a ValidationException."""
    _visible(monkeypatch, None)
    rows = _digests_then_reports()
    _, first = _get(_FakeTable(rows))
    assert first["next_cursor"]

    resp, body = _get(_FakeTable(rows), {"cluster": "c-open", "cursor": first["next_cursor"]})
    assert resp["statusCode"] == 400
    assert "cursor" in body["error"]


def test_exhausted_index_reports_no_cursor(monkeypatch):
    """The other half of the contract: a complete page must NOT claim more."""
    _visible(monkeypatch, None)
    rows = _digests_then_reports(digests=2, reports=1)
    _, body = _get(_FakeTable(rows), {"limit": "50"})
    assert body["count"] == 3
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# 3. The statistics badge is the SAME defect on a second endpoint
# ---------------------------------------------------------------------------


def _stats_event():
    ev = _event()
    ev["rawPath"] = "/api/tasks/stats"
    return ev


def _get_stats(table):
    with patch.object(handler, "_table", return_value=table):
        resp = handler.lambda_handler(_stats_event(), None)
    return resp, json.loads(resp["body"])


def test_the_badge_counts_the_callers_own_fleet_not_the_first_page_of_it(monkeypatch):
    """GET /api/tasks/stats described the newest 500 FLEET rows, then filtered.

    Same shape as the list defect: a caller whose rows all sit behind that
    window read zeros in a badge captioned as their own fleet. The window has
    to be FULL of rows the caller cannot see for the cap to bite, so the fleet
    here carries 500 noisy rows, one whole window, before any of the caller's.
    """
    _visible(monkeypatch, {"c-mine"})
    table = _FakeTable(_noisy_then_mine(noisy=500, mine=50))

    resp, body = _get_stats(table)

    assert resp["statusCode"] == 200
    assert body["total"] == 50, "the caller owns 50 rows, and they are all visible"
    assert body["by_status"] == {"done": 50}
    assert body["by_kind"] == {"auto_rca": 50}
    assert body["success_rate"] == 1.0
    # The badge must not describe the noisy tenant it cannot see, in either
    # direction: not by counting their rows, and not by reading zero because
    # they own the newest window.
    assert "c-noisy" not in json.dumps(body)


def test_the_badge_still_excludes_rows_the_caller_may_not_see(monkeypatch):
    """The mirror case, so the fix above cannot be "stop filtering". A tenant
    whose own rows ARE on the first page still gets only their own counted."""
    _visible(monkeypatch, {"c-mine"})
    rows = [_row(f"x{i}", "c-noisy", _T0 - i * 1_000) for i in range(20)]
    rows += [_row(f"m{i}", "c-mine", _T0 - (20 + i) * 1_000) for i in range(7)]
    table = _FakeTable(rows)

    resp, body = _get_stats(table)

    assert resp["statusCode"] == 200
    assert body["total"] == 7, "7 own rows out of a 27-row fleet, not 27"

"""diagnose_root_cause: "no DDL change in the window" vs "no DDL data at all".

`signals_examined` is pre-seeded to 0 for every source, so `skipped_sources` is
the ONLY thing that distinguishes those two. A missing schema_snapshots TABLE was
always reported as skipped; an EMPTY one was not, and once the table exists (E-4
creates it) that gap becomes the live case for every cluster whose family has no
snapshot producer.

That matters more here than for other signals: schema_change carries the highest
base weight in BASE_WEIGHTS, so a silently-absent producer does not merely lose a
signal, it systematically under-ranks the most common real cause of a regression.
"""

from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import (
    BASE_WEIGHTS,
    _collect_schema_changes,
)
from mcp_servers.shared.models import QueryResult

ANCHOR_START = "2026-07-01T11:00:00+00:00"
ANCHOR_END = "2026-07-01T13:00:00+00:00"

_EMPTY = QueryResult(columns=[], rows=[], row_count=0)


def _probe(snapshots, schemas):
    return QueryResult(
        columns=["snapshots", "schemas"],
        rows=[{"snapshots": snapshots, "schemas": schemas}],
        row_count=1,
    )


def _run(*results):
    cache = MagicMock()
    cache.execute.side_effect = list(results)
    examined, skipped = {}, []
    out = _collect_schema_changes(cache, "prod-pg-1", ANCHOR_START, ANCHOR_END,
                                  None, 60, examined, skipped)
    return out, examined, skipped, cache


def test_schema_change_is_the_highest_weighted_category():
    """Context for why the producer probe matters at all."""
    assert BASE_WEIGHTS["schema_change"] == max(BASE_WEIGHTS.values())


def test_no_snapshots_at_all_is_skipped_not_examined_zero():
    out, examined, skipped, _ = _run(_EMPTY, _probe(0, 0))
    assert out == []
    assert skipped == ["schema_changes"]
    assert "schema_changes" not in examined


def test_baseline_only_is_skipped_because_no_diff_could_exist():
    """One snapshot per schema cannot produce a diff row, so an empty window
    proves nothing about DDL."""
    out, examined, skipped, _ = _run(_EMPTY, _probe(3, 3))
    assert out == []
    assert skipped == ["schema_changes"]


def test_comparable_history_with_an_empty_window_is_a_real_negative():
    """Snapshots exist and at least one schema has two, so "no DDL change in this
    window" is supportable: examined, NOT skipped."""
    out, examined, skipped, _ = _run(_EMPTY, _probe(5, 2))
    assert out == []
    assert skipped == []
    assert examined["schema_changes"] == 0


def test_rows_in_the_window_skip_the_probe_entirely():
    out, examined, skipped, cache = _run(QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[{"snapshot_time": "2026-07-01T12:00:00+00:00", "schema_name": "app",
               "changes": '{"dropped": ["orders"]}'}],
        row_count=1,
    ))
    assert skipped == []
    assert examined["schema_changes"] == 1
    assert out[0]["category"] == "schema_change"
    assert out[0]["evidence"]["schema_name"] == "app"
    assert cache.execute.call_count == 1  # no probe on the happy path


def test_probe_failure_falls_back_to_skipped_not_to_a_clean_zero():
    cache = MagicMock()
    cache.execute.side_effect = [_EMPTY, RuntimeError("cache unavailable")]
    examined, skipped = {}, []
    out = _collect_schema_changes(cache, "prod-pg-1", ANCHOR_START, ANCHOR_END,
                                  None, 60, examined, skipped)
    assert out == []
    assert skipped == ["schema_changes"]
    assert "schema_changes" not in examined

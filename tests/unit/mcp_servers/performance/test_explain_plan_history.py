"""C3: opportunistic plan-history — structural signature + flip-vs-growth."""
import hashlib
from unittest.mock import MagicMock

import mcp_servers.performance.tools.explain_plan as ep
from mcp_servers.shared.models import QueryResult


def _expected_hash(nodes):
    return hashlib.md5(ep._plan_signature(nodes).encode()).hexdigest()


def test_signature_ignores_cost_but_tracks_structure():
    # Same node type + relation, wildly different cost → SAME signature (cost is
    # excluded, so a cost-only change reads as data growth, not a plan flip).
    a = [{"Node Type": "Seq Scan", "Relation Name": "t", "Total Cost": 100}]
    b = [{"Node Type": "Seq Scan", "Relation Name": "t", "Total Cost": 999999}]
    assert ep._plan_signature(a) == ep._plan_signature(b)
    # Different access method → different signature (a plan flip).
    c = [{"Node Type": "Index Scan", "Relation Name": "t", "Index Name": "t_pk"}]
    assert ep._plan_signature(a) != ep._plan_signature(c)


def test_normalize_query_collapses_literals():
    """The invariant Codex flagged: same logical query must hash the same across
    runs regardless of literal values (numeric AND string)."""
    n = ep._normalize_query
    assert n("SELECT * FROM orders WHERE id = 1") == n("SELECT * FROM orders WHERE id = 2")
    assert n("SELECT * FROM t WHERE name = 'alice'") == n("SELECT * FROM t WHERE name = 'bob'")
    # genuinely different queries stay distinct
    assert n("SELECT * FROM orders WHERE id = 1") != n("SELECT * FROM customers WHERE id = 1")


def test_first_seen_when_no_prior():
    cache = MagicMock()
    cache.execute.side_effect = [QueryResult([], [], 0), {}]  # SELECT (none), INSERT
    out = ep._capture_plan_history(
        cache, "c1", "select 1", [{"Node Type": "Result"}]
    )
    assert out["first_seen"] is True


def test_unchanged_plan_reads_as_data_growth():
    nodes = [{"Node Type": "Index Scan", "Relation Name": "orders", "Index Name": "orders_pk"}]
    cache = MagicMock()
    cache.execute.side_effect = [
        QueryResult(
            ["plan_hash", "captured_at"],
            [{"plan_hash": _expected_hash(nodes), "captured_at": "2026-05-01T00:00:00Z"}],
            1,
        ),
        {},  # INSERT
    ]
    out = ep._capture_plan_history(cache, "c1", "select * from orders where id = 1", nodes)
    assert out["changed"] is False
    assert "data growth" in out["note"].lower()


def test_changed_plan_reads_as_flip():
    nodes = [{"Node Type": "Seq Scan", "Relation Name": "orders"}]
    cache = MagicMock()
    cache.execute.side_effect = [
        QueryResult(
            ["plan_hash", "captured_at"],
            [{"plan_hash": "OLD_DIFFERENT_HASH", "captured_at": "2026-04-01T00:00:00Z"}],
            1,
        ),
        {},  # INSERT
    ]
    out = ep._capture_plan_history(cache, "c1", "select * from orders where id = 1", nodes)
    assert out["changed"] is True
    assert "flip" in out["note"].lower()
    assert out["previous_seen"] == "2026-04-01T00:00:00Z"

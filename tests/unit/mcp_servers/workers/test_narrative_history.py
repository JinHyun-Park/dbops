"""test_narrative_history — _history_line helper unit tests.

Tests the helper that fetches remediation track record from
remediation_outcomes_agg and formats a one-line prompt insert.
"""

from unittest.mock import MagicMock, call

import mcp_servers.workers.task_worker as tw


def _make_cache(rows):
    cache = MagicMock()
    cache.execute.return_value.rows = rows
    return cache


def test_history_line_built_from_agg():
    cache = _make_cache([
        {"action_class": "index_add", "successes": 4, "attempts": 5},
        {"action_class": "param_change", "successes": 1, "attempts": 3},
    ])
    line = tw._history_line(cache, "c1", "lock_contention")
    assert "index_add" in line and "4/5" in line
    assert "param_change" in line and "1/3" in line
    assert "과거 효과 이력" in line


def test_history_line_empty_when_no_rows():
    """No cluster or fleet rows -> returns empty string (caller omits the line)."""
    cache = _make_cache([])
    line = tw._history_line(cache, "c1", "cpu_spike")
    assert line == ""


def test_history_line_fallback_to_fleet():
    """If cluster row is empty, falls back to cluster_id='*' fleet row."""
    cache = MagicMock()
    # First call (cluster-specific) returns empty; second call (fleet) returns data.
    cache.execute.side_effect = [
        MagicMock(rows=[]),
        MagicMock(rows=[{"action_class": "vacuum_run", "successes": 7, "attempts": 8}]),
    ]
    line = tw._history_line(cache, "c1", "bloat")
    assert "vacuum_run" in line and "7/8" in line
    # Verify the second call used cluster_id='*'
    second_call_params = cache.execute.call_args_list[1][0][1]
    assert second_call_params.get("sc") == "rca:bloat"
    assert "cid" not in second_call_params  # fleet query has no :cid param


def test_history_line_returns_empty_on_cache_error():
    """Cache failure must never propagate — returns '' (best-effort)."""
    cache = MagicMock()
    cache.execute.side_effect = RuntimeError("db unavailable")
    line = tw._history_line(cache, "c1", "memory_pressure")
    assert line == ""


def test_history_line_symptom_class_prefix():
    """Verifies rca:<category> is used as the symptom_class filter."""
    cache = _make_cache([
        {"action_class": "restart_node", "successes": 2, "attempts": 2},
    ])
    tw._history_line(cache, "c1", "oom_kill")
    first_call_params = cache.execute.call_args_list[0][0][1]
    assert first_call_params.get("sc") == "rca:oom_kill"

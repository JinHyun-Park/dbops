from unittest.mock import MagicMock

from mcp_servers.incident.tools.remediation_history import get_remediation_history_impl
from mcp_servers.shared.models import QueryResult


def _empty():
    return QueryResult(columns=[], rows=[], row_count=0)


def test_returns_actions_for_symptom():
    cache = MagicMock()
    cache.execute.return_value.rows = [{"action_class": "index_add", "successes": 4, "attempts": 5}]
    out = get_remediation_history_impl(cache, "c1", "finding:query_regression")
    assert out["actions"][0]["action_class"] == "index_add"


def test_returns_both_actions_and_recent():
    cache = MagicMock()
    agg_rows = QueryResult(
        columns=["action_class", "symptom_class", "successes", "attempts", "last_outcome"],
        rows=[{"action_class": "index_add", "symptom_class": "finding:query_regression",
               "successes": 3, "attempts": 4, "last_outcome": "resolved"}],
        row_count=1,
    )
    recent_rows = QueryResult(
        columns=["symptom_class", "action_class", "status", "evaluated_at"],
        rows=[{"symptom_class": "finding:query_regression", "action_class": "index_add",
               "status": "resolved", "evaluated_at": "2026-06-01T00:00:00Z"}],
        row_count=1,
    )
    cache.execute.side_effect = [agg_rows, recent_rows]
    out = get_remediation_history_impl(cache, "c1", "finding:query_regression")
    assert len(out["actions"]) == 1
    assert len(out["recent"]) == 1
    assert out["recent"][0]["status"] == "resolved"


def test_no_symptom_class_omits_filter():
    cache = MagicMock()
    cache.execute.side_effect = [_empty(), _empty()]
    out = get_remediation_history_impl(cache, "c1")
    assert out == {"actions": [], "recent": []}
    # neither the agg nor the recent query should carry a symptom_class filter
    for call in cache.execute.call_args_list:
        sql, params = call[0]
        assert ":sc" not in sql
        assert "sc" not in params


def test_symptom_class_filter_included_when_provided():
    cache = MagicMock()
    cache.execute.side_effect = [_empty(), _empty()]
    get_remediation_history_impl(cache, "c1", "finding:query_regression")
    first_sql, first_params = cache.execute.call_args_list[0][0]
    assert "symptom_class" in first_sql
    assert first_params.get("sc") == "finding:query_regression"


def test_recent_scoped_by_symptom_class_when_provided():
    """recent[] must carry the symptom_class filter — not return all symptoms for the cluster."""
    cache = MagicMock()
    cache.execute.side_effect = [_empty(), _empty()]
    get_remediation_history_impl(cache, "c1", "finding:query_regression")
    calls = cache.execute.call_args_list
    assert len(calls) == 2
    # second call = recent query
    recent_sql, recent_params = calls[1][0]
    assert ":sc" in recent_sql
    assert recent_params.get("sc") == "finding:query_regression"


def test_recent_cluster_wide_when_no_symptom_class():
    """When symptom_class is omitted, recent query must NOT filter by symptom_class."""
    cache = MagicMock()
    cache.execute.side_effect = [_empty(), _empty()]
    get_remediation_history_impl(cache, "c1")
    recent_sql, recent_params = cache.execute.call_args_list[1][0]
    assert ":sc" not in recent_sql
    assert "sc" not in recent_params


def test_cluster_id_always_bound():
    cache = MagicMock()
    cache.execute.side_effect = [_empty(), _empty()]
    get_remediation_history_impl(cache, "prod-cluster-42")
    for call in cache.execute.call_args_list:
        _, params = call[0]
        assert params.get("cid") == "prod-cluster-42"

from unittest.mock import MagicMock

from mcp_servers.incident.tools.similar_incidents import (
    _tokenize,
    find_similar_incidents_impl,
)
from mcp_servers.shared.models import QueryResult


def _empty():
    return QueryResult(columns=[], rows=[], row_count=0)


def test_tokenize_extracts_keywords():
    kws = _tokenize("High CPU and connection spike on the cluster!")
    # short tokens / stopwords dropped, de-duped, lowercased
    assert "cpu" in kws
    assert "connection" in kws
    assert "spike" in kws
    assert "and" not in kws
    assert "the" not in kws
    assert "on" not in kws


def test_tokenize_empty_returns_empty():
    assert _tokenize("") == []
    assert _tokenize("a to is") == []  # all too short / stopwords


def test_returns_matches_when_events_match_symptoms():
    mock_cache = MagicMock()
    event_rows = QueryResult(
        columns=["cluster_id", "event_time", "event_type", "severity", "source", "message", "match_count"],
        rows=[
            {
                "cluster_id": "prod-pg-1",
                "event_time": "2026-05-01T00:00:00Z",
                "event_type": "alert",
                "severity": "critical",
                "source": "dbops-alert-evaluator",
                "message": "High CPU utilization detected on writer",
                "match_count": 2,
            }
        ],
        row_count=1,
    )
    # first call = event search (returns a hit), second call = runbook search (empty)
    mock_cache.execute.side_effect = [event_rows, _empty()]

    result = find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="high CPU utilization spike",
    )

    assert result["cluster_id"] == "prod-pg-1"
    assert result["count"] == 1
    assert len(result["similar_incidents"]) == 1
    inc = result["similar_incidents"][0]
    assert inc["kind"] == "event"
    assert inc["severity"] == "critical"
    assert "cpu" in inc["why_matched"]
    assert "found" in result["note"].lower()


def test_includes_matching_runbooks():
    mock_cache = MagicMock()
    runbook_rows = QueryResult(
        columns=["id", "cluster_id", "title", "summary_md", "tags", "source", "created_at", "match_count"],
        rows=[
            {
                "id": 7,
                "cluster_id": None,
                "title": "Resolving autovacuum stalls",
                "summary_md": "Playbook for high bloat / autovacuum issues",
                "tags": ["autovacuum", "bloat"],
                "source": "chat",
                "created_at": "2026-04-01T00:00:00Z",
                "match_count": 1,
            }
        ],
        row_count=1,
    )
    # events empty for cluster, empty fleet-wide, then runbook hit
    mock_cache.execute.side_effect = [_empty(), _empty(), runbook_rows]

    result = find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="autovacuum bloat problem",
    )

    assert result["count"] == 1
    rb = result["similar_incidents"][0]
    assert rb["kind"] == "runbook"
    assert rb["runbook_id"] == 7
    assert "autovacuum" in rb["why_matched"]


def test_falls_back_to_fleet_wide_when_cluster_has_no_events():
    mock_cache = MagicMock()
    fleet_rows = QueryResult(
        columns=["cluster_id", "event_time", "event_type", "severity", "source", "message", "match_count"],
        rows=[
            {
                "cluster_id": "other-pg-9",
                "event_time": "2026-05-02T00:00:00Z",
                "event_type": "alert",
                "severity": "warning",
                "source": "dbops-monitor",
                "message": "Replica lag growing rapidly",
                "match_count": 1,
            }
        ],
        row_count=1,
    )
    # cluster-scoped events empty -> fleet-wide returns hit -> runbooks empty
    mock_cache.execute.side_effect = [_empty(), fleet_rows, _empty()]

    result = find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="replica lag growing",
    )

    assert result["count"] == 1
    inc = result["similar_incidents"][0]
    assert inc["cluster_id"] == "other-pg-9"
    assert inc["scope"] == "fleet"
    # event search invoked twice (cluster then fleet) + one runbook search
    assert mock_cache.execute.call_count == 3


def test_empty_and_note_when_no_matches():
    mock_cache = MagicMock()
    # cluster events empty, fleet events empty, runbooks empty
    mock_cache.execute.side_effect = [_empty(), _empty(), _empty()]

    result = find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="some exotic never-seen symptom",
    )

    assert result["count"] == 0
    assert result["similar_incidents"] == []
    assert "no similar" in result["note"].lower()


def test_no_keywords_returns_graceful_note():
    mock_cache = MagicMock()
    result = find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="a to is",  # all stopwords / too short
    )
    assert result["count"] == 0
    assert result["similar_incidents"] == []
    assert "keyword" in result["note"].lower()
    # must not hit the DB when there is nothing to search
    mock_cache.execute.assert_not_called()


def test_uses_parameterized_ilike_query():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [_empty(), _empty(), _empty()]
    find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="deadlock contention",
    )
    first_sql, first_params = mock_cache.execute.call_args_list[0][0]
    assert "ILIKE" in first_sql
    assert ":kw0" in first_sql
    # keyword bound as a wildcarded, parameterized value (no string interpolation)
    assert any(v == "%deadlock%" for v in first_params.values())
    assert first_params["cluster_id"] == "prod-pg-1"

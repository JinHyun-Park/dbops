"""DocumentDB profiler rows in query_stats must be VISIBLE to the agent, and labelled.

ee0a63c / 7eeee72 / ff48098 built the DocumentDB profiler ingestion pipeline:
docdb_mongo_collector accumulates each grid-aligned profiler log window into
query_stats. The performance handler gates get_top_queries / get_slow_queries /
detect_regressions on the `query_stats` capability, which was False for
documentdb, so those rows were DARK: a DBA asking the agent for DocumentDB slow
ops got unsupported_engine while the rows sat in the table. Same failure class as
a tool that is implemented but never registered in cdk/tool_definitions.py.

Flipping the capability is only half the fix. The rows carry the same COLUMNS as
pg_stat_statements and a different MEANING:
  * calls / total_time_ms / rows_returned cover ONLY the ops that crossed
    profiler_threshold_ms, so they are not the shape's real traffic.
  * mean_time_ms is that censored subset's mean, always >= the threshold.
  * query_text is a Mongo op shape, not SQL, so it is not EXPLAIN input.
  * a window with no slow op writes no row at all, so "no rows" is not "healthy".
So the handler labels every row it returns for this family, and these tests pin
the label STRUCTURALLY (which keys exist, for which families) rather than by
matching prose.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

import mcp_servers.performance.handler as handler  # noqa: E402
from mcp_servers.shared.cache_client import CacheClient  # noqa: E402
from mcp_servers.shared.models import QueryResult  # noqa: E402

_QUERY_TOOLS = {
    "get_top_queries": {"cluster_id": "docdb-prod-1"},
    # 100ms, the DocumentDB profiler default threshold. The tool default (1000ms)
    # would filter out real profiler rows, which is correct behaviour, not a bug.
    "get_slow_queries": {"cluster_id": "docdb-prod-1", "threshold_ms": 100.0},
    "detect_regressions": {"cluster_id": "docdb-prod-1", "change_point": "2026-07-28T00:00:00Z"},
}

# What the profiler accumulator actually leaves in query_stats: an op shape in
# query_text, cumulative counters over slow ops only, snapshot_time = window end.
_PROFILER_ROWS = [
    {
        "cluster_id": "docdb-prod-1",
        "snapshot_time": "2026-07-28T02:05:00+00:00",
        "query_hash": "9f2c41aa77b0e5d3c8146ba20f5e7731",
        "query_text": "query shop.orders {customerId, sort}",
        "calls": 12,
        "total_time_ms": 4380.0,
        "mean_time_ms": 365.0,
        "rows_returned": 240,
    },
    {
        "cluster_id": "docdb-prod-1",
        "snapshot_time": "2026-07-28T02:05:00+00:00",
        "query_hash": "2b7de90114cc6f8a5530e1d47a9b0c62",
        "query_text": "aggregate shop.events {$match, $group}",
        "calls": 3,
        "total_time_ms": 5100.0,
        "mean_time_ms": 1700.0,
        "rows_returned": 9,
    },
]

_REGRESSION_ROWS = [
    {
        "query_hash": "9f2c41aa77b0e5d3c8146ba20f5e7731",
        "query_text": "query shop.orders {customerId, sort}",
        "before_mean_ms": 180.0,
        "after_mean_ms": 620.0,
        "before_calls": 40,
        "after_calls": 52,
        "change_pct": 244.4,
    },
]


class _FakeCache:
    """Real _build_query (no AWS, so a regression in the builder still shows up),
    canned execute."""

    _build_query = CacheClient._build_query

    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or {}))
        cols = list(self.rows[0]) if self.rows else []
        return QueryResult(columns=cols, rows=self.rows, row_count=len(self.rows))


class _Ctx:
    def __init__(self, tool_name):
        self.client_context = MagicMock()
        self.client_context.custom = {"bedrockAgentCoreToolName": f"x___{tool_name}"}


def _invoke(tool_name, event, fam, rows):
    fake = _FakeCache(rows)
    with patch.object(handler, "_resolve_family", lambda cid: fam), \
            patch.object(handler, "cache", fake):
        raw = handler.lambda_handler(event, _Ctx(tool_name))
    return json.loads(raw["content"][0]["text"]), fake


def _rows_for(tool_name):
    return _REGRESSION_ROWS if tool_name == "detect_regressions" else _PROFILER_ROWS


def _payload_rows(tool_name, result):
    return result["regressions"] if tool_name == "detect_regressions" else result["queries"]


# ---------------------------------------------------------------------------
# 1. The dark rows are reachable again.
# ---------------------------------------------------------------------------


def test_all_three_query_tools_return_documentdb_profiler_rows():
    """The bug: unsupported_engine while the rows existed. Assert real data comes
    back, not just that the refusal is gone."""
    for tool, event in _QUERY_TOOLS.items():
        rows = _rows_for(tool)
        result, fake = _invoke(tool, event, "documentdb", rows)
        assert result.get("status") != "unsupported_engine", tool
        assert _payload_rows(tool, result) == rows, tool
        assert result["count" if tool == "detect_regressions" else "row_count"] == len(rows), tool
        # The impl was really reached and really queried query_stats.
        assert fake.executed, tool
        assert "query_stats" in fake.executed[0][0], tool


def test_dynamodb_is_still_refused_so_the_flip_was_not_a_blanket_widening():
    """Only documentdb gained a producer. A family with no writer must still be
    refused, or the query tools go back to answering with a false empty state."""
    for tool, event in _QUERY_TOOLS.items():
        result, fake = _invoke(tool, event, "dynamodb", _rows_for(tool))
        assert result["status"] == "unsupported_engine", tool
        assert fake.executed == [], tool


# ---------------------------------------------------------------------------
# 2. Same columns, different meaning: the rows must be labelled.
# ---------------------------------------------------------------------------


def test_documentdb_rows_carry_a_data_source_label_and_sql_families_do_not():
    """Structural, not prose: which keys exist, for which family. Without the
    label, a censored profiler mean reads as the op's average response time."""
    expected_keys = {"origin", "query_text", "counters", "mean_time_ms", "coverage"}
    for tool, event in _QUERY_TOOLS.items():
        result, _ = _invoke(tool, event, "documentdb", _rows_for(tool))
        note = result["data_source"]
        assert expected_keys <= set(note), (tool, sorted(note))
    for fam in ("relational", "rds_instance"):
        for tool, event in _QUERY_TOOLS.items():
            result, _ = _invoke(tool, event, fam, _rows_for(tool))
            assert "data_source" not in result, (fam, tool)


def test_get_slow_queries_source_is_family_correct():
    """The relational `source` string claims these are NOT a slow-query log. For
    documentdb that is exactly backwards, so the handler replaces it. Pinned by
    inequality against the relational value, not by matching the new wording."""
    relational, _ = _invoke("get_slow_queries", _QUERY_TOOLS["get_slow_queries"],
                            "relational", _PROFILER_ROWS)
    docdb, _ = _invoke("get_slow_queries", _QUERY_TOOLS["get_slow_queries"],
                       "documentdb", _PROFILER_ROWS)
    assert docdb["source"] != relational["source"]
    assert docdb["source"] == handler._DOCDB_SLOW_QUERIES_SOURCE


def test_empty_documentdb_result_still_carries_the_coverage_caveat():
    """The profiler writes a row ONLY when an op crossed the threshold, so zero
    rows also covers profiler OFF, sampling, and a failed log read. The tool must
    not let an empty result be read as a measured "no slow ops"."""
    for tool, event in _QUERY_TOOLS.items():
        result, _ = _invoke(tool, event, "documentdb", [])
        assert _payload_rows(tool, result) == [], tool
        assert "coverage" in result["data_source"], tool


def test_only_detect_regressions_gets_the_join_caveat():
    """before/after are INNER JOINed on query_hash. For a SQL engine an absent
    before row means "the query did not exist"; for documentdb it means "it was
    not slow then", which is the regression being looked for. That row drops out,
    so an empty result is not evidence of no regression."""
    result, _ = _invoke("detect_regressions", _QUERY_TOOLS["detect_regressions"],
                        "documentdb", _REGRESSION_ROWS)
    assert "caveat" in result["data_source"]
    for tool in ("get_top_queries", "get_slow_queries"):
        other, _ = _invoke(tool, _QUERY_TOOLS[tool], "documentdb", _PROFILER_ROWS)
        assert "caveat" not in other["data_source"], tool


# ---------------------------------------------------------------------------
# 3. detect_regressions has no minimum-calls filter, so sample size must be visible.
# ---------------------------------------------------------------------------


def test_detect_regressions_reports_sample_size_for_every_family():
    """There is no MIN_CALLS here (that lives in the ETL query_regression
    findings collector), so min_change_pct can fire on a shape whose whole sample
    is a couple of profiler-logged ops. The cumulative call counters ride along so
    the agent can weigh change_pct instead of quoting it blind."""
    for fam in ("documentdb", "relational", "rds_instance"):
        result, fake = _invoke("detect_regressions", _QUERY_TOOLS["detect_regressions"],
                               fam, _REGRESSION_ROWS)
        sql = fake.executed[0][0]
        assert "MAX(calls) as before_calls" in sql, fam
        assert "MAX(calls) as after_calls" in sql, fam
        assert "b.before_calls, a.after_calls" in sql, fam
        row = result["regressions"][0]
        assert row["before_calls"] == 40 and row["after_calls"] == 52, fam
        assert "methodology" in result, fam


# ---------------------------------------------------------------------------
# 4. Nothing that needs a SQL surface became reachable.
# ---------------------------------------------------------------------------


def test_documentdb_capabilities_gained_query_stats_and_nothing_else():
    caps = handler.CAPABILITIES["documentdb"]
    assert caps["query_stats"] is True
    # The SQL surface: documentdb has none, and query_stats is not a stand-in.
    assert caps["sql"] is False
    assert "sql_via" not in caps
    assert caps["explain"] is False
    assert caps["index_advice"] is False
    assert caps["cluster_parameter"] is False
    assert caps["perf_insights"] is False
    assert caps["simulation"] is False


@patch("mcp_servers.operations.tools.execute_sql._lookup_cluster")
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_execute_sql_still_refuses_documentdb(mock_boto3, mock_lookup):
    """execute_sql gates on `sql`, not on query_stats. Flipping query_stats must
    not open the Data API path to a family that speaks the Mongo wire protocol."""
    from mcp_servers.operations.tools.execute_sql import execute_sql_impl

    mock_rds_data = MagicMock()
    mock_boto3.client.return_value = mock_rds_data
    mock_lookup.return_value = {
        "engine": "docdb",
        "engine_family": "documentdb",
        "cluster_arn": "arn:test",
        "secret_arn": "arn:secret",
    }
    result = execute_sql_impl(MagicMock(), cluster_id="docdb-prod-1", sql="SELECT 1")
    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == "documentdb"
    mock_rds_data.execute_statement.assert_not_called()


# ---------------------------------------------------------------------------
# 5. The TypeScript mirror. tests/unit/data_pipeline/test_engine_family.py keeps
#    the four Python copies byte-identical; the TS mirror is outside it.
# ---------------------------------------------------------------------------


def test_ts_mirror_has_no_query_stats_capability_to_drift():
    """frontend/src/lib/engine.ts mirrors engine_family(): the classification and
    the panel sets, never the capability flags. There is therefore no TS
    counterpart to this flip, and a partial capability map added later would make
    the frontend disagree with the backend silently."""
    src = (Path(__file__).resolve().parents[4] / "frontend/src/lib/engine.ts").read_text()
    assert "query_stats" not in src
    assert "queryStats" not in src
    # Classification still agrees with engine_family() for the flipped family.
    assert re.search(r'includes\("docdb"\).*return "documentdb"', src, re.S)
    assert handler._engine_family("docdb") == "documentdb"

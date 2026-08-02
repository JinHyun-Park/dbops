from unittest.mock import MagicMock

from mcp_servers.performance.tools.compare_periods import compare_periods_impl
from mcp_servers.shared.models import QueryResult


def test_compare_periods_calls_twice():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["avg_value", "max_value", "min_value", "sample_count"],
        rows=[{"avg_value": 3.5, "max_value": 8.0, "min_value": 0.5, "sample_count": 100}],
        row_count=1,
    )
    result = compare_periods_impl(
        mock_cache, "prod-pg-1",
        "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
        "2026-05-07T00:00:00Z", "2026-05-08T00:00:00Z",
    )
    assert mock_cache.execute.call_count == 2
    assert "period_a" in result
    assert "period_b" in result


def test_period_bounds_are_cast_to_timestamptz():
    """The SQL itself must cast, and this test exists because the assertions above
    cannot fail on a broken query.

    A MagicMock cache accepts any string, so `call_count == 2` stayed green while
    PostgreSQL rejected the statement outright: the RDS Data API sends every bound
    parameter as stringValue, and there is no `timestamp with time zone >= text`
    operator (SQLState 42883). This tool was dead for every engine family, in
    production, with a passing test. Assert on the SQL that was actually built.
    """
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["avg_value"], rows=[{"avg_value": 1.0}], row_count=1)
    compare_periods_impl(
        mock_cache, "prod-pg-1",
        "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
        "2026-05-07T00:00:00Z", "2026-05-08T00:00:00Z",
    )
    for call in mock_cache.execute.call_args_list:
        sql = call.args[0]
        assert ":start_time::timestamptz" in sql, f"start_time not cast: {sql}"
        assert ":end_time::timestamptz" in sql, f"end_time not cast: {sql}"

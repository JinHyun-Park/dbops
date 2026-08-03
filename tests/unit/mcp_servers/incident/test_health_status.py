import json
from unittest.mock import MagicMock

from mcp_servers.incident.tools.health_status import get_health_status_impl
from mcp_servers.shared.models import QueryResult


def test_health_status_available_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "instance_class"],
            rows=[{"cluster_id": "prod-pg-1", "status": "available", "engine": "aurora-postgresql", "instance_class": "db.r6g.xlarge"}],
            row_count=1,
        ),
        QueryResult(
            columns=["metric_type", "avg_val", "max_val"],
            rows=[
                {"metric_type": "cpu", "avg_val": 25.0, "max_val": 40.0},
                {"metric_type": "connections", "avg_val": 50.0, "max_val": 80.0},
            ],
            row_count=2,
        ),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["health"] == "healthy"
    assert result["cluster_id"] == "prod-pg-1"
    assert len(result["current_metrics"]) == 2
    assert mock_cache.execute.call_count == 2


def test_health_status_modifying_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status"],
            rows=[{"cluster_id": "prod-pg-1", "status": "modifying"}],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["health"] == "warning"


def test_health_status_unknown_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(columns=[], rows=[], row_count=0),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="unknown-cluster")
    # "unknown", not "critical". With no cluster_meta row there is no status word at
    # all, and an unregistered cluster is not the same thing as a cluster in
    # trouble: it may be perfectly healthy and simply not onboarded. The verdict
    # says so instead of guessing the worst.
    assert result["health"] == "unknown"
    assert "status" in result["reason"]
    assert result["telemetry"]["metrics_count"] == 0


# --- engine-aware extension tests ---

def test_health_status_dynamodb_engine_includes_resource_details():
    resource_details_payload = json.dumps({
        "billing_mode": "PAY_PER_REQUEST",
        "table_status": "ACTIVE",
        "gsi_count": 2,
    })
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "resource_details"],
            rows=[{
                "cluster_id": "my-ddb-table",
                "status": "available",
                "engine": "dynamodb",
                "resource_details": resource_details_payload,
            }],
            row_count=1,
        ),
        # Real samples: this test is about resource_details parsing, so it must not
        # trip the zero-telemetry rule and end up asserting that instead.
        QueryResult(
            columns=["metric_type", "avg_val", "max_val"],
            rows=[{"metric_type": "consumed_rcu", "avg_val": 1.0, "max_val": 3.0}],
            row_count=1,
        ),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="my-ddb-table")

    assert result["health"] == "healthy"
    assert result["engine"] == "dynamodb"
    assert isinstance(result["resource_details"], dict)
    assert result["resource_details"]["billing_mode"] == "PAY_PER_REQUEST"
    assert result["resource_details"]["gsi_count"] == 2


def test_health_status_documentdb_engine_includes_resource_details():
    resource_details_payload = json.dumps({
        "num_instances": 3,
        "engine_version": "5.0.0",
    })
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "resource_details"],
            rows=[{
                "cluster_id": "docdb-prod",
                "status": "available",
                "engine": "docdb",
                "resource_details": resource_details_payload,
            }],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="docdb-prod")

    assert result["engine"] == "docdb"
    assert result["resource_details"]["num_instances"] == 3


def test_health_status_relational_engine_unchanged_shape():
    """Aurora clusters must NOT get engine/resource_details injected."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "instance_class"],
            rows=[{
                "cluster_id": "aurora-pg",
                "status": "available",
                "engine": "aurora-postgresql",
                "instance_class": "db.r6g.xlarge",
            }],
            row_count=1,
        ),
        QueryResult(
            columns=["metric_type", "avg_val", "max_val"],
            rows=[{"metric_type": "cpu", "avg_val": 10.0, "max_val": 20.0}],
            row_count=1,
        ),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="aurora-pg")

    assert result["health"] == "healthy"
    assert "engine" not in result
    assert "resource_details" not in result


def test_health_status_resource_details_null_is_safe():
    """resource_details=None must not crash for non-relational engines."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "resource_details"],
            rows=[{
                "cluster_id": "ddb-no-details",
                "status": "available",
                "engine": "dynamodb",
                "resource_details": None,
            }],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="ddb-no-details")

    assert result["engine"] == "dynamodb"
    assert result.get("resource_details") is None


# ---------------------------------------------------------------------------
# The two live-measured failures, in opposite directions, from one cause
# ---------------------------------------------------------------------------


def _cache(status, metric_rows, engine="aurora-postgresql"):
    c = MagicMock()
    c.execute.side_effect = [
        QueryResult(columns=["cluster_id", "status", "engine"],
                    rows=[{"cluster_id": "c1", "status": status, "engine": engine}],
                    row_count=1),
        QueryResult(columns=["metric_type", "avg_val", "max_val"],
                    rows=metric_rows, row_count=len(metric_rows)),
    ]
    return c


_SAMPLE = [{"metric_type": "cpu", "avg_val": 5.0, "max_val": 9.0}]


def test_a_healthy_dynamodb_table_is_not_reported_critical():
    """DynamoDB's TableStatus word is ACTIVE, not "available".

    Measured 2026-08-02 against a real table: the old hardcoded lookup knew only the
    RDS vocabulary and fell through its `critical` catch-all, so a perfectly healthy
    table was reported CRITICAL. A false critical is worse than a missed one; it
    teaches the reader to ignore the field.
    """
    result = get_health_status_impl(_cache("ACTIVE", _SAMPLE, engine="dynamodb"),
                                   cluster_id="ddb-1")
    assert result["health"] == "healthy"
    assert result["telemetry"]["control_plane_status"] == "ACTIVE"


def test_no_telemetry_is_not_reported_healthy():
    """`status == "available"` stays true while collection is broken.

    That is the failure this tool exists to catch, and it used to be invisible: the
    verdict said "healthy" with `current_metrics: []` right beside it and nothing
    connecting the two.
    """
    result = get_health_status_impl(_cache("available", []), cluster_id="c1")
    assert result["health"] == "unknown"
    assert result["telemetry"]["metrics_count"] == 0
    assert "0개" in result["reason"]


def test_a_critical_status_is_not_downgraded_by_missing_telemetry():
    """Only the HEALTHY verdict is withheld for want of samples. A bad control-plane
    state is a real signal and must stand on its own, or a broken cluster whose
    collection also stopped would soften to "unknown"."""
    result = get_health_status_impl(_cache("storage-full", []), cluster_id="c1")
    assert result["health"] == "critical"


def test_an_unrecognised_status_word_is_unknown_not_critical():
    """Guessing "critical" from a word we do not know is the same mistake as
    guessing "healthy" from one. The unknown word is echoed so the fix is to add it
    to a set, not to re-derive the verdict."""
    result = get_health_status_impl(_cache("some-future-aws-state", _SAMPLE), cluster_id="c1")
    assert result["health"] == "unknown"
    assert "some-future-aws-state" in result["reason"]


def test_transitional_states_are_warning_across_vocabularies():
    for word in ("modifying", "backing-up", "snapshotting", "UPDATING"):
        result = get_health_status_impl(_cache(word, _SAMPLE), cluster_id="c1")
        assert result["health"] == "warning", word

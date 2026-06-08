from unittest.mock import MagicMock, patch

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.upgrade_impact import estimate_upgrade_impact_impl


def _cache_with(storage_gb, engine_version):
    cache = MagicMock()
    cache.execute.return_value = QueryResult(
        columns=["storage_size_gb", "engine_version"],
        rows=[{"storage_size_gb": storage_gb, "engine_version": engine_version}],
        row_count=1,
    )
    return cache


def _rds_with_readers(reader_count):
    """MagicMock rds whose describe returns 1 writer + `reader_count` readers."""
    members = [{"IsClusterWriter": True}]
    members += [{"IsClusterWriter": False} for _ in range(reader_count)]
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterMembers": members}]}
    return rds


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_major_upgrade_recommends_blue_green(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(2)
    cache = _cache_with("800", "15.4")

    result = estimate_upgrade_impact_impl(cache, cluster_id="prod-pg-1", target_version="16.2")

    assert result["upgrade_type"] == "major"
    assert result["recommendation"] == "blue_green"
    assert result["readers"] == 2
    assert "메이저" in result["recommendation_reason"]
    assert len(result["methods"]) == 3


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_minor_small_cluster_recommends_in_place(mock_rds_for):
    # describe failure => degrade to 0 readers; small storage => in_place.
    mock_rds_for.side_effect = RuntimeError("cluster not reachable")
    cache = _cache_with("50", "15.4")

    result = estimate_upgrade_impact_impl(cache, cluster_id="dev-pg-1", target_version="15.5")

    assert result["upgrade_type"] == "minor"
    assert result["readers"] == 0
    assert result["recommendation"] == "in_place"
    assert "unavailable" in result["recommendation_reason"]


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_minor_large_storage_recommends_blue_green(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("800", "15.4")

    result = estimate_upgrade_impact_impl(cache, cluster_id="prod-pg-1", target_version="15.5")

    assert result["upgrade_type"] == "minor"
    assert result["readers"] == 0
    assert result["recommendation"] == "blue_green"


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_minor_many_readers_recommends_blue_green(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(2)
    cache = _cache_with("50", "15.4")

    result = estimate_upgrade_impact_impl(cache, cluster_id="prod-pg-1", target_version="15.5")

    assert result["upgrade_type"] == "minor"
    assert result["readers"] == 2
    assert result["recommendation"] == "blue_green"


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_reader_count_extends_time_estimate(mock_rds_for):
    cache = _cache_with("200", "15.4")

    mock_rds_for.return_value = _rds_with_readers(0)
    zero = estimate_upgrade_impact_impl(cache, cluster_id="prod-pg-1", target_version="15.5")

    mock_rds_for.return_value = _rds_with_readers(3)
    three = estimate_upgrade_impact_impl(cache, cluster_id="prod-pg-1", target_version="15.5")

    zero_bg = next(m for m in zero["methods"] if m["method"] == "blue_green")
    three_bg = next(m for m in three["methods"] if m["method"] == "blue_green")
    assert three_bg["estimated_minutes"] > zero_bg["estimated_minutes"]


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_mysql_aurora_major_change(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("100", "8.0.mysql_aurora.3.04.0")

    result = estimate_upgrade_impact_impl(
        cache, cluster_id="prod-mysql-1", target_version="8.0.mysql_aurora.3.06.0"
    )

    # Same aurora major family (3) => minor.
    assert result["upgrade_type"] == "minor"


@patch("mcp_servers.simulation.tools.upgrade_impact.rds_client_for_cluster")
def test_unparseable_current_version_is_major(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("100", "unknown")

    result = estimate_upgrade_impact_impl(cache, cluster_id="x", target_version="15.5")

    assert result["upgrade_type"] == "major"
    assert result["recommendation"] == "blue_green"

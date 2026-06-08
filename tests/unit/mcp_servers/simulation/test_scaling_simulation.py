from unittest.mock import MagicMock, patch

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.scaling_simulation import (
    ACU_PRICE_PER_HOUR,
    HOURS_PER_MONTH,
    simulate_scaling_impl,
)

MODULE = "mcp_servers.simulation.tools.scaling_simulation"


def _serverless_cluster(min_capacity=2.0, max_capacity=16.0, readers=1):
    """A realistic describe_db_clusters DBCluster: Serverless v2 with a writer
    plus `readers` reader instances."""
    members = [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}]
    for i in range(readers):
        members.append({"IsClusterWriter": False, "DBInstanceIdentifier": f"reader-{i}"})
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "ServerlessV2ScalingConfiguration": {
            "MinCapacity": min_capacity,
            "MaxCapacity": max_capacity,
        },
        "DBClusterMembers": members,
    }


def _empty_cache():
    """A cache whose observed-load query returns no rows."""
    cache = MagicMock()
    cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    return cache


def test_current_reflects_live_config_not_hardcoded():
    """current must mirror the LIVE 2/16 range and member counts, NOT the old
    fabricated 0.5/4.0 figures."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster()]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    assert result["current"]["min_acu"] == 2.0
    assert result["current"]["max_acu"] == 16.0
    assert result["current"]["min_acu"] != 0.5
    assert result["current"]["max_acu"] != 4.0
    assert result["current"]["writers"] == 1
    assert result["current"]["readers"] == 1
    assert result["data_source"] == "live (describe_db_clusters)"
    rds.describe_db_clusters.assert_called_once_with(DBClusterIdentifier="prod-pg-1")


def test_proposed_defaults_to_current_when_omitted():
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster()]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    # No new range supplied -> proposed == current -> 0% change.
    assert result["proposed"]["min_acu"] == 2.0
    assert result["proposed"]["max_acu"] == 16.0
    assert result["cost_impact"]["change_pct"] == 0.0


def test_cost_scales_with_members_and_real_range():
    """Cost must use the real range AND multiply by the writer+reader count."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=1)]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_scaling_impl(
            cache, cluster_id="prod-pg-1", new_min_acu=4.0, new_max_acu=32.0
        )

    members = 2  # 1 writer + 1 reader
    expected_current = ((2.0 + 16.0) / 2) * ACU_PRICE_PER_HOUR * HOURS_PER_MONTH * members
    expected_proposed = ((4.0 + 32.0) / 2) * ACU_PRICE_PER_HOUR * HOURS_PER_MONTH * members

    assert result["cost_impact"]["current_monthly_estimate"] == f"${expected_current:,.2f}"
    assert result["cost_impact"]["proposed_monthly_estimate"] == f"${expected_proposed:,.2f}"
    # Doubling the midpoint -> +100%.
    assert result["cost_impact"]["change_pct"] == 100.0


def test_cost_doubles_with_extra_reader():
    """Two readers vs one (3 vs 2 members) must raise the current estimate."""
    cache = _empty_cache()

    rds_one = MagicMock()
    rds_one.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=1)]}
    rds_two = MagicMock()
    rds_two.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=2)]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds_one):
        one = simulate_scaling_impl(cache, cluster_id="prod-pg-1")
    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds_two):
        two = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    midpoint = (2.0 + 16.0) / 2
    base = midpoint * ACU_PRICE_PER_HOUR * HOURS_PER_MONTH
    assert one["cost_impact"]["current_monthly_estimate"] == f"${base * 2:,.2f}"
    assert two["cost_impact"]["current_monthly_estimate"] == f"${base * 3:,.2f}"


def test_non_serverless_cluster_gives_not_applicable_note():
    """Provisioned cluster (no ServerlessV2ScalingConfiguration) -> clear
    not-applicable note + instance classes, no cost math."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DBClusterIdentifier": "prod-pg-1",
                "DBClusterMembers": [
                    {"IsClusterWriter": True, "DBInstanceClass": "db.r6g.large"},
                    {"IsClusterWriter": False, "DBInstanceClass": "db.r6g.large"},
                ],
            }
        ]
    }

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    assert result["current"]["min_acu"] is None
    assert result["current"]["max_acu"] is None
    assert "Serverless v2" in result["note"]
    assert "db.r6g.large" in result["note"]
    assert result["instance_classes"] == ["db.r6g.large", "db.r6g.large"]
    assert result["cost_impact"]["current_monthly_estimate"] is None


def test_describe_failure_degrades_gracefully_without_fake_numbers():
    """If describe raises, fall back to a clear estimate note and NEVER emit
    the old fabricated 2-ACU / 0.5-4.0 figures."""
    cache = MagicMock()
    cache.execute.return_value = QueryResult(
        columns=["engine", "engine_version"],
        rows=[{"engine": "aurora-postgresql", "engine_version": "15.4"}],
        row_count=1,
    )
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = RuntimeError("AccessDenied assuming spoke role")

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_scaling_impl(cache, cluster_id="unregistered-1")

    assert result["data_source"] == "estimate (live describe unavailable)"
    assert result["current"]["min_acu"] is None
    assert result["current"]["max_acu"] is None
    assert result["cost_impact"]["current_monthly_estimate"] is None
    assert result["cost_impact"]["change_pct"] is None
    # No fabricated baseline anywhere in the payload.
    assert 2.0 not in (result["current"]["min_acu"], result["current"]["max_acu"])
    assert "$" not in (result["cost_impact"]["current_monthly_estimate"] or "")
    # Cache context should still surface engine info.
    assert "aurora-postgresql" in result["note"]


def test_observed_load_enrichment_when_available():
    """observed_load is attached when the metric cache returns 1h averages."""
    cache = MagicMock()
    cache.execute.return_value = QueryResult(
        columns=["avg_aas", "avg_cpu"],
        rows=[{"avg_aas": 3.4, "avg_cpu": 62.5}],
        row_count=1,
    )
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster()]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    assert result["observed_load"] == {"avg_aas_1h": 3.4, "avg_cpu_pct_1h": 62.5}

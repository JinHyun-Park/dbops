from unittest.mock import MagicMock, patch

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.scaling_simulation import (
    HOURS_PER_MONTH,
    simulate_scaling_impl,
)

MODULE = "mcp_servers.simulation.tools.scaling_simulation"


def _serverless_cluster(min_capacity=2.0, max_capacity=16.0, readers=1, io_optimized=False):
    """A realistic describe_db_clusters DBCluster: Serverless v2 with a writer
    plus `readers` reader instances."""
    members = [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}]
    for i in range(readers):
        members.append({"IsClusterWriter": False, "DBInstanceIdentifier": f"reader-{i}"})
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "StorageType": "aurora-iopt1" if io_optimized else "aurora",
        "ServerlessV2ScalingConfiguration": {
            "MinCapacity": min_capacity,
            "MaxCapacity": max_capacity,
        },
        "DBClusterMembers": members,
    }


def _provisioned_cluster(writer_class="db.r6g.large", reader_class="db.r6g.large", readers=1, io_optimized=False):
    """A provisioned (non-Serverless-v2) DBCluster: writer + `readers` readers,
    no ServerlessV2ScalingConfiguration."""
    members = [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}]
    for i in range(readers):
        members.append({"IsClusterWriter": False, "DBInstanceIdentifier": f"reader-{i}"})
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "StorageType": "aurora-iopt1" if io_optimized else "aurora",
        "DBClusterMembers": members,
    }


def _describe_instances(class_by_id):
    """Build a describe_db_instances response mapping identifier -> class."""
    return {
        "DBInstances": [
            {"DBInstanceIdentifier": ident, "DBInstanceClass": klass}
            for ident, klass in class_by_id.items()
        ]
    }


def _empty_cache():
    """A cache that returns no rows; the live path is authoritative so the cache
    isn't actually queried, but the dispatcher always passes one."""
    cache = MagicMock()
    cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    return cache


def _cw(averages=None):
    """A CloudWatch client mock whose get_metric_statistics returns the given
    per-hour Average ACU datapoints (empty → forces the midpoint fallback)."""
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Average": a} for a in (averages or [])]
    }
    return cw


def test_serverless_uses_real_acu_price_and_member_count():
    """Serverless v2: mode serverless, current 2/16, cost uses the REAL 0.26
    rate (NOT the old 0.12) and multiplies by member_count (writer + reader)."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=1)]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_acu_hour", return_value=0.26) as acu_price, patch(
        f"{MODULE}.price_per_instance_hour"
    ), patch(f"{MODULE}.client_for_cluster", return_value=_cw()):  # no observed ACU → midpoint
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    member_count = 2  # 1 writer + 1 reader
    expected = round(((2.0 + 16.0) / 2) * 0.26 * HOURS_PER_MONTH * member_count, 2)

    assert result["mode"] == "serverless"
    assert result["current"] == {"min_acu": 2.0, "max_acu": 16.0}
    assert result["proposed"] == {"min_acu": 2.0, "max_acu": 16.0}
    assert result["writers"] == 1
    assert result["readers"] == 1
    assert result["cost_impact"]["current_monthly_usd"] == expected
    # The real rate was used, NOT the old hardcoded 0.12.
    wrong = round(((2.0 + 16.0) / 2) * 0.12 * HOURS_PER_MONTH * member_count, 2)
    assert result["cost_impact"]["current_monthly_usd"] != wrong
    assert result["unit_pricing"]["kind"] == "acu"
    assert result["unit_pricing"]["price_per_hour"] == 0.26
    assert result["unit_pricing"]["region"] == "ap-northeast-2"
    assert result["unit_pricing"]["source"] == "aws_pricing_api"
    assert result["data_source"] == "live (describe_db_clusters)"
    acu_price.assert_called_once_with("ap-northeast-2", "aurora-postgresql", False)
    rds.describe_db_clusters.assert_called_once_with(DBClusterIdentifier="prod-pg-1")


def test_serverless_uses_observed_acu_not_midpoint():
    """When CloudWatch has observed ACU, cost uses the OBSERVED average (clamped
    into the range), not the min/max midpoint — a mostly-idle cluster costs far
    less than (min+max)/2 implies."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=1)]}

    # Observed ~3 ACU on a 2..16 range — midpoint would be 9, far higher.
    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_acu_hour", return_value=0.26), patch(
        f"{MODULE}.price_per_instance_hour"
    ), patch(f"{MODULE}.client_for_cluster", return_value=_cw([2.5, 3.0, 3.5])):
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    member_count = 2
    observed = 3.0  # avg of 2.5/3.0/3.5
    expected = round(observed * 0.26 * HOURS_PER_MONTH * member_count, 2)
    midpoint_cost = round(((2.0 + 16.0) / 2) * 0.26 * HOURS_PER_MONTH * member_count, 2)

    assert result["acu_basis"] == "observed"
    assert result["observed_avg_acu"] == 3.0
    assert result["confidence"] == "high"
    assert result["cost_impact"]["current_monthly_usd"] == expected
    assert result["cost_impact"]["current_monthly_usd"] != midpoint_cost


def test_serverless_proposed_overrides_drive_cost_and_pct():
    """Proposed range overrides change the cost and produce a sane change_pct."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=1)]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_acu_hour", return_value=0.26), patch(
        f"{MODULE}.price_per_instance_hour"
    ), patch(f"{MODULE}.client_for_cluster", return_value=_cw()):  # no observed ACU → midpoint
        result = simulate_scaling_impl(
            cache, cluster_id="prod-pg-1", new_min_acu=4.0, new_max_acu=32.0
        )

    member_count = 2
    current = round(((2.0 + 16.0) / 2) * 0.26 * HOURS_PER_MONTH * member_count, 2)
    proposed = round(((4.0 + 32.0) / 2) * 0.26 * HOURS_PER_MONTH * member_count, 2)
    assert result["cost_impact"]["current_monthly_usd"] == current
    assert result["cost_impact"]["proposed_monthly_usd"] == proposed
    # Doubling the midpoint -> +100%.
    assert result["cost_impact"]["change_pct"] == 100.0
    assert result["cost_impact"]["delta_monthly_usd"] == round(proposed - current, 2)


def test_provisioned_resize_costs_more():
    """Provisioned: no ServerlessV2 config; describe_db_instances gives the
    writer class; a larger new_instance_class costs MORE than current."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_provisioned_cluster(readers=1)]}
    rds.describe_db_instances.return_value = _describe_instances(
        {"writer-1": "db.r6g.large", "reader-0": "db.r6g.large"}
    )

    def _instance_price(region, engine, instance_class, io_opt):
        return {"db.r6g.large": 0.313, "db.r6g.xlarge": 0.626}.get(instance_class)

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_acu_hour"), patch(
        f"{MODULE}.price_per_instance_hour", side_effect=_instance_price
    ):
        result = simulate_scaling_impl(
            cache, cluster_id="prod-pg-1", new_instance_class="db.r6g.xlarge"
        )

    member_count = 2
    expected_current = round(2 * 0.313 * HOURS_PER_MONTH, 2)
    expected_proposed = round(member_count * 0.626 * HOURS_PER_MONTH, 2)

    assert result["mode"] == "provisioned"
    assert result["current"]["instance_class"] == "db.r6g.large"
    assert result["proposed"]["instance_class"] == "db.r6g.xlarge"
    assert result["writers"] == 1
    assert result["readers"] == 1
    assert result["cost_impact"]["current_monthly_usd"] == expected_current
    assert result["cost_impact"]["proposed_monthly_usd"] == expected_proposed
    assert result["cost_impact"]["proposed_monthly_usd"] > result["cost_impact"]["current_monthly_usd"]
    assert result["unit_pricing"]["kind"] == "instance"
    assert result["unit_pricing"]["price_per_hour"] == 0.626
    assert result["unit_pricing"]["source"] == "aws_pricing_api"


def test_pricing_unavailable_yields_none_cost_and_fallback_source():
    """When a needed price is None, cost is None, data_source is an estimate and
    unit_pricing.source is fallback — never a crash, never a fabricated number."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [_serverless_cluster(readers=1)]}

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_acu_hour", return_value=None), patch(
        f"{MODULE}.price_per_instance_hour", return_value=None
    ):
        result = simulate_scaling_impl(cache, cluster_id="prod-pg-1")

    assert result["mode"] == "serverless"
    assert result["cost_impact"]["current_monthly_usd"] is None
    assert result["cost_impact"]["proposed_monthly_usd"] is None
    assert result["cost_impact"]["delta_monthly_usd"] is None
    assert result["cost_impact"]["change_pct"] is None
    assert result["unit_pricing"]["price_per_hour"] is None
    assert result["unit_pricing"]["source"] == "fallback"
    assert "estimate" in result["data_source"]


def test_describe_failure_degrades_gracefully_without_raise():
    """If describe raises, return a graceful estimate dict (costs None) and
    NEVER raise."""
    cache = _empty_cache()
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = RuntimeError("AccessDenied assuming spoke role")

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_acu_hour"), patch(f"{MODULE}.price_per_instance_hour"):
        result = simulate_scaling_impl(cache, cluster_id="unregistered-1")

    assert result["data_source"] == "estimate (live describe unavailable)"
    assert result["cost_impact"]["current_monthly_usd"] is None
    assert result["cost_impact"]["proposed_monthly_usd"] is None
    assert result["cost_impact"]["change_pct"] is None
    assert result["unit_pricing"]["source"] == "fallback"
    assert result["unit_pricing"]["region"] == "ap-northeast-2"

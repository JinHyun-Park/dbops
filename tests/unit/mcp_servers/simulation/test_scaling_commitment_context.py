"""Unit tests for the RI-aware `commitment_context` annotation on the scaling
simulation (output-only addition).

Pins: covered vs uncovered instance classes, the "resize strands your RI"
Korean note, the on-demand-only scale-out-vs-scale-up comparison (no fabricated
RI discount rate), the instance-size ladder, serverless handling, and the
promise that any failure degrades to {"available": False} without touching the
existing cost result.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from mcp_servers.simulation.tools.scaling_simulation import (
    HOURS_PER_MONTH,
    _next_class_up,
    simulate_scaling_impl,
)

MODULE = "mcp_servers.simulation.tools.scaling_simulation"
NOW = datetime.now(timezone.utc)


def _ri(cls, count, remaining_days=200, state="active"):
    start = NOW - timedelta(days=365 - remaining_days)
    return {
        "DBInstanceClass": cls,
        "DBInstanceCount": count,
        "State": state,
        "StartTime": start,
        "Duration": 365 * 86400,
    }


def _provisioned_cluster(readers=1):
    members = [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}]
    for i in range(readers):
        members.append({"IsClusterWriter": False, "DBInstanceIdentifier": f"reader-{i}"})
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "StorageType": "aurora",
        "DBClusterMembers": members,
    }


def _serverless_cluster():
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "StorageType": "aurora",
        "ServerlessV2ScalingConfiguration": {"MinCapacity": 2.0, "MaxCapacity": 16.0},
        "DBClusterMembers": [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}],
    }


def _rds(cluster, ris, instance_classes):
    """rds mock: describe_db_clusters, describe_reserved_db_instances, and
    describe_db_instances (member class map)."""
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [cluster]}
    rds.describe_reserved_db_instances.return_value = {"ReservedDBInstances": ris}
    rds.describe_db_instances.return_value = {
        "DBInstances": [
            {"DBInstanceIdentifier": ident, "DBInstanceClass": klass}
            for ident, klass in instance_classes.items()
        ]
    }
    return rds


_PRICES = {"db.r6g.large": 0.29, "db.r6g.xlarge": 0.58}


def _price(region, engine, cls, io):
    return _PRICES.get(cls)


def _run(rds):
    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_instance_hour", side_effect=_price), patch(
        f"{MODULE}.price_per_acu_hour", return_value=0.26
    ), patch(f"{MODULE}.client_for_cluster", return_value=MagicMock(
        **{"get_metric_statistics.return_value": {"Datapoints": []}}
    )):
        return simulate_scaling_impl(MagicMock(), cluster_id="prod-pg-1", **({}))


def _run_resize(rds, new_class):
    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ), patch(f"{MODULE}.price_per_instance_hour", side_effect=_price):
        return simulate_scaling_impl(MagicMock(), cluster_id="prod-pg-1", new_instance_class=new_class)


# ---------------------------------------------------------------------------
# ladder self-check
# ---------------------------------------------------------------------------


def test_next_class_up():
    assert _next_class_up("db.r6g.large") == "db.r6g.xlarge"
    assert _next_class_up("db.r6g.xlarge") == "db.r6g.2xlarge"
    assert _next_class_up("db.r6g.48xlarge") is None  # top of ladder
    assert _next_class_up("weird") is None
    assert _next_class_up(None) is None


# ---------------------------------------------------------------------------
# covered / uncovered / stranded-RI note
# ---------------------------------------------------------------------------


def test_current_class_ri_covered_no_resize_no_note():
    rds = _rds(
        _provisioned_cluster(readers=1),
        ris=[_ri("db.r6g.large", 2)],
        instance_classes={"writer-1": "db.r6g.large", "reader-0": "db.r6g.large"},
    )
    ctx = _run(rds)["commitment_context"]
    assert ctx["available"] is True
    assert ctx["current_class"]["ri_match"] is True
    assert ctx["current_class"]["ri_count"] == 2
    # No resize → proposed == current → still covered → no stranding note.
    assert ctx["proposed_class"]["ri_match"] is True
    assert ctx["note"] is None


def test_resize_off_ri_emits_stranded_note_and_uncovered_proposed():
    rds = _rds(
        _provisioned_cluster(readers=1),
        ris=[_ri("db.r6g.large", 2, remaining_days=45)],
        instance_classes={"writer-1": "db.r6g.large", "reader-0": "db.r6g.large"},
    )
    ctx = _run_resize(rds, "db.r6g.xlarge")["commitment_context"]
    assert ctx["current_class"]["ri_match"] is True
    assert ctx["proposed_class"]["instance_class"] == "db.r6g.xlarge"
    assert ctx["proposed_class"]["ri_match"] is False
    assert ctx["note"] and "db.r6g.xlarge" in ctx["note"]
    assert "온디맨드" in ctx["note"]
    assert "만료" in ctx["note"]  # expiry date is surfaced


def test_autoscale_vs_fixed_on_demand_only_no_fabricated_rate():
    rds = _rds(
        _provisioned_cluster(readers=1),
        ris=[_ri("db.r6g.large", 2)],
        instance_classes={"writer-1": "db.r6g.large", "reader-0": "db.r6g.large"},
    )
    avf = _run(rds)["commitment_context"]["autoscale_vs_fixed"]
    assert avf is not None
    # +1 reader at the current class, on-demand.
    assert avf["add_reader"]["instance_class"] == "db.r6g.large"
    assert avf["add_reader"]["monthly_on_demand_usd"] == round(0.29 * HOURS_PER_MONTH, 2)
    assert avf["add_reader"]["ri_covered"] is True  # current class IS RI-covered
    # upsize one class up: delta on on-demand prices.
    assert avf["upsize_writer"]["instance_class"] == "db.r6g.xlarge"
    assert avf["upsize_writer"]["delta_monthly_on_demand_usd"] == round((0.58 - 0.29) * HOURS_PER_MONTH, 2)
    assert avf["upsize_writer"]["ri_covered"] is False
    assert "온디맨드" in avf["note"]


# ---------------------------------------------------------------------------
# serverless + failure paths
# ---------------------------------------------------------------------------


def test_serverless_context_notes_ri_not_applicable():
    rds = _rds(_serverless_cluster(), ris=[_ri("db.r6g.large", 3)], instance_classes={})
    ctx = _run(rds)["commitment_context"]
    assert ctx["available"] is True
    assert ctx["current_class"] is None
    assert ctx["autoscale_vs_fixed"] is None
    assert "Serverless v2" in ctx["note"]


def test_describe_cluster_failure_yields_available_false():
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = RuntimeError("DBClusterNotFoundFault")
    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds), patch(
        f"{MODULE}.lookup_cluster", return_value={"region": "ap-northeast-2"}
    ):
        result = simulate_scaling_impl(MagicMock(), cluster_id="ghost", new_instance_class="db.r6g.large")
    assert result["data_source"].startswith("estimate")
    assert result["commitment_context"] == {"available": False}


def test_ri_describe_failure_keeps_result_intact():
    """describe_reserved_db_instances failing must NOT break the cost result —
    RIs degrade to empty and current/proposed simply report no match."""
    rds = _rds(
        _provisioned_cluster(readers=0),
        ris=[],
        instance_classes={"writer-1": "db.r6g.large"},
    )
    rds.describe_reserved_db_instances.side_effect = RuntimeError("AccessDenied")
    result = _run(rds)
    assert result["cost_impact"]["current_monthly_usd"] is not None  # cost sim untouched
    assert result["commitment_context"]["available"] is True
    assert result["commitment_context"]["current_class"]["ri_match"] is False

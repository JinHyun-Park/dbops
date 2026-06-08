from unittest.mock import MagicMock, patch

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.upgrade_plan import generate_upgrade_plan_impl


def _cache_with(storage_gb, engine_version, engine="aurora-postgresql"):
    cache = MagicMock()
    cache.execute.return_value = QueryResult(
        columns=["cluster_id", "engine", "engine_version", "storage_size_gb"],
        rows=[
            {
                "cluster_id": "prod-pg-1",
                "engine": engine,
                "engine_version": engine_version,
                "storage_size_gb": storage_gb,
            }
        ],
        row_count=1,
    )
    return cache


def _rds_with_readers(reader_count):
    members = [{"IsClusterWriter": True}]
    members += [{"IsClusterWriter": False} for _ in range(reader_count)]
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterMembers": members}]}
    return rds


def _actions(result):
    return [s["action"] for s in result["steps"]]


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_major_upgrade_adds_compatibility_steps(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("800", "15.4")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="16.2", method="blue_green"
    )

    actions = _actions(result)
    assert result["upgrade_type"] == "major"
    assert "파라미터 그룹 패밀리 마이그레이션" in actions
    assert "확장(extension)/비호환 기능 호환성 점검" in actions
    assert "pg_upgrade 사전 점검" in actions  # PG major
    # step numbers stay contiguous after dynamic insertion
    assert [s["step"] for s in result["steps"]] == list(range(1, len(result["steps"]) + 1))


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_minor_upgrade_omits_major_steps(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("50", "15.4")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="in_place"
    )

    actions = _actions(result)
    assert result["upgrade_type"] == "minor"
    assert "파라미터 그룹 패밀리 마이그레이션" not in actions
    assert "pg_upgrade 사전 점검" not in actions


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_readers_add_per_reader_step(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(2)
    cache = _cache_with("200", "15.4")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="blue_green"
    )

    assert result["readers"] == 2
    assert any("리더" in s["action"] for s in result["steps"])


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_describe_failure_degrades_to_zero_readers(mock_rds_for):
    mock_rds_for.side_effect = RuntimeError("not reachable")
    cache = _cache_with("100", "15.4")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="in_place"
    )

    assert result["readers"] == 0
    assert "reader_note" in result
    assert not any("리더" in s["action"] for s in result["steps"])


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_time_estimate_is_not_len_steps_times_five(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(2)
    cache = _cache_with("800", "16.2")  # major + large + readers

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="17.1", method="blue_green"
    )

    assert result["estimated_total_minutes"] != len(result["steps"]) * 5


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_time_estimate_varies_with_storage(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)

    small = generate_upgrade_plan_impl(
        _cache_with("50", "15.4"), cluster_id="prod-pg-1", target_version="15.5", method="in_place"
    )
    large = generate_upgrade_plan_impl(
        _cache_with("2000", "15.4"), cluster_id="prod-pg-1", target_version="15.5", method="in_place"
    )

    # identical step list, different storage => different estimate
    assert len(small["steps"]) == len(large["steps"])
    assert large["estimated_total_minutes"] > small["estimated_total_minutes"]


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_time_estimate_varies_with_readers(mock_rds_for):
    cache = _cache_with("200", "15.4")

    mock_rds_for.return_value = _rds_with_readers(0)
    zero = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="blue_green"
    )

    mock_rds_for.return_value = _rds_with_readers(4)
    four = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="blue_green"
    )

    assert four["estimated_total_minutes"] > zero["estimated_total_minutes"]


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_keeps_rollback_plan_and_method(mock_rds_for):
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("100", "15.4")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="blue_green"
    )
    assert result["method"] == "blue_green"
    assert "Blue" in result["rollback_plan"]
    assert result["current_version"] == "15.4"


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_mysql_major_does_not_add_pg_upgrade_step(mock_rds_for):
    """Engine comes from cluster_meta.engine — a MySQL major must NOT get a
    pg_upgrade step (the old version-text heuristic would have)."""
    mock_rds_for.return_value = _rds_with_readers(0)
    cache = _cache_with("200", "8.0.mysql_aurora.2.11.0", engine="aurora-mysql")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-mysql-1",
        target_version="8.0.mysql_aurora.3.06.0", method="blue_green",
    )
    actions = _actions(result)
    assert result["upgrade_type"] == "major"
    assert "확장(extension)/비호환 기능 호환성 점검" in actions  # generic major step
    assert "pg_upgrade 사전 점검" not in actions  # PG-only step must be absent


@patch("mcp_servers.simulation.tools.upgrade_plan.rds_client_for_cluster")
def test_clone_method_has_clone_steps_and_rollback(mock_rds_for):
    """method='clone' must produce clone-specific execution steps AND the clone
    rollback plan — previously it got in-place steps with a clone rollback."""
    mock_rds_for.return_value = _rds_with_readers(1)
    cache = _cache_with("100", "15.4")

    result = generate_upgrade_plan_impl(
        cache, cluster_id="prod-pg-1", target_version="15.5", method="clone"
    )
    actions = _actions(result)
    assert any("클론" in a for a in actions)  # clone-specific steps present
    assert "In-place 업그레이드" not in actions  # not the in-place path
    assert "DNS" in result["rollback_plan"]  # clone rollback

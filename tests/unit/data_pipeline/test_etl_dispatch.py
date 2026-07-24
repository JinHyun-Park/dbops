"""ETL dispatcher unit tests — engine_family routing.

Verifies that _collect_one dispatches to the correct collector based on engine
family and that no RDS/PI/cost calls leak into non-relational paths.

Uses the importlib-from-path loader (same pattern as test_param_fitness.py).
Collector functions are monkeypatched on the loaded handler module so we test
the actual dispatch logic without any AWS calls.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load_handler():
    """Load handler.py as a module, ensuring its collectors imports resolve."""
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location("etl_handler", _ROOT / "handler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get_client(*args, **kwargs):
    """Returns a fresh MagicMock regardless of service/region."""
    return MagicMock()


def _noop_cache_execute(sql, params):
    pass


_COMMON_KWARGS = dict(
    get_client=_fake_get_client,
    cache_rds_data=MagicMock(),
    cache_execute=_noop_cache_execute,
    cache_cluster_arn="arn:aws:rds:ap-northeast-2:123:cluster:cache",
    cache_secret_arn="arn:aws:secretsmanager:ap-northeast-2:123:secret:cache",
    cache_db_name="dbops",
    run_ts="2026-06-11T00:00:00+00:00",
)


# ---------------------------------------------------------------------------
# Test 1: DynamoDB routes ONLY to collect_dynamodb_metrics
# ---------------------------------------------------------------------------

def test_dynamodb_routes_to_ddb_collector_only():
    handler = _load_handler()

    resource = {
        "cluster_id": "ddb-abc",
        "engine": "dynamodb",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
        "resource_name": "Orders",
    }

    mock_ddb_collector = MagicMock(return_value={"metrics_inserted": 5})
    mock_meta = MagicMock()
    mock_pi = MagicMock()
    mock_cw = MagicMock()
    mock_cost = MagicMock()

    with (
        patch.object(handler, "collect_dynamodb_metrics", mock_ddb_collector),
        patch.object(handler, "collect_cluster_meta", mock_meta),
        patch.object(handler, "collect_pi_metrics", mock_pi),
        patch.object(handler, "collect_cw_metrics", mock_cw),
        patch.object(handler, "collect_cost_findings", mock_cost),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    # DynamoDB collector must be called exactly once
    mock_ddb_collector.assert_called_once()
    # RDS/PI/CW/cost must NOT be called
    mock_meta.assert_not_called()
    mock_pi.assert_not_called()
    mock_cw.assert_not_called()
    mock_cost.assert_not_called()
    # Result carries the cluster_id
    assert result["cluster_id"] == "ddb-abc"


# ---------------------------------------------------------------------------
# Test 2: DocumentDB routes ONLY to collect_docdb_metrics
# ---------------------------------------------------------------------------

def test_documentdb_routes_to_docdb_collector_only():
    handler = _load_handler()

    resource = {
        "cluster_id": "docdb-cluster-1",
        "engine": "docdb",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }

    mock_docdb_collector = MagicMock(return_value={"metrics_inserted": 3})
    mock_meta = MagicMock()
    mock_pi = MagicMock()
    mock_cw = MagicMock()
    mock_cost = MagicMock()

    with (
        patch.object(handler, "collect_docdb_metrics", mock_docdb_collector),
        patch.object(handler, "collect_cluster_meta", mock_meta),
        patch.object(handler, "collect_pi_metrics", mock_pi),
        patch.object(handler, "collect_cw_metrics", mock_cw),
        patch.object(handler, "collect_cost_findings", mock_cost),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    # DocDB collector must be called exactly once
    mock_docdb_collector.assert_called_once()
    # RDS/PI/CW/cost must NOT be called
    mock_meta.assert_not_called()
    mock_pi.assert_not_called()
    mock_cw.assert_not_called()
    mock_cost.assert_not_called()
    assert result["cluster_id"] == "docdb-cluster-1"


# ---------------------------------------------------------------------------
# Test 3: Relational (aurora-postgresql) calls meta AND cost (existing path)
# ---------------------------------------------------------------------------

def test_relational_runs_meta_and_cost():
    handler = _load_handler()

    resource = {
        "cluster_id": "prod-pg-1",
        "engine": "aurora-postgresql",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
        "cluster_arn": "arn:aws:rds:ap-northeast-2:123:cluster:prod-pg-1",
        "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:123:secret:prod-pg-1",
        "db_name": "sampledb",
    }

    mock_meta = MagicMock(return_value={"status": "available"})
    mock_cost = MagicMock(return_value={"findings": []})
    # Mock all the heavy collectors so no real AWS calls occur
    mock_pi = MagicMock(return_value={"rows": 0})
    mock_cw = MagicMock(return_value={"rows": 0})
    mock_instances = MagicMock(return_value={"DBInstances": []})
    mock_baselines = MagicMock(return_value={"trained": 0})
    mock_ddb_collector = MagicMock()
    mock_docdb_collector = MagicMock()

    # Patch all collectors that touch AWS
    with (
        patch.object(handler, "collect_cluster_meta", mock_meta),
        patch.object(handler, "collect_cost_findings", mock_cost),
        patch.object(handler, "collect_pi_metrics", mock_pi),
        patch.object(handler, "collect_cw_metrics", mock_cw),
        patch.object(handler, "collect_pg_baselines", mock_baselines),
        patch.object(handler, "collect_dynamodb_metrics", mock_ddb_collector),
        patch.object(handler, "collect_docdb_metrics", mock_docdb_collector),
        # Mock the pg/mysql SQL collectors (need rds_data calls)
        patch.object(handler, "collect_query_stats", MagicMock(return_value={})),
        patch.object(handler, "collect_pg_table_stats", MagicMock(return_value={})),
        patch.object(handler, "collect_pg_activity", MagicMock(return_value={})),
        patch.object(handler, "collect_pg_locks", MagicMock(return_value={})),
        patch.object(handler, "collect_pg_health_checks", MagicMock(return_value={})),
        patch.object(handler, "collect_param_fitness", MagicMock(return_value={})),
        patch.object(handler, "collect_capacity_forecast", MagicMock(return_value={})),
        patch.object(handler, "collect_pg_extensions", MagicMock(return_value={})),
    ):
        # describe_db_instances needs to return something; patch via get_client mock
        fake_rds_client = MagicMock()
        fake_rds_client.describe_db_instances.return_value = {"DBInstances": []}

        def patched_get_client(service, region):
            if service == "rds":
                return fake_rds_client
            return MagicMock()

        kwargs = dict(_COMMON_KWARGS)
        kwargs["get_client"] = patched_get_client

        result = handler._collect_one(resource, **kwargs)

    # Relational path: meta AND cost must be called
    mock_meta.assert_called_once()
    mock_cost.assert_called_once()
    # Non-relational collectors must NOT be called
    mock_ddb_collector.assert_not_called()
    mock_docdb_collector.assert_not_called()
    assert result["cluster_id"] == "prod-pg-1"


# ---------------------------------------------------------------------------
# Test 4: RDS instance (non-Aurora) routes ONLY to collect_rds_instance_metrics
# ---------------------------------------------------------------------------

def test_rds_instance_routes_to_instance_collector_only():
    handler = _load_handler()

    resource = {
        "cluster_id": "dbops-demo-mysql",
        "engine": "mysql",
        "engine_family": "rds_instance",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }

    mock_inst_collector = MagicMock(return_value={
        "metrics_inserted": 7, "errors": [],
        "resource_id": None, "pi_enabled": False})
    mock_meta = MagicMock()
    mock_pi = MagicMock()
    mock_cw = MagicMock()
    mock_cost = MagicMock(return_value={})
    mock_capforecast = MagicMock(return_value={})
    mock_qregr = MagicMock(return_value={})
    mock_baselines = MagicMock(return_value={})

    with (
        patch.object(handler, "collect_rds_instance_metrics", mock_inst_collector),
        patch.object(handler, "collect_cluster_meta", mock_meta),
        patch.object(handler, "collect_pi_metrics", mock_pi),
        patch.object(handler, "collect_cw_metrics", mock_cw),
        patch.object(handler, "collect_cost_findings", mock_cost),
        patch.object(handler, "collect_capacity_forecast", mock_capforecast),
        patch.object(handler, "collect_query_regression", mock_qregr),
        patch.object(handler, "collect_pg_baselines", mock_baselines),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    mock_inst_collector.assert_called_once()
    # Aurora-cluster meta / cluster-dimension CW must NOT run; PI must not run
    # either (pi_enabled=False in the collector result).
    mock_meta.assert_not_called()
    mock_cw.assert_not_called()
    mock_pi.assert_not_called()
    # Engine-agnostic cache-only advisory collectors DO run for rds_instance:
    # cost, capacity/storage forecast, query regression, seasonal baselines.
    mock_cost.assert_called_once()
    mock_capforecast.assert_called_once()
    mock_qregr.assert_called_once()
    mock_baselines.assert_called_once()
    # capacity_forecast gets the engine so its storage-exhaustion branch keys off it.
    assert mock_capforecast.call_args.kwargs.get("engine") == "mysql"
    # SHARED run_ts contract: cost/capacity_forecast/query_regression MUST all receive
    # snapshot_ts=run_ts so the dashboard's per-check_type latest-in-window sees them as
    # one batch. A regression dropping run_ts here would scatter them across snapshot_times.
    _rt = _COMMON_KWARGS["run_ts"]
    assert mock_cost.call_args.kwargs.get("snapshot_ts") == _rt
    assert mock_capforecast.call_args.kwargs.get("snapshot_ts") == _rt
    assert mock_qregr.call_args.kwargs.get("snapshot_ts") == _rt
    # baselines writes its own NOW() — no snapshot_ts passed.
    assert "snapshot_ts" not in mock_baselines.call_args.kwargs
    assert result["cluster_id"] == "dbops-demo-mysql"


def test_rds_instance_runs_pi_when_enabled():
    handler = _load_handler()

    resource = {
        "cluster_id": "dbops-demo-mysql",
        "engine": "mysql",
        "engine_family": "rds_instance",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }

    mock_inst_collector = MagicMock(return_value={
        "metrics_inserted": 7, "errors": [],
        "resource_id": "db-ABC", "pi_enabled": True})
    mock_pi = MagicMock(return_value={"rows": 1})

    with (
        patch.object(handler, "collect_rds_instance_metrics", mock_inst_collector),
        patch.object(handler, "collect_pi_metrics", mock_pi),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    mock_pi.assert_called_once()
    # resource_id from the collector (NOT a db-cluster-id filtered lookup)
    assert mock_pi.call_args.args[2] == "db-ABC"
    # engine-safe reduced metric list (rds_instance is non-Aurora; the full
    # Aurora PI list fails the whole GetResourceMetrics call on SQL Server)
    assert mock_pi.call_args.kwargs["metrics"] is handler.PI_METRICS_RDS_INSTANCE
    assert "pi" in result


def test_rds_instance_mysql_calls_param_fitness_with_cache_args():
    """MySQL rds_instance runs the cache-only param_fitness finding using the
    CACHE connection (not the target) and the shared run_ts as snapshot_ts."""
    handler = _load_handler()

    resource = {
        "cluster_id": "dbops-demo-mysql",
        "engine": "mysql",
        "engine_family": "rds_instance",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }
    mock_inst_collector = MagicMock(return_value={
        "metrics_inserted": 7, "errors": [],
        "resource_id": None, "pi_enabled": False})
    mock_pf = MagicMock(return_value={"findings": []})

    with (
        patch.object(handler, "collect_rds_instance_metrics", mock_inst_collector),
        patch.object(handler, "collect_mysql_param_fitness", mock_pf),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    mock_pf.assert_called_once()
    # First positional arg is the CACHE rds-data client, not any target conn.
    assert mock_pf.call_args.args[0] is _COMMON_KWARGS["cache_rds_data"]
    assert mock_pf.call_args.args[1] == _COMMON_KWARGS["cache_cluster_arn"]
    assert mock_pf.call_args.args[2] == _COMMON_KWARGS["cache_secret_arn"]
    assert mock_pf.call_args.args[3] == _COMMON_KWARGS["cache_db_name"]
    assert mock_pf.call_args.args[4] == "dbops-demo-mysql"
    assert mock_pf.call_args.kwargs["snapshot_ts"] == _COMMON_KWARGS["run_ts"]
    assert "param_fitness" in result


def test_rds_instance_sqlserver_does_not_call_param_fitness():
    """SQL Server has no cache-only param_fitness finding — must not be called."""
    handler = _load_handler()

    resource = {
        "cluster_id": "dbops-demo-mssql",
        "engine": "sqlserver-ee",
        "engine_family": "rds_instance",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }
    mock_inst_collector = MagicMock(return_value={
        "metrics_inserted": 4, "errors": [],
        "resource_id": None, "pi_enabled": False})
    mock_pf = MagicMock()

    with (
        patch.object(handler, "collect_rds_instance_metrics", mock_inst_collector),
        patch.object(handler, "collect_mysql_param_fitness", mock_pf),
    ):
        handler._collect_one(resource, **_COMMON_KWARGS)

    mock_pf.assert_not_called()

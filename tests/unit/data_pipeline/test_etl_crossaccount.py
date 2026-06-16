"""ETL collector cross-account session tests.

Registered clusters can live in spoke accounts (registry row carries a
``spoke_role_arn``). The collector must assume that role so TARGET reads run in
the cluster's OWN account, while CACHE writes always stay in the hub account.
These tests pin both the ``_session_for`` primitive and the target-spoke /
cache-local split wired through ``lambda_handler``.

Same importlib-from-path loader as test_etl_dispatch.py.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load_handler():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location("etl_handler", _ROOT / "handler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_session_for_no_role_is_local_no_sts(monkeypatch):
    """No spoke role → transparent local session, STS never touched."""
    m = _load_handler()
    fake_boto3 = MagicMock()
    monkeypatch.setattr(m, "boto3", fake_boto3)

    m._session_for("ap-northeast-2", "")

    fake_boto3.client.assert_not_called()  # no sts.assume_role
    fake_boto3.session.Session.assert_called_once_with(region_name="ap-northeast-2")


def test_session_for_with_role_assumes_spoke(monkeypatch):
    """With a spoke role → assume it (900s) and build a session from the creds."""
    m = _load_handler()
    sts = MagicMock()
    sts.assume_role.return_value = {"Credentials": {
        "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TOK"}}
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = sts
    monkeypatch.setattr(m, "boto3", fake_boto3)

    role = "arn:aws:iam::999988887777:role/dbops-spoke-role"
    m._session_for("us-east-1", role)

    sts.assume_role.assert_called_once()
    kw = sts.assume_role.call_args.kwargs
    assert kw["RoleArn"] == role
    assert kw["DurationSeconds"] == 900
    fake_boto3.session.Session.assert_called_once_with(
        region_name="us-east-1",
        aws_access_key_id="AK", aws_secret_access_key="SK", aws_session_token="TOK",
    )


def test_handler_target_uses_spoke_cache_uses_local(monkeypatch):
    """A cross-account relational cluster: TARGET SQL collectors get the SPOKE
    rds-data client; CACHE-write collectors get the LOCAL rds-data client; the
    spoke role is assumed exactly once for the run."""
    m = _load_handler()

    cross = {
        "cluster_id": "spoke-pg-1",
        "engine": "aurora-postgresql",
        "engine_family": "aurora-postgresql",
        "region": "ap-northeast-2",
        "account_id": "999988887777",
        "spoke_role_arn": "arn:aws:iam::999988887777:role/dbops-spoke-role",
        "cluster_arn": "arn:aws:rds:ap-northeast-2:999988887777:cluster:spoke-pg-1",
        "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:999988887777:secret:spoke",
        "db_name": "appdb",
    }

    # Distinct identities so we can prove which client lands where.
    cache_rds_data = MagicMock(name="cache_rds_data_local")
    spoke_rds_data = MagicMock(name="spoke_rds_data")
    spoke_rds = MagicMock(name="spoke_rds")
    spoke_rds.describe_db_instances.return_value = {"DBInstances": []}

    spoke_session = MagicMock(name="spoke_session")
    spoke_session.client.side_effect = lambda svc, *a, **k: {
        "rds-data": spoke_rds_data, "rds": spoke_rds,
    }.get(svc, MagicMock())

    sts = MagicMock(name="sts")
    sts.assume_role.return_value = {"Credentials": {
        "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TOK"}}

    ddb_table = MagicMock()
    ddb_table.scan.return_value = {"Items": [cross]}
    ddb_resource = MagicMock()
    ddb_resource.Table.return_value = ddb_table

    fake_boto3 = MagicMock()
    fake_boto3.resource.return_value = ddb_resource
    fake_boto3.client.side_effect = lambda svc, *a, **k: {
        "rds-data": cache_rds_data, "sts": sts,
    }.get(svc, MagicMock())
    fake_boto3.session.Session.return_value = spoke_session
    monkeypatch.setattr(m, "boto3", fake_boto3)

    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cache")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:cachesecret")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")

    # Stub every collector; capture the rds-data client passed to a TARGET one
    # (query_stats) and a CACHE one (cost_findings).
    captured = {}
    for fn in ("collect_cluster_meta", "collect_pi_metrics", "collect_cw_metrics",
               "collect_pg_table_stats", "collect_pg_activity", "collect_pg_locks",
               "collect_pg_health_checks", "collect_param_fitness",
               "collect_capacity_forecast", "collect_pg_extensions",
               "collect_pg_baselines"):
        monkeypatch.setattr(m, fn, MagicMock(return_value={}))

    def _cap(name):
        def _fn(rds_data_client, *a, **k):
            captured[name] = rds_data_client
            return {}
        return _fn
    monkeypatch.setattr(m, "collect_query_stats", _cap("target"))
    monkeypatch.setattr(m, "collect_cost_findings", _cap("cache"))

    m.lambda_handler({}, None)

    sts.assume_role.assert_called_once()
    assert sts.assume_role.call_args.kwargs["RoleArn"] == cross["spoke_role_arn"]
    assert captured["target"] is spoke_rds_data   # TARGET SQL → spoke account
    assert captured["cache"] is cache_rds_data     # cache writes → hub account

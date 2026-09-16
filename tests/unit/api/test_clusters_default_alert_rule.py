"""Default `<cpu> > 80` alert rule seeded when a cluster is registered.

Measured before this existed: 11 registered clusters, ONE of which had any
alert_rules row (and that one had the same rule twice), so the whole
alert -> notification -> RCA path was dead for every cluster an operator cares
about.

What these tests actually pin:
  * the metric name is PER FAMILY, and it is the name the collectors write
    (SIGNAL_SETS in mcp_servers/shared/incident_signals.py). A fixture that
    cannot tell cpu from cpu_utilization from cache_cpu proves nothing, so every
    family asserts its own literal.
  * dynamodb seeds NOTHING. It has no CPU gauge, so a cpu rule there is a row
    that can never fire while reading like coverage.
  * a failed rule insert still returns a successful registration.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
# Push clusters/ dir so `import seeder` and `import engine_family` both resolve.
sys.path.insert(0, str(_CLUSTERS_DIR))

_PATH = _CLUSTERS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler_dar", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:1:cluster:cache")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:1:secret:cache")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")


def _table():
    t = MagicMock()
    t.get_item.return_value = {}
    return t


def _rds_data_of(fake_boto3):
    """The rds-data client the handler asked boto3 for."""
    return fake_boto3.client.return_value


def _seeded_params(fake_boto3):
    """{param name -> value} of the single seeded INSERT, or None if none ran."""
    calls = _rds_data_of(fake_boto3).execute_statement.call_args_list
    inserts = [c for c in calls if "INSERT INTO alert_rules" in c.kwargs["sql"]]
    if not inserts:
        return None
    assert len(inserts) == 1, f"expected one seeded rule, got {len(inserts)}"
    kwargs = inserts[0].kwargs
    flat = {}
    for p in kwargs["parameters"]:
        flat[p["name"]] = next(iter(p["value"].values()))
    flat["__sql__"] = kwargs["sql"]
    return flat


# ---------------------------------------------------------------------------
# Per-family metric name
# ---------------------------------------------------------------------------

def _register_relational(fake_boto3, cid="prod-aurora-pg"):
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterArn": f"arn:aws:rds:ap-northeast-2:1:cluster:{cid}",
                        "Engine": "aurora-postgresql", "DatabaseName": "app"}]
    }
    with patch.object(handler, "_rds_client_for", return_value=rds):
        return handler._handle_register(_table(), {
            "cluster_id": cid, "account_id": "111122223333",
            "region": "ap-northeast-2", "engine": "aurora-postgresql"})


def _register_rds_instance(fake_boto3, cid="prod-mysql", engine="mysql"):
    rds = MagicMock()
    rds.describe_db_instances.return_value = {
        "DBInstances": [{"Engine": engine, "EngineVersion": "8.0.39",
                         "Endpoint": {"Address": "h", "Port": 3306}}]
    }
    with patch.object(handler, "_rds_client_for", return_value=rds), \
         patch.object(handler, "_convention_secret_for", return_value=""), \
         patch.object(handler, "_session_for", return_value=MagicMock()):
        return handler._handle_register(_table(), {
            "cluster_id": cid, "account_id": "111122223333",
            "region": "ap-northeast-2", "engine": engine})


def _register_docdb(fake_boto3, cid="prod-docdb"):
    docdb = MagicMock()
    docdb.describe_db_clusters.return_value = {"DBClusters": [{"EngineVersion": "5.0.0"}]}
    with patch.object(handler, "_docdb_client_for", return_value=docdb):
        return handler._handle_register(_table(), {
            "cluster_id": cid, "account_id": "111122223333",
            "region": "ap-northeast-2", "engine": "docdb"})


def _register_elasticache(fake_boto3, name="prod-redis"):
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {
        "ReplicationGroups": [{"ReplicationGroupId": name, "Status": "available",
                               "Engine": "redis", "MemberClusters": [f"{name}-001"],
                               "CacheNodeType": "cache.t4g.micro"}]
    }
    with patch.object(handler, "_elasticache_client_for", return_value=ec):
        return handler._handle_register(_table(), {
            "account_id": "111122223333", "region": "ap-northeast-2",
            "resource_name": name, "engine": "redis"})


def _register_dynamodb(fake_boto3, name="Orders"):
    ddb = MagicMock()
    ddb.describe_table.return_value = {"Table": {"TableName": name}}
    with patch.object(handler, "_ddb_client_for", return_value=ddb):
        return handler._handle_register(_table(), {
            "account_id": "111122223333", "region": "ap-northeast-2",
            "resource_name": name, "engine": "dynamodb"})


@pytest.mark.parametrize("register, expected_metric", [
    (_register_relational, "cpu"),
    (_register_rds_instance, "cpu"),
    (_register_docdb, "cpu_utilization"),
    (_register_elasticache, "cache_cpu"),
])
def test_registration_seeds_family_cpu_metric(register, expected_metric):
    with patch.object(handler, "boto3") as fake_boto3:
        resp = register(fake_boto3)
    assert resp["statusCode"] in (201, 207), resp["body"]
    params = _seeded_params(fake_boto3)
    assert params is not None, "no default alert rule was seeded"
    assert params["metric"] == expected_metric
    assert params["threshold"] == 80.0
    assert params["cid"] == json.loads(resp["body"])["cluster_id"]
    assert params["name"] == f"{expected_metric} > 80"
    # enabled TRUE and comparison '>' are SQL literals, not parameters.
    sql = params["__sql__"]
    assert "'>'" in sql and "TRUE" in sql
    # Re-registering must not stack a second copy.
    assert "NOT EXISTS" in sql


def test_dynamodb_registration_seeds_no_rule():
    """DynamoDB has no CPU gauge at all. A cpu rule on a table can never fire,
    and a rule that cannot fire is worse than no rule: it reads as coverage."""
    with patch.object(handler, "boto3") as fake_boto3:
        resp = _register_dynamodb(fake_boto3)
    assert resp["statusCode"] in (201, 207), resp["body"]
    assert _seeded_params(fake_boto3) is None, "a DynamoDB table must get no cpu rule"


# ---------------------------------------------------------------------------
# Seeding never breaks registration
# ---------------------------------------------------------------------------

def test_rule_insert_failure_still_registers():
    """Registration is the operator's action; the rule is a convenience."""
    with patch.object(handler, "boto3") as fake_boto3:
        _rds_data_of(fake_boto3).execute_statement.side_effect = RuntimeError("cache DB down")
        resp = _register_relational(fake_boto3)
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["status"] == "registered"


def test_no_rule_when_cache_db_unconfigured(monkeypatch):
    monkeypatch.delenv("CACHE_DB_CLUSTER_ARN", raising=False)
    with patch.object(handler, "boto3") as fake_boto3:
        resp = _register_relational(fake_boto3)
    assert resp["statusCode"] == 201
    assert _seeded_params(fake_boto3) is None


def test_rejected_registration_seeds_nothing():
    with patch.object(handler, "boto3") as fake_boto3:
        resp = handler._handle_register(_table(), {"engine": "dynamodb"})  # missing fields
    assert resp["statusCode"] == 400
    assert _seeded_params(fake_boto3) is None


# ---------------------------------------------------------------------------
# Parity with the source of truth
# ---------------------------------------------------------------------------

def test_metric_names_match_incident_signals():
    """api/ cannot import mcp_servers, so the map is inlined in the handler.
    This is the parity test that keeps the copy honest: every seeded metric must
    be a gauge the readers actually query for that family."""
    from mcp_servers.shared.incident_signals import SIGNAL_SETS

    for family, metric in handler._DEFAULT_ALERT_CPU_METRIC.items():
        gauges = SIGNAL_SETS[family]["gauges"]
        assert metric in gauges, f"{family}: {metric} is not in SIGNAL_SETS gauges {gauges}"
        # Each family has exactly one CPU gauge except elasticache, which has
        # cache_cpu (node) and engine_cpu (single-threaded engine); we seed the
        # node-level one.
        cpu_gauges = [g for g in gauges if "cpu" in g]
        assert metric in cpu_gauges and (len(cpu_gauges) == 1 or metric == "cache_cpu")

    # The exclusion, from the same source: dynamodb has no cpu series anywhere.
    ddb = SIGNAL_SETS["dynamodb"]
    assert not [m for m in tuple(ddb["gauges"]) + tuple(ddb["counters"]) if "cpu" in m]
    assert "dynamodb" not in handler._DEFAULT_ALERT_CPU_METRIC

    # Every family the registration dispatch can produce is accounted for.
    assert set(handler._DEFAULT_ALERT_CPU_METRIC) | {"dynamodb"} == set(SIGNAL_SETS)


# ---------------------------------------------------------------------------
# The sample/demo cluster takes the same path
# ---------------------------------------------------------------------------

def test_seeding_the_sample_cluster_also_seeds_its_alert_rule():
    """POST /api/clusters/seed-sample writes a registry row of its own instead
    of going through _handle_register, so it silently missed the rule.

    That is the worst cluster to miss: the demo exists to show an alert firing
    and pulling an automatic RCA behind it, and the cluster the demo hands you
    was the one with metrics and no rule to evaluate them against.
    """
    with patch.object(handler, "boto3") as fake_boto3, \
         patch.object(handler.seeder, "seed_demo_data",
                      return_value={"metric_snapshots": 120}) as seed, \
         patch.object(handler, "_put_registry_item") as put:
        resp = handler._handle_seed_sample(_table())

    assert resp["statusCode"] == 201, resp["body"]
    body = json.loads(resp["body"])
    assert body["status"] == "seeded"
    assert seed.called and put.called, "guard: the seed path itself must have run"

    params = _seeded_params(fake_boto3)
    assert params is not None, "the sample cluster got no default alert rule"
    # aurora-postgresql is `relational`, whose collector metric is plain `cpu`.
    assert params["metric"] == "cpu"
    assert params["cid"] == handler.seeder.SAMPLE_CLUSTER_ID
    assert params["name"] == "cpu > 80"
    assert params["threshold"] == 80.0
    # Seeding is documented as idempotent and IS re-run, so the NOT EXISTS
    # guard is what keeps a second demo reset from stacking a duplicate rule.
    assert "NOT EXISTS" in params["__sql__"]


def test_a_failed_sample_seed_writes_no_alert_rule():
    """The mirror case. seed_demo_data raising returns 500 and writes no
    registry row, so a rule for a cluster that is not registered must not
    appear either."""
    with patch.object(handler, "boto3") as fake_boto3, \
         patch.object(handler.seeder, "seed_demo_data",
                      side_effect=RuntimeError("relation does not exist")):
        resp = handler._handle_seed_sample(_table())

    assert resp["statusCode"] == 500
    assert _seeded_params(fake_boto3) is None

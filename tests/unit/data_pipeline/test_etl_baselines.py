"""Seasonal baseline training for ALL five engine families (E1-4).

`collect_pg_baselines` reads only `metric_snapshots` and writes
`metric_baselines`, so it is engine-agnostic. It used to be called from the
relational and rds_instance branches only, and the other three branches
`return result` early, so DocumentDB / DynamoDB / ElastiCache anomaly detection
was permanently stuck on the low-confidence flat mean/stddev fallback.

Three layers here:

  1. Dispatch: the trainer is invoked from every family branch, with the CACHE
     connection and no shared run_ts (it stamps its own NOW()).
  2. Result: the trainer's REAL SQL is pushed through a model of the parts that
     decide which rows are learned (dimension predicate, 14-day/1-hour window,
     hour-of-week bucketing, the >= 3 sample gate). Only cluster-level rows may
     train, for every family: a per-GSI / per-instance / per-wait-event row
     carries the same metric_type and would poison the baseline.
  3. Consequence: a family with trained buckets makes `detect_anomalies` report
     mode='seasonal'; without them the same series reports 'flat'.
"""

import contextlib
import importlib.util
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ETL = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_STRICT = "dimensions IS NULL OR dimensions::text = '{}'"


def _load_handler():
    if str(_ETL) not in sys.path:
        sys.path.insert(0, str(_ETL))
    spec = importlib.util.spec_from_file_location("etl_handler_baselines", _ETL / "handler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_HANDLER = _load_handler()
_train = _HANDLER.collect_pg_baselines          # the real collector

_CACHE_KWARGS = dict(
    cache_rds_data=MagicMock(name="cache_rds_data"),
    cache_execute=lambda sql, params: None,
    cache_cluster_arn="arn:aws:rds:ap-northeast-2:123:cluster:cache",
    cache_secret_arn="arn:aws:secretsmanager:ap-northeast-2:123:secret:cache",
    cache_db_name="dbops",
    run_ts="2026-07-24T00:00:00+00:00",
)

# One resource + the collectors that branch owns, per non-relational family.
_FAMILIES = [
    ("dynamodb", {"cluster_id": "ddb-orders", "engine": "dynamodb", "resource_name": "Orders"},
     ("collect_dynamodb_metrics", "collect_dynamodb_findings"), "consumed_rcu"),
    ("documentdb", {"cluster_id": "docdb-1", "engine": "docdb"},
     ("collect_docdb_metrics", "collect_docdb_findings"), "storage_bytes"),
    ("elasticache", {"cluster_id": "cache-1", "engine": "redis", "resource_name": "cache-1"},
     ("collect_elasticache_metrics", "collect_elasticache_findings"), "memory_usage_pct"),
]


# ===========================================================================
# 1. Dispatch: every family branch reaches the trainer
# ===========================================================================


@pytest.mark.parametrize("family,resource,own_collectors,_metric", _FAMILIES)
def test_non_relational_branch_trains_baselines(family, resource, own_collectors, _metric):
    """The three branches return EARLY, before the shared relational tail, so the
    call has to live inside each branch."""
    handler = _load_handler()
    mock_trainer = MagicMock(return_value={"bucket_count": 12, "metric_count": 3})
    # Aurora-only collectors must stay untouched by these families.
    forbidden = {n: MagicMock() for n in
                 ("collect_cluster_meta", "collect_pi_metrics", "collect_cw_metrics",
                  "collect_cost_findings", "collect_capacity_forecast")}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(handler, "collect_pg_baselines", mock_trainer))
        for name in own_collectors:
            stack.enter_context(patch.object(handler, name, MagicMock(return_value={})))
        for name, m in forbidden.items():
            stack.enter_context(patch.object(handler, name, m))
        result = handler._collect_one(
            dict(resource, engine_family=family, region="ap-northeast-2",
                 account_id="111122223333"),
            get_client=lambda *a, **k: MagicMock(), **_CACHE_KWARGS)

    mock_trainer.assert_called_once()
    # CACHE connection, positional, exactly as the relational branch passes it.
    assert mock_trainer.call_args.args == (
        _CACHE_KWARGS["cache_rds_data"], _CACHE_KWARGS["cache_cluster_arn"],
        _CACHE_KWARGS["cache_secret_arn"], _CACHE_KWARGS["cache_db_name"],
        resource["cluster_id"])
    # run_ts is for finding collectors sharing one snapshot_time batch; the
    # trainer writes its own NOW() into metric_baselines instead.
    assert mock_trainer.call_args.kwargs == {}
    assert result["baselines"] == {"bucket_count": 12, "metric_count": 3}
    assert "baselines_error" not in result
    assert [n for n, m in forbidden.items() if m.called] == []


@pytest.mark.parametrize("family,resource,own_collectors,_metric", _FAMILIES)
def test_trainer_failure_does_not_break_the_branch(family, resource, own_collectors, _metric):
    """Best-effort, same as every other collector in this handler."""
    handler = _load_handler()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(
            handler, "collect_pg_baselines", MagicMock(side_effect=RuntimeError("boom"))))
        for name in own_collectors:
            stack.enter_context(patch.object(handler, name, MagicMock(return_value={"ok": 1})))
        result = handler._collect_one(
            dict(resource, engine_family=family, region="ap-northeast-2", account_id="1"),
            get_client=lambda *a, **k: MagicMock(), **_CACHE_KWARGS)
    assert "baselines_error" in result
    assert result["cluster_id"] == resource["cluster_id"]


def test_relational_still_trains_exactly_once_with_unchanged_arguments():
    """Aurora regression pin: E1-4 moved the relational call into a shared
    helper, so its argument shape and call count must be provably identical."""
    handler = _load_handler()
    mock_trainer = MagicMock(return_value={"bucket_count": 1})
    stubs = ("collect_cluster_meta", "collect_pi_metrics", "collect_cw_metrics",
             "collect_cw_instance_metrics", "collect_cost_findings", "collect_query_stats",
             "collect_pg_table_stats", "collect_pg_activity", "collect_pg_locks",
             "collect_pg_health_checks", "collect_param_fitness", "collect_capacity_forecast",
             "collect_pg_extensions", "collect_pg_engine_internals", "collect_query_regression")
    with contextlib.ExitStack() as stack:
        for n in stubs:
            stack.enter_context(patch.object(handler, n, MagicMock(return_value={})))
        stack.enter_context(patch.object(handler, "collect_pg_baselines", mock_trainer))
        result = handler._collect_one(
            {"cluster_id": "prod-pg-1", "engine": "aurora-postgresql",
             "region": "ap-northeast-2", "account_id": "1",
             "cluster_arn": "arn:aws:rds:ap-northeast-2:1:cluster:prod-pg-1",
             "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:1:secret:pg",
             "db_name": "sampledb"},
            get_client=lambda *a, **k: MagicMock(), **_CACHE_KWARGS)

    mock_trainer.assert_called_once()
    assert mock_trainer.call_args.args == (
        _CACHE_KWARGS["cache_rds_data"], _CACHE_KWARGS["cache_cluster_arn"],
        _CACHE_KWARGS["cache_secret_arn"], _CACHE_KWARGS["cache_db_name"], "prod-pg-1")
    assert mock_trainer.call_args.kwargs == {}
    assert result["baselines"] == {"bucket_count": 1}


def test_rds_instance_still_trains_exactly_once():
    handler = _load_handler()
    mock_trainer = MagicMock(return_value={"bucket_count": 1})
    with (
        patch.object(handler, "collect_rds_instance_metrics", MagicMock(return_value={
            "metrics_inserted": 3, "errors": [], "resource_id": None, "pi_enabled": False})),
        patch.object(handler, "collect_mysql_param_fitness", MagicMock(return_value={})),
        patch.object(handler, "collect_cost_findings", MagicMock(return_value={})),
        patch.object(handler, "collect_capacity_forecast", MagicMock(return_value={})),
        patch.object(handler, "collect_query_regression", MagicMock(return_value={})),
        patch.object(handler, "collect_pg_baselines", mock_trainer),
    ):
        handler._collect_one(
            {"cluster_id": "demo-mysql", "engine": "mysql", "engine_family": "rds_instance",
             "region": "ap-northeast-2", "account_id": "1"},
            get_client=lambda *a, **k: MagicMock(), **_CACHE_KWARGS)
    mock_trainer.assert_called_once()
    assert mock_trainer.call_args.kwargs == {}


# ===========================================================================
# 2. Result: which rows the trainer's REAL SQL learns
# ===========================================================================

_NOW = datetime.now(timezone.utc)
# Anchor one week back at the top of the hour: same hour-of-week bucket as NOW
# (168h), safely inside the 14-day lookback and outside the "last hour" cutoff,
# and immune to whatever minute the test happens to run at.
_BASE = _NOW.replace(minute=0, second=0, microsecond=0) - timedelta(days=7)
_HOW = ((_BASE.weekday() + 1) % 7) * 24 + _BASE.hour     # PostgreSQL DOW: Sunday=0

_CLUSTER_VALUES = [10.0, 20.0, 30.0, 40.0, 50.0]         # median 30, IQR 20
_CLUSTER_MEDIAN = 30.0
_CLUSTER_IQR = 20.0
_POISON = 9999.0                                          # must never be learned


def _rows_for(metric_type):
    """Cluster-level rows plus every dimensioned shape that shares the SAME
    metric_type, plus two rows the time window must exclude."""
    rows = [{"ts": _BASE + timedelta(minutes=5 * i), "metric_type": metric_type,
             "value": v, "dimensions": {}} for i, v in enumerate(_CLUSTER_VALUES)]
    rows += [
        {"ts": _BASE + timedelta(minutes=1), "metric_type": metric_type,
         "value": 1.0, "dimensions": {"gsi": "byUser"}},
        {"ts": _BASE + timedelta(minutes=2), "metric_type": metric_type,
         "value": 2.0, "dimensions": {"instance": "i-1", "role": "writer"}},
        {"ts": _BASE + timedelta(minutes=3), "metric_type": metric_type,
         "value": 3.0, "dimensions": {"db.wait_event.name": "CPU"}},
        # outside the 14-day lookback (same hour-of-week bucket)
        {"ts": _BASE - timedelta(days=21), "metric_type": metric_type,
         "value": _POISON, "dimensions": {}},
        # inside the 1-hour cutoff (metrics still landing this cycle)
        {"ts": _NOW - timedelta(minutes=10), "metric_type": metric_type,
         "value": _POISON, "dimensions": {}},
    ]
    return rows


def _model_training(sql, rows):
    """Model the parts of the trainer's INSERT..SELECT that decide what is
    learned, driven by the SQL the collector ACTUALLY issued."""
    flat = " ".join(sql.split())
    kept = rows if _STRICT not in flat else [r for r in rows if not r["dimensions"]]
    if "14 days" in flat:
        kept = [r for r in kept if r["ts"] > _NOW - timedelta(days=14)]
    if "INTERVAL '1 hour'" in flat:
        kept = [r for r in kept if r["ts"] < _NOW - timedelta(hours=1)]

    buckets = {}
    for r in kept:
        how = ((r["ts"].weekday() + 1) % 7) * 24 + r["ts"].hour
        buckets.setdefault((r["metric_type"], how), []).append(r["value"])

    out = []
    for (metric_type, how), vals in sorted(buckets.items()):
        if len(vals) < 3:            # HAVING COUNT(*) >= 3
            continue
        q = statistics.quantiles(vals, n=4, method="inclusive")
        out.append({"metric_type": metric_type, "hour_of_week": how,
                    "median": statistics.median(vals),
                    "iqr": max(q[2] - q[0], 0.01), "sample_count": len(vals)})
    return out


def _field(v):
    if v is None:
        return {"isNull": True}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


class _FakeRdsData:
    """Data API stand-in: answers the gate, models the INSERT, reports counts."""

    def __init__(self, rows, hours_since=None):
        self.rows = rows
        self.hours_since = hours_since
        self.trained = None
        self.insert_sql = None

    def _resp(self, cols, records):
        return {"columnMetadata": [{"name": c} for c in cols],
                "records": [[_field(v) for v in rec] for rec in records]}

    def execute_statement(self, **kw):
        sql = kw["sql"]
        if "hours_since" in sql:
            return self._resp(["hours_since"], [[self.hours_since]])
        if sql.lstrip().startswith("/* source=dbops-baseline */ INSERT"):
            self.insert_sql = sql
            self.trained = _model_training(sql, self.rows)
            return {}
        if "bucket_count" in sql:
            t = self.trained or []
            return self._resp(["bucket_count", "metric_count"],
                              [[len(t), len({r["metric_type"] for r in t})]])
        raise AssertionError(f"unexpected SQL: {sql}")


def _run_trainer(metric_type, cluster_id="c"):
    fake = _FakeRdsData(_rows_for(metric_type))
    out = _train(fake, "arn:cache", "arn:secret", "dbops", cluster_id)
    return fake, out


@pytest.mark.parametrize("metric_type", ["consumed_rcu", "storage_bytes", "memory_usage_pct"])
def test_trainer_learns_cluster_level_rows_only(metric_type):
    """One baseline row per (metric, hour-of-week) built from the cluster-level
    series alone. The per-GSI / per-instance / per-wait-event rows share the
    metric_type: learning them would drag the median toward the fractions."""
    fake, out = _run_trainer(metric_type)

    assert len(fake.trained) == 1
    row = fake.trained[0]
    assert row["metric_type"] == metric_type
    assert row["hour_of_week"] == _HOW
    assert row["median"] == pytest.approx(_CLUSTER_MEDIAN)
    assert row["iqr"] == pytest.approx(_CLUSTER_IQR)
    assert row["sample_count"] == len(_CLUSTER_VALUES)
    assert out == {"cluster_id": "c", "bucket_count": 1, "metric_count": 1}

    # Discriminating: without the dimension predicate the SAME fixture trains a
    # different median and 8 samples, so this test cannot pass vacuously.
    polluted = _model_training(fake.insert_sql.replace(_STRICT, "true"), _rows_for(metric_type))
    assert polluted[0]["sample_count"] == 8
    assert polluted[0]["median"] != pytest.approx(_CLUSTER_MEDIAN)


@pytest.mark.parametrize("metric_type", ["consumed_rcu", "storage_bytes", "memory_usage_pct"])
def test_trainer_window_excludes_stale_and_still_landing_rows(metric_type):
    """The 14-day lookback and the 1-hour cutoff are both load-bearing: the
    fixture puts a 9999 cluster-level row on each side of them."""
    fake, _ = _run_trainer(metric_type)
    assert _POISON not in [r["median"] for r in fake.trained]
    assert fake.trained[0]["median"] == pytest.approx(_CLUSTER_MEDIAN)


def test_trainer_skips_when_a_fresh_baseline_exists():
    """The once-per-hour gate is inside the collector and family-blind, so
    calling it from five branches costs one cheap SELECT per cycle."""
    fake = _FakeRdsData(_rows_for("consumed_rcu"), hours_since=0.2)
    out = _train(fake, "arn:cache", "arn:secret", "dbops", "c")
    assert out["skipped"] == "fresh"
    assert fake.insert_sql is None


# ===========================================================================
# 3. Consequence: seasonal instead of flat for the newly trained families
# ===========================================================================

_ANOMALY_COLS = ["metric_type", "recent_max", "recent_avg", "baseline_mean",
                 "baseline_stddev", "z_score", "mode", "sample_count"]
_RECENT_MAX = 90.0
_FLAT_MEAN, _FLAT_STDDEV = 30.0, 10.0


def _anomaly_row(baselines, metric_type):
    """Model the CASE in detect_anomalies: a seasonal baseline row for the
    CURRENT hour-of-week with iqr > 0 wins, else the flat 7-day fallback."""
    b = next((r for r in baselines
              if r["metric_type"] == metric_type and r["hour_of_week"] == _HOW), None)
    if b and b["iqr"] > 0:
        return {"metric_type": metric_type, "recent_max": _RECENT_MAX, "recent_avg": 60.0,
                "baseline_mean": b["median"], "baseline_stddev": b["iqr"],
                "z_score": (_RECENT_MAX - b["median"]) / b["iqr"],
                "mode": "seasonal", "sample_count": b["sample_count"]}
    return {"metric_type": metric_type, "recent_max": _RECENT_MAX, "recent_avg": 60.0,
            "baseline_mean": _FLAT_MEAN, "baseline_stddev": _FLAT_STDDEV,
            "z_score": (_RECENT_MAX - _FLAT_MEAN) / _FLAT_STDDEV,
            "mode": "flat", "sample_count": None}


@pytest.mark.parametrize("family,_resource,_own,metric_type", _FAMILIES)
def test_trained_family_detects_anomalies_in_seasonal_mode(family, _resource, _own, metric_type):
    """End of the chain: the trainer now runs for this family, so its metric has
    a baseline bucket, and the agent's anomaly tool reports the high-confidence
    seasonal mode. Untrained (the pre-E1-4 state) it reports flat."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
    from mcp_servers.shared.models import QueryResult

    fake, _ = _run_trainer(metric_type, cluster_id=family)
    trained = _anomaly_row(fake.trained, metric_type)
    untrained = _anomaly_row([], metric_type)

    def _cache_returning(row):
        cache = MagicMock()
        cache.execute.return_value = QueryResult(
            columns=_ANOMALY_COLS, rows=[row], row_count=1)
        return cache

    hot = detect_anomalies_impl(_cache_returning(trained), family, hours=4, threshold=2.0)
    assert hot["baseline_mode"] == "seasonal"
    assert hot["anomalies"][0]["sample_count"] == len(_CLUSTER_VALUES)
    assert hot["anomalies"][0]["z_score"] == pytest.approx(
        (_RECENT_MAX - _CLUSTER_MEDIAN) / _CLUSTER_IQR)

    cold = detect_anomalies_impl(_cache_returning(untrained), family, hours=4, threshold=2.0)
    assert cold["baseline_mode"] == "flat"

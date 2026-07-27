"""Seasonal baseline training for ALL five engine families (E1-4), and the two
guards that keep a thin bucket from producing a confident-looking anomaly.

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
     hour-of-week bucketing, the sample gate, the IQR floors). Every number the
     model uses is parsed out of the SQL the collector actually issued, so
     loosening a gate in the trainer changes these results. Only cluster-level
     rows may train, for every family: a per-GSI / per-instance / per-wait-event
     row carries the same metric_type and would poison the baseline.
  3. Consequence: a family with trained buckets makes `detect_anomalies` report
     mode='seasonal'; without them the same series reports 'flat'. A thin bucket
     must NOT reach seasonal mode: 3 samples of 20.0 / 20.1 / 20.2 used to train
     median=20.1 / IQR=0.0999 and score a 21.0 reading at z=9.0.
"""

import contextlib
import importlib.util
import os
import re
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

# A genuinely well-populated bucket: 12 samples = one fully observed hour at the
# 5-minute ETL cadence, which is exactly the trainer's sample floor.
_CLUSTER_VALUES = [5.0 * (i + 1) for i in range(12)]      # median 32.5, IQR 27.5
_CLUSTER_MEDIAN = 32.5
_CLUSTER_IQR = 27.5                                       # >> 5% of the median
_POISON = 9999.0                                          # must never be learned


def _rows_for(metric_type, values=None):
    """Cluster-level rows plus every dimensioned shape that shares the SAME
    metric_type, plus two rows the time window must exclude."""
    rows = [{"ts": _BASE + timedelta(minutes=5 * i), "metric_type": metric_type,
             "value": v, "dimensions": {}}
            for i, v in enumerate(_CLUSTER_VALUES if values is None else values)]
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


def _gates(sql):
    """Read the trainer's two guards back OUT of its SQL, so a loosened gate
    changes what this model trains instead of silently passing."""
    flat = " ".join(sql.split())
    having = re.search(r"HAVING COUNT\(\*\) >= (\d+)", flat)
    assert having, f"sample gate missing from the trainer SQL: {flat}"
    rel = re.search(
        r"ABS\(PERCENTILE_CONT\(0\.5\) WITHIN GROUP \(ORDER BY value\)\) \* ([0-9.]+)", flat)
    absolute = re.search(r", ([0-9.]+) \) AS iqr", flat)
    assert absolute, f"absolute IQR floor missing from the trainer SQL: {flat}"
    return int(having.group(1)), float(rel.group(1)) if rel else 0.0, float(absolute.group(1))


def _model_training(sql, rows):
    """Model the parts of the trainer's INSERT..SELECT that decide what is
    learned, driven by the SQL the collector ACTUALLY issued."""
    flat = " ".join(sql.split())
    sample_floor, rel_floor, abs_floor = _gates(flat)
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
        if len(vals) < sample_floor:            # HAVING COUNT(*) >= floor
            continue
        q = statistics.quantiles(vals, n=4, method="inclusive")
        median = statistics.median(vals)
        out.append({"metric_type": metric_type, "hour_of_week": how, "median": median,
                    "iqr": max(q[2] - q[0], abs(median) * rel_floor, abs_floor),
                    "sample_count": len(vals)})
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
    """Data API stand-in: answers the gate, models the retire-DELETE and the
    INSERT, reports counts."""

    def __init__(self, rows, hours_since=None, existing=None):
        self.rows = rows
        self.hours_since = hours_since
        self.existing = list(existing or [])      # already-stored metric_baselines rows
        self.trained = None
        self.insert_sql = None
        self.delete_sql = None

    def _resp(self, cols, records):
        return {"columnMetadata": [{"name": c} for c in cols],
                "records": [[_field(v) for v in rec] for rec in records]}

    def execute_statement(self, **kw):
        sql = kw["sql"]
        if "hours_since" in sql:
            return self._resp(["hours_since"], [[self.hours_since]])
        if "DELETE FROM metric_baselines" in sql:
            self.delete_sql = sql
            floor = re.search(r"sample_count < (\d+)", " ".join(sql.split()))
            assert floor, f"retire-DELETE has no sample floor: {sql}"
            self.existing = [r for r in self.existing
                             if r["sample_count"] >= int(floor.group(1))]
            return {}
        if sql.lstrip().startswith("/* source=dbops-baseline */ INSERT"):
            self.insert_sql = sql
            self.trained = _model_training(sql, self.rows)
            return {}
        if "bucket_count" in sql:
            t = self.trained or []
            return self._resp(["bucket_count", "metric_count"],
                              [[len(t), len({r["metric_type"] for r in t})]])
        raise AssertionError(f"unexpected SQL: {sql}")


def _run_trainer(metric_type, cluster_id="c", values=None, existing=None):
    fake = _FakeRdsData(_rows_for(metric_type, values), existing=existing)
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
    # different median and 15 samples, so this test cannot pass vacuously.
    polluted = _model_training(fake.insert_sql.replace(_STRICT, "true"), _rows_for(metric_type))
    assert polluted[0]["sample_count"] == len(_CLUSTER_VALUES) + 3
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
    assert fake.delete_sql is None


# ===========================================================================
# 2b. The two guards on a thin bucket
# ===========================================================================

_THIN = [20.0, 20.1, 20.2]      # the reviewer's reproduction, on real PostgreSQL
_SPIKE = 21.0                   # 0.9 above the thin median of 20.1


def _pre_fix(sql):
    """The same SQL with both guards removed: the >= 3 sample gate and the
    absolute-only IQR floor this trainer shipped with."""
    old = re.sub(r"HAVING COUNT\(\*\) >= \d+", "HAVING COUNT(*) >= 3", " ".join(sql.split()))
    return re.sub(r"ABS\(PERCENTILE_CONT\(0\.5\)[^*]*\* [0-9.]+, ", "", old)


def test_three_samples_in_one_hour_train_no_baseline():
    """3 samples is 15 minutes of a 5-minute-cadence hour, not an observed
    hour-of-week, so nothing may be learned from it."""
    fake, out = _run_trainer("memory_usage_pct", values=_THIN)
    assert fake.trained == []
    assert out["bucket_count"] == 0

    # Discriminating: the pre-fix SQL trains it, reproducing the review numbers.
    was = _model_training(_pre_fix(fake.insert_sql), _rows_for("memory_usage_pct", _THIN))
    assert was[0]["sample_count"] == 3
    assert was[0]["median"] == pytest.approx(20.1)
    assert was[0]["iqr"] == pytest.approx(0.1, abs=1e-6)
    assert (_SPIKE - was[0]["median"]) / was[0]["iqr"] == pytest.approx(9.0, abs=1e-6)


@pytest.mark.parametrize("n_samples,trains", [(11, False), (12, True)])
def test_sample_gate_pinned_at_one_fully_observed_hour(n_samples, trains):
    """The ETL cadence is 5 minutes, so 12 samples is one fully observed hour and
    11 is not. The model reads the floor out of the SQL the trainer issued, so
    loosening `HAVING COUNT(*)` changes these results and fails here."""
    fake, out = _run_trainer("consumed_rcu", values=[5.0 * (i + 1) for i in range(n_samples)])
    assert bool(fake.trained) is trains
    assert out["bucket_count"] == (1 if trains else 0)
    assert _gates(fake.insert_sql)[0] == 12


def test_iqr_floor_is_relative_to_the_median():
    """A bucket CAN be flat and still clear the sample floor (12 near-identical
    samples), so the second guard carries it: 5% of the median 20.1 is 1.005, so
    a 0.9 move scores z=0.90 instead of z=9.0. The absolute 0.01 floor is
    powerless at this median, and a bucket with real spread keeps its real IQR."""
    fake, _ = _run_trainer("memory_usage_pct", values=_THIN * 4)
    row = fake.trained[0]
    assert row["sample_count"] == 12
    assert row["median"] == pytest.approx(20.1)
    assert row["iqr"] == pytest.approx(1.005)                    # raw IQR is 0.2
    assert (_SPIKE - row["median"]) / row["iqr"] == pytest.approx(0.896, abs=0.001)

    was = _model_training(_pre_fix(fake.insert_sql), _rows_for("memory_usage_pct", _THIN * 4))
    assert (_SPIKE - was[0]["median"]) / was[0]["iqr"] == pytest.approx(4.5, abs=0.01)


def test_thin_baselines_trained_under_the_old_gate_are_retired():
    """The INSERT only upserts, so tightening the gate alone would leave the
    already-trained thin buckets (relational, rds_instance) scoring seasonal
    anomalies forever."""
    stale = {"metric_type": "memory_usage_pct", "hour_of_week": _HOW,
             "median": 20.1, "iqr": 0.0999, "sample_count": 3}
    healthy = dict(stale, metric_type="consumed_rcu", iqr=27.5, sample_count=24)
    fake, _ = _run_trainer("memory_usage_pct", existing=[stale, healthy])
    assert fake.existing == [healthy]
    assert _gates(fake.insert_sql)[0] == int(
        re.search(r"sample_count < (\d+)", fake.delete_sql).group(1))


# ===========================================================================
# 3. Consequence: seasonal instead of flat for the newly trained families
# ===========================================================================

_ANOMALY_COLS = ["metric_type", "recent_max", "recent_avg", "baseline_mean",
                 "baseline_stddev", "z_score", "mode", "sample_count"]
_RECENT_MAX = 150.0
_FLAT_MEAN, _FLAT_STDDEV = 30.0, 10.0


def _anomaly_row(baselines, metric_type, recent_max=_RECENT_MAX):
    """Model the CASE in detect_anomalies: a seasonal baseline row for the
    CURRENT hour-of-week with iqr > 0 wins, else the flat 7-day fallback."""
    b = next((r for r in baselines
              if r["metric_type"] == metric_type and r["hour_of_week"] == _HOW), None)
    if b and b["iqr"] > 0:
        return {"metric_type": metric_type, "recent_max": recent_max, "recent_avg": 60.0,
                "baseline_mean": b["median"], "baseline_stddev": b["iqr"],
                "z_score": (recent_max - b["median"]) / b["iqr"],
                "mode": "seasonal", "sample_count": b["sample_count"]}
    return {"metric_type": metric_type, "recent_max": recent_max, "recent_avg": 60.0,
            "baseline_mean": _FLAT_MEAN, "baseline_stddev": _FLAT_STDDEV,
            "z_score": (recent_max - _FLAT_MEAN) / _FLAT_STDDEV,
            "mode": "flat", "sample_count": None}


def _cache_returning(*rows):
    from mcp_servers.shared.models import QueryResult
    cache = MagicMock()
    cache.execute.return_value = QueryResult(
        columns=_ANOMALY_COLS, rows=list(rows), row_count=len(rows))
    return cache


@pytest.mark.parametrize("family,_resource,_own,metric_type", _FAMILIES)
def test_trained_family_detects_anomalies_in_seasonal_mode(family, _resource, _own, metric_type):
    """End of the chain: the trainer now runs for this family, so its metric has
    a baseline bucket, and the agent's anomaly tool reports the high-confidence
    seasonal mode. Untrained (the pre-E1-4 state) it reports flat."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl

    fake, _ = _run_trainer(metric_type, cluster_id=family)
    trained = _anomaly_row(fake.trained, metric_type)
    untrained = _anomaly_row([], metric_type)

    hot = detect_anomalies_impl(_cache_returning(trained), family, hours=4, threshold=2.0)
    assert hot["baseline_mode"] == "seasonal"
    assert len(hot["anomalies"]) == 1                    # the guards did not mute it
    assert hot["anomalies"][0]["sample_count"] == len(_CLUSTER_VALUES)
    assert hot["anomalies"][0]["z_score"] == pytest.approx(
        (_RECENT_MAX - _CLUSTER_MEDIAN) / _CLUSTER_IQR)

    cold = detect_anomalies_impl(_cache_returning(untrained), family, hours=4, threshold=2.0)
    assert cold["baseline_mode"] == "flat"


def test_thin_bucket_is_not_a_high_confidence_seasonal_anomaly():
    """The reviewer's chain, end to end: 3 samples of 20.0/20.1/20.2 then a 21.0
    reading used to report mode='seasonal', baseline_mode='seasonal', z=9.0 at
    threshold 2.0. With no bucket trained and only 3 samples of history the flat
    fallback has nothing either (it needs COUNT(*) > 50), so the tool reports no
    anomaly at all instead of a confident wrong one."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl

    fake, _ = _run_trainer("memory_usage_pct", values=_THIN)
    assert fake.trained == []
    out = detect_anomalies_impl(_cache_returning(), "docdb-1", hours=4, threshold=2.0)
    assert out["anomalies"] == []
    assert out["baseline_mode"] == "none"

    # Discriminating: the pre-fix trainer DID make that reading a seasonal anomaly.
    was = _model_training(_pre_fix(fake.insert_sql), _rows_for("memory_usage_pct", _THIN))
    before = detect_anomalies_impl(
        _cache_returning(_anomaly_row(was, "memory_usage_pct", recent_max=_SPIKE)),
        "docdb-1", hours=4, threshold=2.0)
    assert before["baseline_mode"] == "seasonal"
    assert before["anomalies"][0]["z_score"] == pytest.approx(9.0, abs=1e-6)


def test_flat_but_well_sampled_bucket_needs_a_real_move():
    """The other half of the guard: 12 near-identical samples DO train, so the
    relative IQR floor is what stops a 0.9 move (4.5% of the median) scoring
    z=9.0. A 25.0 reading on the same baseline still clears the threshold, so the
    feature is floored, not disabled."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl

    fake, _ = _run_trainer("memory_usage_pct", values=_THIN * 4)
    quiet = detect_anomalies_impl(
        _cache_returning(_anomaly_row(fake.trained, "memory_usage_pct", recent_max=_SPIKE)),
        "docdb-1", hours=4, threshold=2.0)
    assert quiet["baseline_mode"] == "seasonal"      # trained, just not anomalous
    assert quiet["anomalies"] == []

    loud = detect_anomalies_impl(
        _cache_returning(_anomaly_row(fake.trained, "memory_usage_pct", recent_max=25.0)),
        "docdb-1", hours=4, threshold=2.0)
    assert loud["anomalies"][0]["z_score"] == pytest.approx(4.876, abs=0.001)

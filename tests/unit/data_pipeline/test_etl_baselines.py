"""Seasonal baseline training for ALL five engine families (E1-4), the two
guards that keep a thin bucket from producing a confident-looking anomaly, and
the cap that keeps the second guard from muting a real one.

`collect_pg_baselines` reads only `metric_snapshots` and writes
`metric_baselines`, so it is engine-agnostic. It used to be called from the
relational and rds_instance branches only, and the other three branches
`return result` early, so DocumentDB / DynamoDB / ElastiCache anomaly detection
was permanently stuck on the low-confidence flat mean/stddev fallback.

Four layers here:

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
     median=20.1 / IQR=0.0999 and score a 21.0 reading at z=9.0. And the floors
     must not run the other way: a 99.5% cache hit ratio collapsing to 95% has to
     stay an anomaly at both default thresholds (agent 2.0, dashboard 2.5).
  4. Boundary, added after a review found the docstring naming the wrong one: the
     real IQR survives only from 5% of |median| upward (NOT 0.5%, which is merely
     where the 10x cap stops binding), and the band the cap actually rescues for
     that cache-hit collapse ends at a real IQR of 0.225. Both swept on real
     PostgreSQL 14.18, and the disclosure text is pinned so a false sentence
     cannot come back silently.
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


_REL_RE = r"(ABS\(PERCENTILE_CONT\(0\.5\) WITHIN GROUP \(ORDER BY value\)\) \* )([0-9.]+)"
_CAP_RE = r"\)\) \* ([0-9.]+) \)"      # the LEAST cap on the relative floor


def _gates(sql):
    """Read the trainer's guards back OUT of its SQL, so a loosened gate changes
    what this model trains instead of silently passing: the sample floor, the
    relative IQR floor, the cap on how far that floor may inflate the observed
    spread, and the absolute IQR floor."""
    flat = " ".join(sql.split())
    having = re.search(r"HAVING COUNT\(\*\) >= (\d+)", flat)
    assert having, f"sample gate missing from the trainer SQL: {flat}"
    rel = re.search(_REL_RE, flat)
    cap = re.search(_CAP_RE, flat)
    assert cap, f"the relative IQR floor is uncapped in the trainer SQL: {flat}"
    absolute = re.search(r", ([0-9.]+) \) AS iqr", flat)
    assert absolute, f"absolute IQR floor missing from the trainer SQL: {flat}"
    return (int(having.group(1)), float(rel.group(2)) if rel else 0.0,
            float(cap.group(1)), float(absolute.group(1)))


def _model_training(sql, rows):
    """Model the parts of the trainer's INSERT..SELECT that decide what is
    learned, driven by the SQL the collector ACTUALLY issued."""
    flat = " ".join(sql.split())
    sample_floor, rel_floor, cap, abs_floor = _gates(flat)
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
        raw_iqr = q[2] - q[0]                   # PERCENTILE_CONT(0.75) - (0.25)
        out.append({"metric_type": metric_type, "hour_of_week": how, "median": median,
                    "iqr": max(raw_iqr,
                               min(abs(median) * rel_floor, raw_iqr * cap),
                               abs_floor),
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
# 2b. The two guards on a thin bucket, and the cap that keeps the second one
#     from silencing a high-median tight-spread metric
# ===========================================================================

_THIN = [20.0, 20.1, 20.2]      # the reviewer's reproduction, on real PostgreSQL
_SPIKE = 21.0                   # 0.9 above the thin median of 20.1

# A healthy Aurora BufferCacheHitRatio hour: 12 samples, high median, genuinely
# tight spread. PERCENTILE_CONT gives median 99.50 and P75-P25 = 0.125 (verified
# on PostgreSQL 14.18), i.e. a real spread far under 5% of the median.
_CACHE_HIT = [99.3, 99.35, 99.4, 99.45, 99.45, 99.5,
              99.5, 99.55, 99.55, 99.6, 99.7, 99.8]
_CACHE_HIT_MEDIAN = 99.5
_CACHE_HIT_IQR = 0.125
# docdb_findings.CACHE_HIT_WARNING_PCT: the product itself calls this a warning.
_CACHE_COLLAPSE = 95.0
_AGENT_THRESHOLD, _DASHBOARD_THRESHOLD = 2.0, 2.5    # detect_anomalies / dashboard


def _pre_fix(sql):
    """The same SQL with both guards removed: the >= 3 sample gate and the
    absolute-only IQR floor this trainer shipped with."""
    old = re.sub(r"HAVING COUNT\(\*\) >= \d+", "HAVING COUNT(*) >= 3", " ".join(sql.split()))
    return re.sub(_REL_RE, r"\g<1>0.0", old)


def _uncapped(sql):
    """The same SQL with the relative floor's LEAST cap effectively lifted: the
    one commit where `ABS(median) * 0.05` won outright over the observed spread."""
    return re.sub(_CAP_RE, ")) * 1000000000 )", " ".join(sql.split()))


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
    powerless at this median, and here the 10x cap is not the binding constraint
    (10 * the observed 0.2 spread is 2.0, above the relative floor)."""
    fake, _ = _run_trainer("memory_usage_pct", values=_THIN * 4)
    row = fake.trained[0]
    assert row["sample_count"] == 12
    assert row["median"] == pytest.approx(20.1)
    assert row["iqr"] == pytest.approx(1.005)                    # raw IQR is 0.2
    assert (_SPIKE - row["median"]) / row["iqr"] == pytest.approx(0.896, abs=0.001)

    was = _model_training(_pre_fix(fake.insert_sql), _rows_for("memory_usage_pct", _THIN * 4))
    assert (_SPIKE - was[0]["median"]) / was[0]["iqr"] == pytest.approx(4.5, abs=0.01)


def test_relative_floor_cannot_inflate_a_real_iqr_without_bound():
    """The class the unbounded floor silenced: a high median with a genuinely
    tight spread. 12 healthy BufferCacheHitRatio samples (median 99.50, real IQR
    0.125) floored at ABS(median) * 0.05 = 4.975, so a collapse to 95% scored
    z=-0.905, under BOTH default thresholds, while docdb_findings calls 95% a
    warning. Capped at 10x the observed spread the floor is 1.250.

    The cap does NOT move the survival boundary: a real IQR still has to reach 5%
    of |median| to be kept, which is what
    test_real_iqr_survives_only_at_five_percent_of_the_median pins."""
    fake, _ = _run_trainer("buffer_cache_hit", values=_CACHE_HIT)
    row = fake.trained[0]
    assert row["sample_count"] == 12               # a full hour, not a cold start
    assert row["median"] == pytest.approx(_CACHE_HIT_MEDIAN)
    assert row["iqr"] == pytest.approx(_CACHE_HIT_IQR * _gates(fake.insert_sql)[2])
    assert row["iqr"] == pytest.approx(1.25)
    for observed, z in ((95.0, -3.6), (92.0, -6.0), (90.0, -7.6)):
        assert (observed - row["median"]) / row["iqr"] == pytest.approx(z, abs=0.001)

    # Discriminating: uncapped, the same fixture floors at 5% of the median and
    # every one of those drops falls under both thresholds.
    was = _model_training(_uncapped(fake.insert_sql), _rows_for("buffer_cache_hit", _CACHE_HIT))
    assert was[0]["iqr"] == pytest.approx(4.975)
    for observed, z in ((95.0, -0.905), (92.0, -1.508), (90.0, -1.910)):
        assert (observed - was[0]["median"]) / was[0]["iqr"] == pytest.approx(z, abs=0.001)

    # A bucket with real spread is untouched by either floor, capped or not.
    real = _run_trainer("cpu_utilization")[0].trained[0]
    assert real["iqr"] == pytest.approx(_CLUSTER_IQR)


def _bucket_at(median, target_iqr):
    """12 samples whose PERCENTILE_CONT median is `median` and whose P75-P25 is
    exactly `target_iqr`. N=12 puts the 0.25 interpolation at index 2.75 and the
    0.75 at 8.25, so pinning v[2]==v[3]==P25 and v[8]==v[9]==P75 makes both exact,
    and every filler offset is a multiple of the half-spread so the series stays
    sorted at any scale. Verified against real PostgreSQL 14.18 PERCENTILE_CONT
    for every fixture used below; each test also asserts the raw IQR it produced,
    so a drift here cannot pass silently."""
    h = target_iqr / 2.0
    lo, hi = median - h, median + h
    return [lo - 3 * h, lo - h, lo, lo, median - h / 2, median, median,
            median + h / 2, hi, hi, hi + h, hi + 3 * h]


# Swept on real PostgreSQL 14.18 through this trainer's own SQL: median 99.50,
# 12 samples, the real IQR walked across BOTH candidate boundaries. 0.5% of 99.5
# is 0.4975 and 5% is 4.975. The real IQR survives only from 5% upward; 0.5% is
# merely where the 10x cap stops binding, which is the claim 180760f got wrong.
_BOUNDARY_SWEEP = [
    (0.125,  1.250, False),
    (0.400,  4.000, False),
    (0.4975, 4.975, False),
    (0.600,  4.975, False),     # above 0.5% of the median and still NOT kept
    (1.000,  4.975, False),
    (4.900,  4.975, False),
    (4.975,  4.975, True),      # the actual survival boundary: 5% of |median|
    (5.500,  5.500, True),
]


@pytest.mark.parametrize("real_iqr,trained_iqr,kept", _BOUNDARY_SWEEP)
def test_real_iqr_survives_only_at_five_percent_of_the_median(real_iqr, trained_iqr, kept):
    """The boundary nothing tested before. A bucket keeps its real IQR exactly
    when that IQR reaches MIN_IQR_MEDIAN_FRACTION of |median| (5%), NOT 0.5%: at
    and above 0.5% the LEAST resolves to the flat 5% of |median|, so the GREATEST
    keeps returning 4.975 for every real IQR from 0.4975 up to 4.975."""
    fake, _ = _run_trainer("buffer_cache_hit", values=_bucket_at(99.5, real_iqr))
    row = fake.trained[0]
    assert row["median"] == pytest.approx(99.5)
    q = statistics.quantiles(_bucket_at(99.5, real_iqr), n=4, method="inclusive")
    assert q[2] - q[0] == pytest.approx(real_iqr)          # the fixture is honest
    assert row["iqr"] == pytest.approx(trained_iqr)
    assert (row["iqr"] == pytest.approx(real_iqr)) is kept


# Measured on real PostgreSQL 14.18, median 20.1, 12 samples: the smallest move
# above the median that reaches the agent's default threshold of 2.0, as a
# fraction of the median. The "flat metric needs a >= 10% move" line 1da5f86 wrote
# holds only where the relative floor is the binding branch; under 0.5% of the
# median the cap lowers the floor, so a tighter bucket flags on LESS.
_MOVE_TO_THRESHOLD = [
    (0.0400, 0.400, 0.0398),      # 0.199% of the median: cap binds, 3.98% move
    (0.1005, 1.005, 0.1000),      # 0.500%: the two branches meet, 10.00% move
    (1.0000, 1.005, 0.1000),      # 4.975%: relative floor binds, 10.00% move
]


@pytest.mark.parametrize("real_iqr,trained_iqr,move_fraction", _MOVE_TO_THRESHOLD)
def test_flat_metric_move_needed_depends_on_which_branch_binds(
        real_iqr, trained_iqr, move_fraction):
    """1da5f86's "a flat metric now needs a >= 10% move" is true only at or above
    0.5% of |median|, where the LEAST resolves to the flat 5% of |median|. The cap
    makes a tighter bucket MORE sensitive, not less, which is the whole point of
    it, and the docstring now says so with these measured numbers."""
    fake, _ = _run_trainer("memory_usage_pct", values=_bucket_at(20.1, real_iqr))
    row = fake.trained[0]
    assert row["median"] == pytest.approx(20.1)
    q = statistics.quantiles(_bucket_at(20.1, real_iqr), n=4, method="inclusive")
    assert q[2] - q[0] == pytest.approx(real_iqr)
    assert row["iqr"] == pytest.approx(trained_iqr)
    # smallest move above the median scoring |z| >= 2.0, as a fraction of it
    assert (_AGENT_THRESHOLD * row["iqr"]) / row["median"] == pytest.approx(
        move_fraction, abs=0.0001)


def test_trainer_docstring_states_the_measured_survival_boundary():
    """180760f's message and docstring both claimed the real IQR survives at
    >= 0.5% of |median|. The sweep above disproves it. Pinned as text too, because
    a wrong sentence in a docstring is what shipped twice already."""
    doc = " ".join(sys.modules[_train.__module__].__doc__.split())
    assert "keeps its real IQR exactly when that IQR is >= 5% of |median|" in doc
    assert "keeps its real IQR exactly when that IQR is >= 0.5% of |median|" not in doc
    assert "0.5% is only where the CAP stops binding" in doc


def test_zero_median_counter_ceiling_is_documented_not_silently_fixed():
    """Pinned decision. An all-zero bucket is the HEALTHY shape for a counter like
    deadlocks, and neither guard touches it: both relative branches are 0, so the
    absolute 0.01 floor wins and one event scores z=100 as seasonal. A blanket
    "one event" floor is NOT the fix: read_latency / write_latency are raw
    CloudWatch SECONDS in this same table, so a 1.0 floor would score a 20 ms
    spike on an idle cluster at z=0.02 and hide it forever, the same silencing
    bug the cap above removes. The ceiling is disclosed in the trainer docstring
    instead, and this test fails if either the behaviour or the disclosure moves."""
    fake, _ = _run_trainer("deadlocks", values=[0.0] * 12)
    row = fake.trained[0]
    assert (row["sample_count"], row["median"]) == (12, 0.0)
    assert row["iqr"] == pytest.approx(_gates(fake.insert_sql)[3])    # absolute floor
    assert (1.0 - row["median"]) / row["iqr"] == pytest.approx(100.0)

    doc = sys.modules[_train.__module__].__doc__
    assert "KNOWN CEILING (zero-median counters)" in doc
    assert "SECONDS" in doc         # the reason a 1.0 floor is not the fix


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
    # The claim here is "no baseline was used", not how detect_anomalies spells
    # the empty case: which not-scored mode it reports is that tool's own
    # taxonomy, pinned by tests/unit/mcp_servers/performance/test_detect_anomalies.
    assert out["baseline_mode"] not in ("seasonal", "flat")

    # Discriminating: the pre-fix trainer DID make that reading a seasonal anomaly.
    was = _model_training(_pre_fix(fake.insert_sql), _rows_for("memory_usage_pct", _THIN))
    before = detect_anomalies_impl(
        _cache_returning(_anomaly_row(was, "memory_usage_pct", recent_max=_SPIKE)),
        "docdb-1", hours=4, threshold=2.0)
    assert before["baseline_mode"] == "seasonal"
    assert before["anomalies"][0]["z_score"] == pytest.approx(9.0, abs=1e-6)


def test_cache_hit_collapse_is_flagged_on_both_anomaly_surfaces():
    """The regression, end to end through the real tool: a 99.5% cache hit ratio
    collapsing to 95% must be an anomaly at the agent's default threshold (2.0)
    AND the dashboard's (2.5). Uncapped it was silent on both."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl

    fake, _ = _run_trainer("buffer_cache_hit", values=_CACHE_HIT)
    was = _model_training(_uncapped(fake.insert_sql), _rows_for("buffer_cache_hit", _CACHE_HIT))

    for threshold in (_AGENT_THRESHOLD, _DASHBOARD_THRESHOLD):
        out = detect_anomalies_impl(
            _cache_returning(_anomaly_row(fake.trained, "buffer_cache_hit",
                                          recent_max=_CACHE_COLLAPSE)),
            "docdb-1", hours=4, threshold=threshold)
        assert out["baseline_mode"] == "seasonal"
        assert len(out["anomalies"]) == 1
        assert out["anomalies"][0]["z_score"] == pytest.approx(-3.6, abs=0.001)

        # Discriminating: the uncapped floor muted the identical collapse.
        muted = detect_anomalies_impl(
            _cache_returning(_anomaly_row(was, "buffer_cache_hit",
                                          recent_max=_CACHE_COLLAPSE)),
            "docdb-1", hours=4, threshold=threshold)
        assert muted["baseline_mode"] == "seasonal"
        assert muted["anomalies"] == []


# Measured on real PostgreSQL 14.18 (trainer SQL) + the shipped
# detect_anomalies_impl scoring, for the metric the review was filed on:
# BufferCacheHitRatio, median 99.50, collapsing to 95.0, a drop of 4.5.
# Flagging at the agent's 2.0 needs a trained IQR <= 2.25, and the trained IQR is
# 10x the observed spread only while that spread is under 0.4975, so the band the
# cap actually rescues ends at a real IQR of 0.225 (0.226% of the median).
_RESCUE_BAND = [
    (0.125, 1.250, -3.600, True,  True),      # the reported case, inside the band
    (0.225, 2.250, -2.000, True,  False),     # last real IQR the agent still flags
    (0.226, 2.260, -1.991, False, False),     # 1/1000th over, and it goes silent
    (0.400, 4.000, -1.125, False, False),
    (3.000, 4.975, -0.905, False, False),     # cap no longer binds: flat 5% floor
]


@pytest.mark.parametrize("real_iqr,trained_iqr,z,flag20,flag25", _RESCUE_BAND)
def test_capped_floor_rescue_band_ends_around_two_tenths_of_a_percent(
        real_iqr, trained_iqr, z, flag20, flag25):
    """How narrow the cap's rescue really is. The 0.125 bucket the review reported
    is genuinely fixed, but a healthy cache-hit hour with slightly more real
    jitter is still muted by the relative floor: between roughly 0.2% and the 5%
    survival boundary the SAME 4.5-point collapse scores under both thresholds.
    Disclosed in the trainer docstring rather than fixed, because widening the cap
    re-opens the median-20.1 false positive."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl

    fake, _ = _run_trainer("buffer_cache_hit", values=_bucket_at(99.5, real_iqr))
    row = fake.trained[0]
    assert row["iqr"] == pytest.approx(trained_iqr)

    scored = _anomaly_row(fake.trained, "buffer_cache_hit", recent_max=_CACHE_COLLAPSE)
    assert scored["z_score"] == pytest.approx(z, abs=0.001)
    for threshold, flagged in ((_AGENT_THRESHOLD, flag20), (_DASHBOARD_THRESHOLD, flag25)):
        out = detect_anomalies_impl(
            _cache_returning(scored), "docdb-1", hours=4, threshold=threshold)
        assert out["baseline_mode"] == "seasonal"        # trained either way
        assert bool(out["anomalies"]) is flagged


def test_trainer_docstring_discloses_the_remaining_silence_zone():
    """Honest-disclosure decision, pinned like the zero-median ceiling above: the
    band is named in the docstring, so the next reader does not have to rediscover
    that 0.226% of |median| is where the rescue stops."""
    doc = " ".join(sys.modules[_train.__module__].__doc__.split())
    assert "KNOWN CEILING (how narrow the cap's rescue really is)" in doc
    assert "the rescued band is a real IQR up to 0.225, about 0.2% of |median|" in doc
    # and it must say what happens in the zone above the band, not just below it
    assert "Between roughly 0.2% and the 5% survival boundary" in doc


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

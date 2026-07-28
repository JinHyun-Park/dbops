import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from mcp_servers.performance.tools.forecast_capacity import (
    _ACTIONABLE_HORIZON_DAYS,
    _VALID_METRICS,
    forecast_capacity_impl,
)
from mcp_servers.shared.models import QueryResult

_GIB = 1024 ** 3
_VOLUME_MAX_BYTES = 128 * 1024 ** 4


_DIM_FILTER = "AND (dimensions IS NULL OR dimensions::text = '{}')"


def _cache(slope, current, r2=0.9, n=200, max_connections=None, instance_class=None,
           engine="aurora-postgresql", allocated_storage_gb=None, meta_rows=None,
           serverlessv2_max_acu=None, settings_max_connections=None,
           docdb_connections_limit=None, provisioned=None, evictions=None):
    """cluster_meta, optionally a limit-fallback lookup, then the metric trend.
    side_effect routes each by which table/metric the caller asked for. `seen`
    records the metric_type the trend query was actually parameterized with, the
    exact SQL string the cache received (so the dimension filter can be asserted
    on the EXECUTED text, not on a constant), and which fallback lookup ran."""
    metric_qr = QueryResult(
        columns=["slope_per_day", "r2", "n", "current_value"],
        rows=[{"slope_per_day": slope, "r2": r2, "n": n, "current_value": current}],
        row_count=1,
    )
    rows = [{"engine": engine, "max_connections": max_connections,
             "instance_class": instance_class,
             "serverlessv2_max_acu": serverlessv2_max_acu,
             "allocated_storage_gb": allocated_storage_gb}] if meta_rows is None else meta_rows
    meta_qr = QueryResult(
        columns=["engine", "max_connections", "instance_class", "serverlessv2_max_acu",
                 "allocated_storage_gb"],
        rows=rows,
        row_count=len(rows),
    )

    def _single(value):
        return QueryResult(columns=["value"],
                           rows=[] if value is None else [{"value": value}],
                           row_count=0 if value is None else 1)

    cache = MagicMock()
    cache.seen = {}

    def _exec(sql, params=None):
        if "cluster_meta" in sql:
            return meta_qr
        if "cluster_settings" in sql:
            cache.seen["settings_sql"] = sql
            return _single(settings_max_connections)
        if "db_connections_limit" in sql:
            cache.seen["docdb_limit_sql"] = sql
            return _single(docdb_connections_limit)
        if (params or {}).get("provisioned_metric"):
            cache.seen["provisioned_sql"] = sql
            cache.seen["provisioned_metric"] = params["provisioned_metric"]
            return _single(provisioned)
        if "'evictions'" in sql:
            cache.seen["evictions_sql"] = sql
            return _single(evictions)
        cache.seen["metric_type"] = (params or {}).get("metric")
        cache.seen["sql"] = sql
        return metric_qr

    cache.execute.side_effect = _exec
    return cache


# ===== the dimension filter: cluster-level rows ONLY =====


def test_trend_sql_filters_out_per_instance_and_wait_event_rows():
    """metric_snapshots holds the SAME metric_type at several dimensionalities:
    cw_collector writes db_connections cluster-level (dimensions='{}') AND
    per-instance (dimensions={instance,role}), and pi_collector splits aas by
    db.wait_event plus a '{}' total. Without the filter REGR_SLOPE regresses
    cluster totals mixed with per-instance fractions and current_value (latest
    row) can be an instance row. The filter must be on BOTH the aggregate WHERE
    and the current_value subselect, and must be the STRICT form: the
    jsonb_exists(dimensions,'instance') form would let the aas wait-event rows
    through (they carry no 'instance' key).

    Every metric of every family, because the per-GSI DynamoDB rows are the
    sharpest case: dynamodb_cw_collector.py:160-167 writes consumed_rcu/wcu under
    the SAME metric_type as the table-level row with dimensions={"gsi": name}, so
    the weak filter would regress the table total plus each index summed."""
    for metric, engine in (("storage", "aurora-postgresql"),
                           ("connections", "aurora-postgresql"),
                           ("aas", "aurora-postgresql"),
                           ("storage", "mysql"),
                           ("read_capacity", "dynamodb"),
                           ("write_capacity", "dynamodb"),
                           ("memory", "redis")):
        cache = _cache(slope=1.0, current=1.0, max_connections=1000,
                       instance_class="db.r6g.large", engine=engine,
                       provisioned=100.0, evictions=0.0)
        forecast_capacity_impl(cache, cluster_id="c", metric=metric)
        sql = cache.seen["sql"]
        assert sql.count(_DIM_FILTER) == 2, (metric, engine, sql)
        assert "jsonb_exists" not in sql, (metric, engine)
    # the per-family ceiling / eviction lookups are cluster-level scalars too
    cache = _cache(slope=1.0, current=1.0, engine="dynamodb", provisioned=100.0)
    forecast_capacity_impl(cache, cluster_id="c", metric="read_capacity")
    assert _DIM_FILTER in cache.seen["provisioned_sql"]
    cache = _cache(slope=1.0, current=1.0, engine="redis", evictions=5.0)
    forecast_capacity_impl(cache, cluster_id="c", metric="memory")
    assert _DIM_FILTER in cache.seen["evictions_sql"]


# ===== fail-closed: no cluster_meta row =====


def test_unregistered_cluster_refuses_before_running_the_trend_query():
    """No cluster_meta row means the engine is unknown; engine_family() defaults
    to relational by legacy convention, so continuing would report storage_bytes
    vs a 128 TiB ceiling with 0 samples as a flat, safe-looking trend. Refuse
    instead, and never touch metric_snapshots."""
    cache = _cache(slope=0.0, current=0.0, meta_rows=[])
    result = forecast_capacity_impl(cache, cluster_id="ghost", metric="storage")
    assert result["status"] == "unknown_cluster"
    assert result["samples"] == 0
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False
    assert result["grounded"] is False
    assert "수집" in result["reason"]
    assert cache.seen == {}  # trend query never ran
    assert cache.execute.call_count == 1


# ===== storage: the GROWING shape (Aurora / DocumentDB storage_bytes) =====


def test_aurora_storage_reads_storage_bytes_and_volume_ceiling():
    """The old default metric `storage_gb` is written by NO collector, so the
    default path returned zero samples. Aurora must read `storage_bytes` and
    compare it against the real 128 TiB volume ceiling (in bytes)."""
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB)
    result = forecast_capacity_impl(cache, cluster_id="c")  # default metric
    assert cache.seen["metric_type"] == "storage_bytes"
    assert result["metric"] == "storage"
    assert result["limit"] == _VOLUME_MAX_BYTES
    assert result["forecast"] == "growing"
    assert result["days_until_limit"] > 0
    assert "128 TiB" in result["limit_basis"]


def test_documentdb_storage_uses_the_same_growing_series():
    cache = _cache(slope=1.0 * _GIB, current=50.0 * _GIB, engine="docdb")
    result = forecast_capacity_impl(cache, cluster_id="docdb-1", metric="storage")
    assert result["engine_family"] == "documentdb"
    assert cache.seen["metric_type"] == "storage_bytes"
    assert result["days_until_limit"] > 0


def test_aurora_storage_shrinking_claims_no_date():
    """A DECREASING volume is moving away from the ceiling, so no ETA. The old
    -1 sentinel is gone: an agent renders it verbatim as "-1일 후 한계 도달"."""
    cache = _cache(slope=-1.0 * _GIB, current=100.0 * _GIB)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False
    assert result["days_until_limit_range"] is None
    assert result["confidence"] == "low"
    assert "향하지 않아" in result["note"]


# ===== storage: the SHRINKING shape (standalone RDS free_storage_bytes) =====


def test_rds_instance_storage_forecasts_exhaustion_from_free_space():
    """rds_instance collects FreeStorageSpace (free_storage_bytes), which SHRINKS
    toward 0. 20 GiB free losing 2 GiB/day → ~10 days to STORAGE_FULL."""
    cache = _cache(slope=-2.0 * _GIB, current=20.0 * _GIB,
                   engine="mysql", allocated_storage_gb="100")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
    assert result["engine_family"] == "rds_instance"
    assert cache.seen["metric_type"] == "free_storage_bytes"
    assert result["limit"] == 0.0
    # 'shrinking' would label the most alarming case with the most reassuring word.
    assert result["forecast"] == "depleting"
    assert result["days_until_limit"] == 10
    assert result["approaching_limit"] is True
    assert result["grounded"] is True
    # allocated_storage_gb from cluster_meta.resource_details gives usage context.
    assert result["allocated_gb"] == 100.0
    assert result["usage_pct"] == 80.0


def test_rds_instance_free_space_growing_claims_no_date():
    """Free space GROWING means the disk is emptying, not filling, so no ETA."""
    cache = _cache(slope=1.0 * _GIB, current=20.0 * _GIB, engine="sqlserver-se")
    result = forecast_capacity_impl(cache, cluster_id="rds-mssql-1", metric="storage")
    assert cache.seen["metric_type"] == "free_storage_bytes"
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False
    assert result["days_until_limit_range"] is None


def test_rds_instance_without_allocated_storage_still_forecasts_exhaustion():
    """The 0-byte floor is a hard fact, so a missing allocated_storage_gb only
    drops the usage_pct context, the exhaustion ETA stays valid."""
    cache = _cache(slope=-1.0 * _GIB, current=5.0 * _GIB, engine="mysql")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-2", metric="storage")
    assert result["days_until_limit"] == 5
    assert "allocated_gb" not in result
    # usage_pct is on every payload so a consumer never has to divide by `limit`
    # (0 here), but the depleting mode has no denominator without allocated size,
    # so the honest value is null, not a fabricated percentage.
    assert result["usage_pct"] is None
    assert result["direction"] == "down"


def test_storage_unsupported_engine_refuses_instead_of_zero_forecast():
    """DynamoDB/ElastiCache have no storage series at all, so say so instead of
    reporting a flat 0-sample trend."""
    for engine in ("dynamodb", "redis"):
        cache = _cache(slope=0.0, current=0.0, engine=engine)
        result = forecast_capacity_impl(cache, cluster_id="x", metric="storage")
        assert result["status"] == "unsupported_metric"
        assert result["days_until_limit"] is None
        assert result["samples"] == 0
        # this refusal blames the ENGINE, unlike unknown_metric above
        assert result["engine_family"] in result["reason"]
        assert cache.seen == {}  # trend query never ran


def test_unknown_metric_name_is_distinct_from_an_engine_refusal():
    """The old documented metric name `storage_gb` used to come back as
    `unsupported_metric` with a reason blaming the ENGINE, indistinguishable from
    a genuine DynamoDB refusal. A bad NAME gets its own status and lists the
    valid values, and never runs a query."""
    cache = _cache(slope=1.0, current=1.0)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage_gb")
    assert result["status"] == "unknown_metric"
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False
    for valid in ("storage", "connections", "aas"):
        assert valid in result["reason"]
    cache.execute.assert_not_called()


# ===== connections / aas =====


def test_connections_limit_from_cluster_meta_and_canonical_series():
    cache = _cache(slope=1.0, current=100.0, max_connections=2000)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["limit"] == 2000  # cluster's real max_connections, not 5000
    assert "max_connections" in result["limit_basis"]
    # db_connections (CloudWatch) is collected for every engine; the PI-only
    # `connections` series is empty whenever Performance Insights is off.
    assert cache.seen["metric_type"] == "db_connections"


def test_aas_limit_from_instance_vcpu():
    cache = _cache(slope=0.1, current=2.0, instance_class="db.r6g.4xlarge")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 16  # 4xlarge = 16 vCPU, not 64
    assert "vCPU=16" in result["limit_basis"]
    assert cache.seen["metric_type"] == "aas"


def test_ungrounded_limit_claims_no_date():
    """Serverless (vCPU unknown) → the limit is an assumption, so no ETA is
    asserted: days_until_limit is None, not a number."""
    cache = _cache(slope=0.1, current=2.0, r2=0.9, n=200, instance_class="db.serverless")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 64  # fallback, flagged
    assert result["grounded"] is False
    assert result["days_until_limit"] is None
    assert result["days_until_limit_range"] is None
    assert result["confidence"] == "low"
    assert "단정하지 않습니다" in result["note"]


def test_ungrounded_connections_claims_no_date():
    cache = _cache(slope=5.0, current=100.0)  # no max_connections in cluster_meta
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["grounded"] is False
    assert result["days_until_limit"] is None


# ===== confidence banding =====


def test_confidence_high_with_good_fit_and_grounded_limit():
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB, r2=0.85, n=300)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["confidence"] == "high"
    assert result["days_until_limit_range"] is not None
    lo, hi = result["days_until_limit_range"]
    assert lo <= result["days_until_limit"]


# ===== the gateway contract the agent actually sees =====


def test_gateway_schema_carries_the_metric_enum():
    """The agent only sees cdk/tool_definitions.py (mcp-servers/schemas/*.json is
    documentation read by nothing). Without the enum there it keeps sending the
    old documented metric='storage_gb'. The description is also the only place the
    agent learns the payload contract, so the statuses it must branch on and the
    fallback sources must be named there."""
    repo = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location(
        "dbops_tool_definitions", repo / "cdk" / "tool_definitions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tool = {t["name"]: t for t in mod.performance_schema()}["forecast_capacity"]
    assert tool["inputSchema"]["properties"]["metric"]["enum"] == list(_VALID_METRICS)
    assert "storage_gb" in tool["description"]  # explicitly called out as gone
    for contract in ("limit_reached", "no_data", "unsupported_metric",
                     "cluster_settings", "serverlessv2_max_acu"):
        assert contract in tool["description"], contract


def test_low_fit_is_low_confidence():
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB, r2=0.1, n=300)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["confidence"] == "low"


# ===== actionable horizon: a far-future ETA is not "approaching" =====


def test_far_future_eta_reports_the_date_but_is_not_flagged_approaching():
    """Live regression (dbops-demo-mysql, 2026-07-24): free space was declining a
    few MB/day, so the ETA came out at 80170 days (about 219 years) and
    approaching_limit was still true. A DBA reads that boolean and investigates.

    The ETA itself is honest data and stays in the payload; only the flag is
    bounded to an actionable horizon."""
    # 20 GiB free, losing 1 MiB/day -> ~20480 days, far beyond a year.
    cache = _cache(slope=-1.0 * 1024 ** 2, current=20.0 * _GIB,
                   engine="mysql", allocated_storage_gb="20")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
    assert result["days_until_limit"] > _ACTIONABLE_HORIZON_DAYS
    assert result["approaching_limit"] is False
    # the trend direction is still reported truthfully
    assert result["forecast"] == "depleting"
    # and the note must explain WHY the flag is false, so the number is not read
    # as a contradiction
    assert "실행 가능 기간" in result["note"]
    assert str(result["days_until_limit"]) in result["note"]


def test_eta_inside_the_horizon_is_still_flagged():
    """Control: the bound must not silence a genuinely urgent forecast."""
    cache = _cache(slope=-2.0 * _GIB, current=20.0 * _GIB,
                   engine="mysql", allocated_storage_gb="100")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
    assert result["days_until_limit"] == 10
    assert result["approaching_limit"] is True


def test_confidence_reflects_fit_not_the_horizon():
    """confidence describes how well the line fits, so a far-future ETA with a
    good fit is a confident far-future estimate, not a low-confidence one. This
    decoupling is why the horizon bound could not simply reuse `approaching`."""
    cache = _cache(slope=-1.0 * 1024 ** 2, current=20.0 * _GIB,
                   engine="mysql", allocated_storage_gb="20", r2=0.95, n=500)
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
    assert result["approaching_limit"] is False
    assert result["confidence"] == "high"
    # the uncertainty band belongs to the estimate, so it is still present
    assert result["days_until_limit_range"] is not None


# ===== ALREADY AT the limit is not the same as moving AWAY from it =====
# _days() returned None for every gap <= 0, so a cluster pinned at its ceiling
# got the identical payload to one trending away: no date, approaching_limit
# false, confidence low, and a note saying the trend is not heading to the limit.
# The calmest possible answer for the most urgent state.


def test_connections_at_the_ceiling_is_limit_reached_not_a_calm_null():
    cache = _cache(slope=0.5, current=2000.0, max_connections=2000)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["status"] == "limit_reached"
    assert result["days_until_limit"] == 0
    assert result["approaching_limit"] is True
    # observed, not extrapolated: the fit does not get to downgrade a fact
    assert result["confidence"] == "high"
    assert "이미 한계" in result["note"]
    assert "향하지 않아" not in result["note"]


def test_connections_past_the_ceiling_stays_urgent_even_while_recovering():
    """Over the ceiling with a FALLING trend: the trend is 'moving away', but the
    cluster is refusing connections right now."""
    cache = _cache(slope=-3.0, current=2100.0, max_connections=2000)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["status"] == "limit_reached"
    assert result["approaching_limit"] is True
    assert result["days_until_limit"] == 0


def test_free_storage_at_zero_is_limit_reached_not_a_null_forecast():
    """rds_instance with 0 free bytes IS storage-full. The limit for
    free_storage_bytes is 0, so the gap is 0 and the old code called that 'not
    heading to the limit'."""
    cache = _cache(slope=-1.0 * _GIB, current=0.0, engine="mysql",
                   allocated_storage_gb="100")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
    assert result["metric_type"] == "free_storage_bytes"
    assert result["status"] == "limit_reached"
    assert result["days_until_limit"] == 0
    assert result["approaching_limit"] is True
    assert "STORAGE_FULL" in result["note"]
    assert result["usage_pct"] == 100.0


def test_aurora_storage_at_the_volume_ceiling_is_limit_reached():
    cache = _cache(slope=1.0 * _GIB, current=float(_VOLUME_MAX_BYTES))
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["metric_type"] == "storage_bytes"
    assert result["status"] == "limit_reached"
    assert result["days_until_limit"] == 0
    assert result["approaching_limit"] is True


def test_zero_samples_never_reads_as_at_the_limit():
    """The at-limit test must require real samples. free_storage_bytes has a limit
    of 0 and current_value defaults to 0.0 when there is no row at all, so an
    uncollected cluster would otherwise be declared STORAGE_FULL. Both no-row
    shapes (NULL current, and 0.0 with zero samples) must land on no_data."""
    for current in (None, 0.0):
        cache = _cache(slope=0.0, current=current, n=0, engine="mysql")
        result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
        assert result["status"] == "no_data", current
        assert result["approaching_limit"] is False
        assert result["days_until_limit"] is None
        # samples==0 is not a trend, so it is never labelled the reassuring "stable"
        assert result["forecast"] == "no_data"
        assert "표본이 없어" in result["note"]


def test_an_ungrounded_limit_cannot_be_declared_reached():
    """No max_connections anywhere: the limit is a guessed 5000, so 'you are at
    5000' would be a fabricated alarm. Stay ungrounded and dateless."""
    cache = _cache(slope=1.0, current=9000.0)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["grounded"] is False
    assert result["status"] == "ok"
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False


# ===== the limit must be resolvable on a REAL cluster (sibling parity) =====
# cluster_meta.max_connections is written by api/clusters/seeder.py only (the demo
# seeder), meta_collector's INSERT has no such column, and the vCPU ceiling is
# None for instance_class db.serverless. Without these fallbacks 2 of the 3
# advertised metrics never produced a date on a real cluster, while the dashboard
# and the ETL collector both DID answer, so they contradicted each other.


def test_connections_falls_back_to_cluster_settings_like_its_siblings():
    """Same query and precedence as capacity_forecast.py / api/dashboard: latest
    cluster_settings row named max_connections (pg_locks / mysql_locks write it)."""
    cache = _cache(slope=5.0, current=100.0, settings_max_connections="1000")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["grounded"] is True
    assert result["limit"] == 1000.0
    assert "cluster_settings.max_connections" in result["limit_basis"]
    assert result["days_until_limit"] == 180  # (1000 - 100) / 5
    sql = cache.seen["settings_sql"]
    assert "name = 'max_connections'" in sql
    assert "ORDER BY updated_at DESC" in sql


def test_cluster_meta_max_connections_wins_over_cluster_settings():
    cache = _cache(slope=5.0, current=100.0, max_connections=2000,
                   settings_max_connections="1000")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["limit"] == 2000.0
    assert "cluster_meta" in result["limit_basis"]
    assert "settings_sql" not in cache.seen  # no pointless second lookup


def test_documentdb_connections_ceiling_comes_from_the_limit_metric():
    """DocDB has no max_connections setting, so cluster_settings is the wrong
    source. api/dashboard uses the latest DatabaseConnectionsLimit datapoint."""
    cache = _cache(slope=1.0, current=100.0, engine="docdb",
                   docdb_connections_limit=400)
    result = forecast_capacity_impl(cache, cluster_id="docdb-1", metric="connections")
    assert result["grounded"] is True
    assert result["limit"] == 400.0
    assert "DatabaseConnectionsLimit" in result["limit_basis"]
    assert result["days_until_limit"] == 300  # (400 - 100) / 1
    assert "settings_sql" not in cache.seen
    # the ceiling lookup is a cluster-level scalar, so it needs the strict filter
    assert _DIM_FILTER in cache.seen["docdb_limit_sql"]


def test_serverless_v2_aas_ceiling_comes_from_max_acu():
    """instance_class is db.serverless for every Serverless v2 cluster, so the
    vCPU map always missed and aas was ungrounded fleet-wide. meta_collector DOES
    populate serverlessv2_max_acu (the ETL collector's own ACU ceiling); 4 ACU per
    vCPU (db.r6g.large = 2 vCPU / 16 GiB = 8 ACU)."""
    cache = _cache(slope=0.1, current=2.0, instance_class="db.serverless",
                   serverlessv2_max_acu=32)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["grounded"] is True
    assert result["limit"] == 8.0  # 32 ACU / 4
    assert "serverlessv2_max_acu" in result["limit_basis"]
    assert result["days_until_limit"] == 60  # (8 - 2) / 0.1


def test_provisioned_instance_class_outranks_max_acu():
    """A cluster can carry a Serverless v2 scaling config while its instances are
    provisioned; the real instance's vCPU is the better ceiling."""
    cache = _cache(slope=0.1, current=2.0, instance_class="db.r6g.4xlarge",
                   serverlessv2_max_acu=128)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 16
    assert "vCPU=16" in result["limit_basis"]


# ===== connections / aas are per-family too, not family-agnostic =====


def test_connections_is_refused_on_engines_that_never_write_the_series():
    """db_connections comes from cw_collector / docdb_cw_collector /
    rds_instance_cw_collector only. DynamoDB has no connection concept and
    ElastiCache's maxclients ceiling is not collected, so forecasting there
    returned samples=0, forecast='stable' and a fabricated 5000 limit."""
    for engine in ("dynamodb", "redis"):
        cache = _cache(slope=0.0, current=0.0, n=0, engine=engine)
        result = forecast_capacity_impl(cache, cluster_id="x", metric="connections")
        assert result["status"] == "unsupported_metric", engine
        assert result["days_until_limit"] is None
        assert result["samples"] == 0
        assert result["engine_family"] in result["reason"]
        assert cache.seen == {}  # no trend query, no limit lookup


def test_aas_is_refused_where_performance_insights_never_writes_it():
    """`aas` has exactly one writer, pi_collector, so only the PI-capable families
    (relational, rds_instance) have the series. DocumentDB has no PI at all."""
    for engine in ("docdb", "dynamodb", "memcached"):
        cache = _cache(slope=0.0, current=0.0, n=0, engine=engine)
        result = forecast_capacity_impl(cache, cluster_id="x", metric="aas")
        assert result["status"] == "unsupported_metric", engine
        assert result["engine_family"] in result["reason"]
        assert cache.seen == {}


def test_rds_instance_aas_is_supported_via_pi_db_load():
    """PI on standalone RDS collects db.load.avg (the only universally supported
    metric there), so aas IS forecastable for this family."""
    cache = _cache(slope=0.1, current=1.0, engine="mysql",
                   instance_class="db.m5.large")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="aas")
    assert result["engine_family"] == "rds_instance"
    assert cache.seen["metric_type"] == "aas"
    assert result["limit"] == 2  # db.m5.large = 2 vCPU
    assert result["status"] == "ok"


# ===== E1-5: DynamoDB throughput =====


def test_dynamodb_write_capacity_ceiling_is_provisioned_times_sixty():
    """consumed_wcu is a per-MINUTE Sum (dynamodb_cw_collector.py:12,24) while
    provisioned_wcu is a per-SECOND rate (:20), so the ceiling is x60. 1000/min
    now, +100/min per day, ceiling 50/s = 3000/min -> (3000-1000)/100 = 20 days."""
    cache = _cache(slope=100.0, current=1000.0, engine="dynamodb", provisioned=50.0)
    result = forecast_capacity_impl(cache, cluster_id="ddb-1", metric="write_capacity")
    assert result["engine_family"] == "dynamodb"
    assert cache.seen["metric_type"] == "consumed_wcu"
    assert cache.seen["provisioned_metric"] == "provisioned_wcu"
    assert result["limit"] == 3000.0
    assert result["grounded"] is True
    assert result["direction"] == "up"
    assert result["status"] == "ok"
    assert result["days_until_limit"] == 20
    assert result["approaching_limit"] is True
    assert result["usage_pct"] == 33.3           # 1000 / 3000, rounded to 1dp


def test_dynamodb_read_capacity_uses_the_read_pair():
    cache = _cache(slope=10.0, current=100.0, engine="dynamodb", provisioned=20.0)
    result = forecast_capacity_impl(cache, cluster_id="ddb-1", metric="read_capacity")
    assert cache.seen["metric_type"] == "consumed_rcu"
    assert cache.seen["provisioned_metric"] == "provisioned_rcu"
    assert result["limit"] == 1200.0


def test_dynamodb_ondemand_table_reports_the_trend_without_a_date():
    """provisioned_* rows exist only for billing_mode == PROVISIONED
    (dynamodb_cw_collector.py:134), so an on-demand table has no grounded
    ceiling. The consumption trend is still real data: report it, claim no date,
    and never divide by the absent limit."""
    cache = _cache(slope=50.0, current=1000.0, engine="dynamodb", provisioned=None)
    result = forecast_capacity_impl(cache, cluster_id="ddb-2", metric="read_capacity")
    assert result["status"] == "ok"
    assert result["grounded"] is False
    assert result["limit"] == 0.0
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False
    assert result["usage_pct"] is None          # limit 0 -> no percentage, no ZeroDivisionError
    assert result["forecast"] == "growing"       # the measured trend is still reported
    assert "온디맨드" in result["note"]


def test_dynamodb_at_the_provisioned_ceiling_is_limit_reached():
    cache = _cache(slope=1.0, current=3000.0, engine="dynamodb", provisioned=50.0)
    result = forecast_capacity_impl(cache, cluster_id="ddb-1", metric="write_capacity")
    assert result["status"] == "limit_reached"
    assert result["days_until_limit"] == 0
    assert result["approaching_limit"] is True
    assert result["usage_pct"] == 100.0


def test_dynamodb_has_no_storage_connections_or_aas_series():
    for metric in ("storage", "connections", "aas", "memory"):
        cache = _cache(slope=0.0, current=0.0, n=0, engine="dynamodb")
        result = forecast_capacity_impl(cache, cluster_id="ddb-1", metric=metric)
        assert result["status"] == "unsupported_metric", metric
        assert result["days_until_limit"] is None
        assert cache.seen == {}


# ===== E1-5: ElastiCache memory, and the cache that is at capacity BY DESIGN =====


def test_elasticache_memory_forecasts_toward_the_definitional_100_pct_ceiling():
    """A cache with ZERO evictions and a rising memory trend really is filling up
    (the noeviction-policy case, where full means write errors). 60% now, +2%/day,
    ceiling 100 -> 20 days. The 100 ceiling needs no node-type -> memory map
    (there is none in this repo): the metric IS a percentage."""
    cache = _cache(slope=2.0, current=60.0, engine="redis", evictions=0.0)
    result = forecast_capacity_impl(cache, cluster_id="cache-1", metric="memory")
    assert result["engine_family"] == "elasticache"
    assert cache.seen["metric_type"] == "memory_usage_pct"
    assert result["limit"] == 100.0
    assert result["grounded"] is True
    assert result["status"] == "ok"
    assert result["days_until_limit"] == 20
    assert result["approaching_limit"] is True
    assert result["usage_pct"] == 60.0


def test_valkey_uses_the_same_memory_series():
    cache = _cache(slope=1.0, current=50.0, engine="valkey", evictions=0.0)
    result = forecast_capacity_impl(cache, cluster_id="cache-2", metric="memory")
    assert result["engine_family"] == "elasticache"
    assert cache.seen["metric_type"] == "memory_usage_pct"
    assert result["status"] == "ok"


def test_a_healthy_lru_cache_pinned_at_maxmemory_is_not_reported_as_exhausting():
    """An LRU/TTL cache sits at 97% BY DESIGN with a slope of about 0. Reporting
    "days until 100%" there is noise, and reporting it as at/near exhaustion is a
    false alarm on a perfectly healthy cache. Evictions are the accurate signal,
    so their presence switches the answer to status=evicting with NO date, and
    approaching_limit stays false: a cache evicting on policy is not an
    incident."""
    cache = _cache(slope=0.01, current=97.0, engine="redis", evictions=4200.0)
    result = forecast_capacity_impl(cache, cluster_id="cache-1", metric="memory")
    assert result["status"] == "evicting"
    assert result["days_until_limit"] is None
    assert result["approaching_limit"] is False
    assert result["days_until_limit_range"] is None
    # 97% is real and still reported: this is "cannot date the exhaustion", not
    # "nothing to see". The note must send the operator to the eviction findings.
    assert result["usage_pct"] == 97.0
    assert result["current_value"] == 97.0
    assert "eviction" in result["note"]
    assert "elasticache_evictions_spike" in result["note"]
    assert "문제 없음" in result["note"]  # explicitly disclaims the all-clear reading


def test_evicting_outranks_the_at_limit_verdict():
    """At exactly 100% WITH evictions is still the recycling case, not a
    write-stopping wall: the LRU policy is doing its job. Without this ordering
    the calmer at_limit prose would claim immediate action on a healthy cache."""
    cache = _cache(slope=0.0, current=100.0, engine="redis", evictions=10.0)
    result = forecast_capacity_impl(cache, cluster_id="cache-1", metric="memory")
    assert result["status"] == "evicting"
    assert result["days_until_limit"] is None


def test_memory_with_no_samples_is_no_data_not_evicting():
    """Zero memory samples means we cannot say anything, and the eviction probe
    must not be allowed to invent a verdict from another metric's rows."""
    cache = _cache(slope=0.0, current=None, n=0, engine="redis", evictions=999.0)
    result = forecast_capacity_impl(cache, cluster_id="cache-1", metric="memory")
    assert result["status"] == "no_data"
    assert result["forecast"] == "no_data"
    assert "evictions_sql" not in cache.seen


def test_memcached_memory_is_refused_by_engine_not_answered_by_family():
    """DatabaseMemoryUsagePercentage is in the Redis/Valkey list only
    (elasticache_cw_collector.py:12); _MEMCACHED_METRICS has no equivalent, and
    its FreeableMemory is HOST memory, not cache fill, so there is no honest
    substitute. Memcached is in the elasticache family, so the refusal has to key
    off the engine, not the family."""
    cache = _cache(slope=1.0, current=50.0, n=0, engine="memcached")
    result = forecast_capacity_impl(cache, cluster_id="cache-mc", metric="memory")
    assert result["engine_family"] == "elasticache"
    assert result["status"] == "unsupported_metric"
    assert result["days_until_limit"] is None
    assert result["samples"] == 0
    assert "Memcached" in result["reason"]
    assert cache.seen == {}          # no trend query, no eviction probe


def test_elasticache_has_no_storage_connections_or_aas_series():
    for metric in ("storage", "connections", "aas", "read_capacity"):
        cache = _cache(slope=0.0, current=0.0, n=0, engine="redis")
        result = forecast_capacity_impl(cache, cluster_id="cache-1", metric=metric)
        assert result["status"] == "unsupported_metric", metric
        assert cache.seen == {}


# ===== regression pin: the Aurora/relational storage path must not move =====


def test_aurora_storage_contract_is_unchanged_by_the_limit_fallbacks():
    """The limit-fallback and at-limit work must not touch the relational
    storage_bytes path: same series, same 128 TiB ceiling, same ETA math, and
    still exactly two queries (cluster_meta + trend, no fallback lookups)."""
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB, r2=0.85, n=300)
    result = forecast_capacity_impl(cache, cluster_id="aurora-1", metric="storage")
    assert result["metric_type"] == "storage_bytes"
    assert result["limit"] == float(_VOLUME_MAX_BYTES)
    assert result["grounded"] is True
    assert result["status"] == "ok"
    assert result["forecast"] == "growing"
    assert result["confidence"] == "high"
    assert result["days_until_limit"] == int(
        (_VOLUME_MAX_BYTES - 100.0 * _GIB) / (2.0 * _GIB))
    assert result["days_until_limit_range"] is not None
    assert cache.execute.call_count == 2
    assert "settings_sql" not in cache.seen and "docdb_limit_sql" not in cache.seen

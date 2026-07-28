"""E1-5: the agent tool and the REST dashboard endpoint must give the SAME
capacity answer for the same cluster.

Two implementations exist on purpose (api/ cannot import mcp_servers, and calling
the agent to paint a panel is heavy and slow), so the guard has to be behavioural:
one set of seeded facts is pushed through BOTH
  * mcp-servers/mcp_servers/performance/tools/forecast_capacity.py
    (forecast_capacity_impl, cache.execute -> QueryResult)
  * api/dashboard/handler.py::_capacity_forecast (query(sql, params) -> [dict])
and every key the two payloads SHARE must be equal. Sharing the comparison that
way is deliberate: add a field to both and it is compared automatically, and a
field only one surface has (the tool's `note`, the panel's `projections`) is
skipped without having to maintain an exclusion list.

What used to be wrong: the tool took logical metric names (storage / connections /
aas) while the endpoint took raw metric_type values (storage_bytes /
db_connections / aas), and the endpoint's family map had no rds_instance or
elasticache key. So for one standalone RDS instance the agent produced a
storage-exhaustion ETA while the panel said "not applicable" for the same
cluster, at the same moment, from the same rows.

Two things this suite could NOT see at first, both found by review and both fixed:

  * the family SOURCE. Comparing verdicts is blind to the two surfaces resolving
    the engine family from two different places (the tool from
    cluster_meta.engine, the endpoint from the DynamoDB registry) for as long as
    the two sources agree. `_both` now lets them DISAGREE.
  * the SQL. A stub that hands the same canned trend row to both implementations
    cannot see a divergence living in the SQL text, and there was one: the tool's
    current_value subselect carried no lookback predicate while the endpoint's
    array_agg latest sits inside the windowed aggregate. So `_trend_row` models
    the lookback predicate each clause ACTUALLY carries, the same way
    tests/unit/test_metric_filters.py models the dimension predicate.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp_servers.performance.tools.forecast_capacity import forecast_capacity_impl
from mcp_servers.shared.models import QueryResult

_ROOT = Path(__file__).resolve().parents[3]
_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
_spec = importlib.util.spec_from_file_location(
    "dashboard_handler_capacity_parity", _DASHBOARD_DIR / "handler.py")
_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dash)

_GIB = 1024 ** 3
_TIB = 1024 ** 4


def _facts(engine, slope, current, samples=200, r2=0.9, meta_present=True,
           max_connections=None, settings_max_connections=None,
           docdb_connections_limit=None, instance_class=None,
           serverlessv2_max_acu=None, allocated_storage_gb=None,
           provisioned=None, evictions=None, stale_current=None):
    """One description of the world, handed to both surfaces.

    `slope` / `current` / `samples` describe the rows INSIDE the lookback window.
    `stale_current` is the newest value of a series whose rows all fall OUTSIDE it
    (collection stopped): a query bounded by the window cannot see it, an
    unbounded one can, which is the only way the two clauses can differ."""
    assert stale_current is None or samples == 0, (
        "stale_current means every row is older than the window, so the in-window "
        "sample count must be 0")
    return {
        "engine": engine, "slope": slope, "current": current, "samples": samples,
        "r2": r2, "meta_present": meta_present,
        "max_connections": max_connections,
        "settings_max_connections": settings_max_connections,
        "docdb_connections_limit": docdb_connections_limit,
        "instance_class": instance_class,
        "serverlessv2_max_acu": serverlessv2_max_acu,
        "allocated_storage_gb": allocated_storage_gb,
        "provisioned": provisioned, "evictions": evictions,
        "stale_current": stale_current,
    }


def _meta_row(f):
    return {
        "engine": f["engine"],
        "max_connections": f["max_connections"],
        "instance_class": f["instance_class"],
        "serverlessv2_max_acu": f["serverlessv2_max_acu"],
        "allocated_storage_gb": f["allocated_storage_gb"],
    }


def _route(sql, params, f):
    """Which fake result the seeded world owes this SQL. Shared by both adapters
    so neither surface can be fed a different world by accident. Order is
    most-specific first: every branch but cluster_settings hits metric_snapshots."""
    p = params or {}
    if "cluster_meta" in sql:
        return "meta"
    if "cluster_settings" in sql:
        return "settings"
    if "db_connections_limit" in sql:
        return "docdb_limit"
    if p.get("provisioned_metric") or p.get("pm"):
        return "provisioned"
    if "'evictions'" in sql:
        return "evictions"
    return "trend"


# `ts > NOW() - (:days || ' days')::interval`, whatever the param is named and
# whatever table alias qualifies ts. Its PRESENCE in a clause is what decides
# which rows that clause may see.
_WINDOW = re.compile(r"ts\s*>\s*NOW\(\)\s*-\s*\(:\w+\s*\|\|\s*' days'\)::interval")


def _split_latest_clause(sql):
    """(aggregate_text, latest_clause_text or None).

    The two surfaces get "current" two different ways: the tool from a nested
    `(SELECT value ... ORDER BY ts DESC LIMIT 1)` subselect, the endpoint from
    `(array_agg(value ORDER BY ts DESC))[1]` inside the aggregate itself. Cut the
    subselect out so each part can be inspected for its own predicates; None means
    the latest value comes from the aggregate and is therefore windowed by
    construction."""
    i = sql.find("(SELECT")
    if i < 0:
        return sql, None
    depth = 0
    for j in range(i, len(sql)):
        if sql[j] == "(":
            depth += 1
        elif sql[j] == ")":
            depth -= 1
            if depth == 0:
                return sql[:i] + sql[j + 1:], sql[i:j + 1]
    raise AssertionError(f"unbalanced subselect in trend SQL: {sql}")


def _trend_row(sql, f):
    """The trend row the seeded world owes THIS statement.

    Handing both implementations the same canned row is what made this suite
    blind to the SQL: the tool's current_value subselect had no lookback bound
    while the endpoint's latest value sits inside the windowed aggregate. Executed
    verbatim on PostgreSQL 14.18 against free_storage_bytes rows that all lay 60
    to 90 days back, the tool returned current_value=107374182400.0 and the
    endpoint 0.0, both alongside samples=0.

    So the lookback predicate is MODELLED per clause (the dimension predicate is
    not: tests/unit/test_metric_filters.py owns that with a mixed-row fixture).
    The aggregate must carry it, and the latest-value clause only sees in-window
    rows when it carries it too.

    The returned dict carries both surfaces' column aliases (slope_per_day/n/
    current_value for the tool, slope/samples/latest for the endpoint). Each
    implementation reads only its own, and reading the wrong one would show up as
    a mismatch."""
    agg, latest_clause = _split_latest_clause(sql)
    assert _WINDOW.search(agg), f"trend aggregate is not bounded by the lookback: {agg}"
    n = f["samples"]
    # PostgreSQL over an empty window: REGR_SLOPE / REGR_R2 and the latest value
    # are all NULL, COUNT(*) is 0.
    slope = None if n == 0 else f["slope"]
    r2 = None if n == 0 else f["r2"]
    latest = None if n == 0 else f["current"]
    if latest_clause is not None and not _WINDOW.search(latest_clause):
        # Unwindowed: this clause reaches rows the aggregate cannot count.
        latest = f["current"] if f["stale_current"] is None else f["stale_current"]
    return {"slope_per_day": slope, "slope": slope, "r2": r2,
            "n": n, "samples": n, "current_value": latest, "latest": latest,
            "first_ts": None, "last_ts": None}


def _mcp_cache(f):
    seen = {}

    def _single(v):
        return QueryResult(columns=["value"], rows=[] if v is None else [{"value": v}],
                           row_count=0 if v is None else 1)

    def _exec(sql, params=None):
        kind = _route(sql, params, f)
        seen.setdefault("kinds", []).append(kind)
        if kind == "meta":
            rows = [_meta_row(f)] if f["meta_present"] else []
            return QueryResult(columns=list(_meta_row(f)), rows=rows, row_count=len(rows))
        if kind == "settings":
            return _single(f["settings_max_connections"])
        if kind == "docdb_limit":
            return _single(f["docdb_connections_limit"])
        if kind == "provisioned":
            return _single(f["provisioned"])
        if kind == "evictions":
            return _single(f["evictions"])
        row = _trend_row(sql, f)
        return QueryResult(columns=list(row), rows=[row], row_count=1)

    cache = MagicMock()
    cache.execute.side_effect = _exec
    cache.seen = seen
    return cache


def _rest_query(f):
    def _q(sql, params=None):
        kind = _route(sql, params, f)
        if kind == "meta":
            return [_meta_row(f)] if f["meta_present"] else []
        if kind == "settings":
            v = f["settings_max_connections"]
            return [] if v is None else [{"value": v}]
        if kind == "docdb_limit":
            v = f["docdb_connections_limit"]
            return [] if v is None else [{"value": v}]
        if kind == "provisioned":
            v = f["provisioned"]
            return [] if v is None else [{"value": v}]
        if kind == "evictions":
            v = f["evictions"]
            return [] if v is None else [{"value": v}]
        return [_trend_row(sql, f)]
    return _q


def _both(f, metric, monkeypatch, cluster_id="c", days_lookback=30,
          registry_engine=None):
    """Run both surfaces over the same seeded world.

    `registry_engine` defaults to the cluster_meta engine, which is the ordinary
    case. Pass a different string to make the two POSSIBLE sources of the engine
    family disagree: the registry holds what an operator typed at registration,
    cluster_meta.engine holds what the collector observed writing the rows."""
    reg = f["engine"] if registry_engine is None else registry_engine
    monkeypatch.setattr(_dash, "_registry_engine", lambda _cid: reg)
    agent = forecast_capacity_impl(_mcp_cache(f), cluster_id=cluster_id, metric=metric,
                                   days_lookback=days_lookback)
    rest = _dash._capacity_forecast(_rest_query(f), cluster_id, metric, days_lookback)
    return agent, rest


def _assert_same_verdict(agent, rest):
    """Every key the two payloads share must carry the same value. Surface-only
    keys (the tool's note/confidence/range, the panel's label/projections) are
    skipped, so this needs no exclusion list to maintain."""
    shared = sorted(set(agent) & set(rest))
    # A payload pair with nothing in common would make this vacuous.
    for required in ("status", "days_until_limit", "approaching_limit", "grounded"):
        assert required in shared, f"{required} missing from one surface: {sorted(agent)} / {sorted(rest)}"
    mismatch = {k: (agent[k], rest[k]) for k in shared if agent[k] != rest[k]}
    assert not mismatch, f"agent vs REST disagree on {mismatch}"
    return {k: agent[k] for k in shared}


# ===========================================================================
# ONE source for the engine family
# ===========================================================================


def test_the_engine_family_comes_from_cluster_meta_on_both_surfaces(monkeypatch):
    """The dual-source defect, in its last hiding place. The tool derives the
    family from cluster_meta.engine; the endpoint used to derive it from the
    DynamoDB registry and then read the cluster_meta row while IGNORING its engine
    column, so the two surfaces still resolved the family from two different
    places. Verdict comparison could not see it while the sources agreed, and they
    are allowed to disagree: the registry holds what an operator typed at
    registration, cluster_meta.engine holds what the collector observed.

    cluster_meta wins because it is the engine that WROTE the rows: the family
    picks the metric_type (Aurora storage_bytes vs standalone-RDS
    free_storage_bytes), so a stale or mistyped registry value sends a surface to a
    series nobody writes. Measured on this exact world before the fix: the agent
    read free_storage_bytes and withheld the date while the endpoint read
    storage_bytes and reported days_until_limit=65486, disagreeing on 7 shared
    keys (engine_family, metric_type, limit, limit_basis, direction, usage_pct,
    days_until_limit)."""
    f = _facts("mysql", slope=2.0 * _GIB, current=100.0 * _GIB,
               allocated_storage_gb="200")
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-1",
                        registry_engine="aurora-mysql")
    v = _assert_same_verdict(agent, rest)
    assert v["engine_family"] == "rds_instance"
    assert v["metric_type"] == "free_storage_bytes"
    assert v["direction"] == "down"
    # free space GROWING is moving away from the 0-byte floor, so no date.
    assert v["days_until_limit"] is None


def test_an_absent_registry_row_does_not_make_the_endpoint_relational(monkeypatch):
    """A cluster missing from the registry reads back as engine "" (the registry
    row was legitimately absent, which is NOT the failed-lookup None), and
    engine_family("") is relational by legacy default. Deriving the family there
    silently turned every such cluster into an Aurora one: measured before the fix,
    a DynamoDB table answered status=ok/engine_family=dynamodb on the agent and
    unsupported_metric/relational on the endpoint."""
    f = _facts("dynamodb", slope=1.0, current=10.0, provisioned=50.0)
    agent, rest = _both(f, "read_capacity", monkeypatch, cluster_id="ddb-1",
                        registry_engine="")
    v = _assert_same_verdict(agent, rest)
    assert v["engine_family"] == "dynamodb"
    assert v["status"] == "ok"
    assert v["metric_type"] == "consumed_rcu"


# ===========================================================================
# The two response MODES, on both surfaces
# ===========================================================================


def test_growing_toward_a_ceiling_agrees_on_both_surfaces(monkeypatch):
    """Aurora volume GROWING toward the 128 TiB ceiling. 100 GiB now, +2 GiB/day."""
    f = _facts("aurora-postgresql", slope=2.0 * _GIB, current=100.0 * _GIB)
    agent, rest = _both(f, "storage", monkeypatch)
    v = _assert_same_verdict(agent, rest)
    assert v["metric_type"] == "storage_bytes"
    assert v["direction"] == "up"
    assert v["limit"] == float(128 * _TIB)
    assert v["forecast"] == "growing"
    assert v["days_until_limit"] == int((128 * _TIB - 100 * _GIB) / (2 * _GIB))


def test_depleting_toward_zero_agrees_on_both_surfaces(monkeypatch):
    """The second response mode: standalone RDS free space DEPLETING to 0 bytes
    (STORAGE_FULL). 20 GiB free losing 2 GiB/day is 10 days, and the percentage
    is measured against the ALLOCATED size, not against the limit of 0."""
    f = _facts("mysql", slope=-2.0 * _GIB, current=20.0 * _GIB,
               allocated_storage_gb="100")
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-1")
    v = _assert_same_verdict(agent, rest)
    assert v["engine_family"] == "rds_instance"
    assert v["metric_type"] == "free_storage_bytes"
    assert v["direction"] == "down"
    assert v["limit"] == 0.0
    assert v["forecast"] == "depleting"
    assert v["days_until_limit"] == 10
    assert v["approaching_limit"] is True
    assert v["usage_pct"] == 80.0


def test_a_zero_limit_produces_no_division_and_no_bogus_percentage(monkeypatch):
    """The depleting mode's limit IS 0, which is exactly what broke the panel's
    (current/limit)*100. usage_pct is computed server-side and is null when there
    is no denominator, so no consumer divides and nobody prints "0% of 0"."""
    f = _facts("mysql", slope=-1.0 * _GIB, current=5.0 * _GIB)  # no allocated size
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-2")
    v = _assert_same_verdict(agent, rest)
    assert v["limit"] == 0.0
    assert v["usage_pct"] is None
    assert v["days_until_limit"] == 5          # the ETA is still valid
    assert "allocated_gb" not in rest          # panel context only when known


def test_a_missing_limit_produces_no_percentage_and_no_date(monkeypatch):
    """An on-demand DynamoDB table has no provisioned_* row, so there is no
    ceiling at all: no percentage, no ETA, and the measured trend still reported.
    The old endpoint threw the trend away as not_applicable."""
    f = _facts("dynamodb", slope=50.0, current=1000.0, provisioned=None)
    agent, rest = _both(f, "read_capacity", monkeypatch, cluster_id="ddb-1")
    v = _assert_same_verdict(agent, rest)
    assert v["grounded"] is False
    assert v["limit"] == 0.0
    assert v["usage_pct"] is None
    assert v["days_until_limit"] is None
    assert v["approaching_limit"] is False
    assert v["forecast"] == "growing"


# ===========================================================================
# One growing + one depleting/near-limit case per family
# ===========================================================================


@pytest.mark.parametrize("engine,metric,extra,expect_type", [
    ("aurora-postgresql", "storage", {}, "storage_bytes"),
    ("aurora-mysql", "connections", {"max_connections": 2000}, "db_connections"),
    ("aurora-postgresql", "aas", {"instance_class": "db.r6g.4xlarge"}, "aas"),
    ("docdb", "storage", {}, "storage_bytes"),
    ("docdb", "connections", {"docdb_connections_limit": 1700}, "db_connections"),
    ("mysql", "connections", {"settings_max_connections": "1000"}, "db_connections"),
    # db.m5.4xlarge = 16 vCPU, so a current AAS of 10 is below the ceiling
    # (db.m5.large would be 2 vCPU, i.e. already past it).
    ("sqlserver-se", "aas", {"instance_class": "db.m5.4xlarge"}, "aas"),
    ("dynamodb", "read_capacity", {"provisioned": 50.0}, "consumed_rcu"),
    ("dynamodb", "write_capacity", {"provisioned": 50.0}, "consumed_wcu"),
    ("redis", "memory", {"evictions": 0.0}, "memory_usage_pct"),
    ("valkey", "memory", {"evictions": 0.0}, "memory_usage_pct"),
])
def test_every_enabled_family_metric_agrees_while_growing(engine, metric, extra,
                                                          expect_type, monkeypatch):
    f = _facts(engine, slope=1.0, current=10.0, **extra)
    agent, rest = _both(f, metric, monkeypatch)
    v = _assert_same_verdict(agent, rest)
    assert v["metric_type"] == expect_type
    assert v["status"] == "ok"
    assert v["forecast"] == "growing"
    assert v["direction"] == "up"
    # grounded means the ceiling came from this cluster's real config, so the
    # ETA is asserted rather than withheld.
    assert v["grounded"] is True
    assert v["days_until_limit"] is not None


@pytest.mark.parametrize("engine,metric,extra,current", [
    ("aurora-postgresql", "storage", {}, float(128 * _TIB)),
    ("aurora-mysql", "connections", {"max_connections": 2000}, 2000.0),
    ("aurora-postgresql", "aas", {"instance_class": "db.r6g.large"}, 2.0),
    ("docdb", "connections", {"docdb_connections_limit": 1700}, 1700.0),
    ("mysql", "connections", {"settings_max_connections": "1000"}, 1000.0),
    ("dynamodb", "write_capacity", {"provisioned": 50.0}, 3000.0),
])
def test_already_at_the_limit_agrees_and_stays_urgent(engine, metric, extra,
                                                      current, monkeypatch):
    """Pinned at the ceiling used to look identical to trending away from it (no
    date, approaching_limit false) on both surfaces: the calmest payload for the
    most urgent state."""
    f = _facts(engine, slope=1.0, current=current, **extra)
    agent, rest = _both(f, metric, monkeypatch)
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "limit_reached"
    assert v["days_until_limit"] == 0
    assert v["approaching_limit"] is True
    assert v["usage_pct"] == 100.0


def test_rds_instance_storage_full_agrees_and_stays_urgent(monkeypatch):
    """The depleting mode's at-limit case: 0 free bytes IS storage-full."""
    f = _facts("mysql", slope=-1.0 * _GIB, current=0.0, allocated_storage_gb="100")
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-1")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "limit_reached"
    assert v["days_until_limit"] == 0
    assert v["approaching_limit"] is True
    assert v["usage_pct"] == 100.0


# ===========================================================================
# ElastiCache: a cache at capacity BY DESIGN is not a cache running out
# ===========================================================================


def test_healthy_lru_cache_at_maxmemory_is_not_exhausting_on_either_surface(monkeypatch):
    """A Redis cache with an LRU/TTL policy sits at 97% by design with a slope of
    about 0. Both surfaces must say "cannot date this" (status=evicting, no ETA,
    approaching_limit false) rather than either doom it or all-clear it. 97% is
    still reported, so this is not silence."""
    f = _facts("redis", slope=0.01, current=97.0, evictions=4200.0)
    agent, rest = _both(f, "memory", monkeypatch, cluster_id="cache-1")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "evicting"
    assert v["days_until_limit"] is None
    assert v["approaching_limit"] is False
    assert v["usage_pct"] == 97.0
    # Neither surface may present it as a clean bill of health.
    assert "eviction" in agent["note"]
    assert "eviction" in rest["reason"]
    assert "elasticache_evictions_spike" in agent["note"]
    assert "elasticache_evictions_spike" in rest["reason"]


def test_a_cache_with_no_evictions_and_a_rising_trend_is_still_forecast(monkeypatch):
    """Control for the case above: zero evictions plus a rising memory trend is
    the noeviction-policy cache genuinely filling up, where full means write
    errors. Suppressing that would be the opposite mistake."""
    f = _facts("redis", slope=2.0, current=60.0, evictions=0.0)
    agent, rest = _both(f, "memory", monkeypatch, cluster_id="cache-1")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "ok"
    assert v["days_until_limit"] == 20        # (100 - 60) / 2
    assert v["approaching_limit"] is True


# ===========================================================================
# Refusals: explicit on BOTH surfaces, never silence and never a number
# ===========================================================================


@pytest.mark.parametrize("engine,metric", [
    ("dynamodb", "storage"),
    ("dynamodb", "connections"),
    ("dynamodb", "aas"),
    ("dynamodb", "memory"),
    ("redis", "storage"),
    ("redis", "connections"),
    ("redis", "aas"),
    ("redis", "read_capacity"),
    ("memcached", "memory"),
    ("docdb", "aas"),
    ("docdb", "read_capacity"),
    ("mysql", "memory"),
    ("aurora-postgresql", "write_capacity"),
])
def test_unsupported_family_metric_is_explicit_on_both_surfaces(engine, metric,
                                                                monkeypatch):
    """Not silence and not a number: the same status, the same reason sentence,
    and no forecast fields to misread. Memcached memory is in the list because it
    is refused per ENGINE inside a family whose other engines are supported."""
    f = _facts(engine, slope=1.0, current=1.0, samples=0)
    agent, rest = _both(f, metric, monkeypatch, cluster_id="x")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "unsupported_metric"
    assert v["days_until_limit"] is None
    assert v["approaching_limit"] is False
    assert v["samples"] == 0
    assert v["grounded"] is False
    assert agent["reason"] == rest["reason"]
    assert v["engine_family"] in agent["reason"]


def test_memcached_memory_names_the_engine_not_just_the_family(monkeypatch):
    f = _facts("memcached", slope=1.0, current=50.0, samples=0)
    agent, rest = _both(f, "memory", monkeypatch, cluster_id="cache-mc")
    assert agent["engine_family"] == "elasticache" == rest["engine_family"]
    assert "Memcached" in agent["reason"] and "Memcached" in rest["reason"]


@pytest.mark.parametrize("bad", ["storage_bytes", "db_connections", "consumed_rcu",
                                 "memory_usage_pct", "storage_gb", ""])
def test_a_raw_metric_type_is_unknown_metric_on_both_surfaces(bad, monkeypatch):
    """The raw metric_type values the endpoint used to accept are now bad NAMES on
    both surfaces, told apart from an engine refusal, and both list the valid
    logical names in the same order."""
    f = _facts("aurora-postgresql", slope=1.0, current=1.0)
    agent, rest = _both(f, bad, monkeypatch)
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "unknown_metric"
    assert agent["reason"] == rest["reason"]
    for valid in ("storage", "connections", "aas", "read_capacity",
                  "write_capacity", "memory"):
        assert valid in agent["reason"]


def test_no_cluster_meta_row_fails_closed_the_same_way_on_both_surfaces(monkeypatch):
    """engine_family(None) defaults to relational, so proceeding without a
    cluster_meta row applies storage_bytes vs 128 TiB to an unregistered cluster
    and reports zero samples as a calm, flat trend. Both surfaces refuse, and the
    endpoint needs that row anyway: every limit input (max_connections,
    instance_class, serverlessv2_max_acu, allocated_storage_gb) lives there, so
    without it the aas / connections ceilings silently become fleet-wide
    fallbacks and the two surfaces stop agreeing."""
    f = _facts("aurora-postgresql", slope=1.0, current=1.0, meta_present=False)
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="ghost")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "unknown_cluster"
    assert v["samples"] == 0
    assert v["days_until_limit"] is None
    assert agent["reason"] == rest["reason"]


@pytest.mark.parametrize("engine,metric", [
    ("dynamodb", "storage"),
    ("redis", "connections"),
    ("memcached", "memory"),
])
def test_an_unsupported_metric_on_an_uncollected_cluster_still_agrees(engine, metric,
                                                                     monkeypatch):
    """The ordering corner. The tool derives the family FROM cluster_meta.engine, so
    a missing row means it cannot reach a family verdict at all and answers
    unknown_cluster. The endpoint reads the family from the registry, so it COULD
    have rejected the metric first and answered unsupported_metric for the same
    cluster at the same moment. Both must check in the same order."""
    f = _facts(engine, slope=1.0, current=1.0, samples=0, meta_present=False)
    agent, rest = _both(f, metric, monkeypatch, cluster_id="cold")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "unknown_cluster"
    assert agent["reason"] == rest["reason"]


def test_zero_samples_is_no_data_on_both_surfaces_never_at_the_limit(monkeypatch):
    """free_storage_bytes has a limit of 0 and `latest` is null with no rows, so an
    uncollected cluster would otherwise be declared STORAGE_FULL by whichever
    surface got there first.

    allocated_storage_gb is SET here on purpose: a known allocated size is a
    denominator, and with it both surfaces used to answer usage_pct=100.0 for a
    cluster nothing was measured on, because the depleting mode reads
    (allocated - current)/allocated and `current` is 0.0 for lack of a row rather
    than because 0 bytes were observed. "Storage 100% used" is the worst possible
    thing to invent out of no data, and the REST projections were a matching flat
    line at zero. A measurement is required, not just a denominator."""
    f = _facts("mysql", slope=0.0, current=None, samples=0,
               allocated_storage_gb="100")
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-1")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "no_data"
    assert v["forecast"] == "no_data"
    assert v["approaching_limit"] is False
    assert v["days_until_limit"] is None
    assert v["usage_pct"] is None
    assert v["current_value"] == 0.0
    # and nothing is projected from a trend that does not exist
    assert "projections" not in rest


def test_a_series_older_than_the_window_is_no_data_on_both_surfaces(monkeypatch):
    """SQL parity, not just verdict parity. The rows exist but every one of them is
    older than the lookback, so the window holds nothing: both surfaces owe
    samples=0 AND a current_value that describes the same rows the count does.

    This is the divergence the canned-row stub could not see. The tool read
    `current` from a subselect with no lookback bound while the endpoint read it
    from array_agg INSIDE the windowed aggregate, so on PostgreSQL 14.18 with
    free_storage_bytes rows 60 to 90 days back the tool answered
    current_value=107374182400.0 (usage_pct 50.0 against a 200 GB allocation) and
    the endpoint 0.0, both reporting samples=0. A stale reading presented as
    "current" is how a cluster whose collection stopped months ago gets a storage
    verdict from data nobody is collecting."""
    f = _facts("mysql", slope=0.0, current=None, samples=0,
               stale_current=100.0 * _GIB, allocated_storage_gb="200")
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-stale")
    v = _assert_same_verdict(agent, rest)
    assert v["status"] == "no_data"
    assert v["samples"] == 0
    assert v["current_value"] == 0.0
    assert v["usage_pct"] is None


def test_serverless_v2_aas_ceiling_agrees_on_both_surfaces(monkeypatch):
    """Every other aas case seeds a provisioned instance_class, so the ACU
    conversion (instance_class db.serverless has no vCPU token, so the ceiling
    comes from serverlessv2_max_acu at 4 ACU per vCPU) was pinned on the tool side
    only. Measured: with this case deselected, changing the endpoint's
    _ACU_PER_VCPU from 4.0 to 1.0 leaves 2096 unit tests green, while the same
    mutation in the tool fails test_serverless_v2_aas_ceiling_comes_from_max_acu.
    Serverless v2 is the default shape for new Aurora clusters, so this pair is not
    an edge case."""
    f = _facts("aurora-postgresql", slope=0.1, current=2.0,
               instance_class="db.serverless", serverlessv2_max_acu=64.0)
    agent, rest = _both(f, "aas", monkeypatch)
    v = _assert_same_verdict(agent, rest)
    assert v["limit"] == 16.0                 # 64 ACU / 4 ACU per vCPU
    assert "serverlessv2_max_acu=64.0" in v["limit_basis"]
    assert v["grounded"] is True
    assert v["days_until_limit"] == 140       # (16 - 2) / 0.1


def test_ungrounded_limit_withholds_the_date_on_both_surfaces(monkeypatch):
    """No max_connections anywhere: the 5000 ceiling is an assumption, so neither
    surface may date it. This is the pair that used to disagree most often, since
    the endpoint had no cluster_meta lookup at all."""
    f = _facts("aurora-postgresql", slope=5.0, current=100.0)
    agent, rest = _both(f, "connections", monkeypatch)
    v = _assert_same_verdict(agent, rest)
    assert v["grounded"] is False
    assert v["limit"] == 5000.0
    assert v["days_until_limit"] is None
    assert v["approaching_limit"] is False


def test_far_future_eta_is_reported_but_unflagged_on_both_surfaces(monkeypatch):
    """Live regression (dbops-demo-mysql, 2026-07-24): free space fell a few MB a
    day, the ETA came out around 219 years, and the boolean still said
    approaching. Both surfaces bound the FLAG to 365 days and keep the number."""
    f = _facts("mysql", slope=-1.0 * 1024 ** 2, current=20.0 * _GIB,
               allocated_storage_gb="20")
    agent, rest = _both(f, "storage", monkeypatch, cluster_id="rds-mysql-1")
    v = _assert_same_verdict(agent, rest)
    assert v["days_until_limit"] > 365
    assert v["approaching_limit"] is False
    assert v["forecast"] == "depleting"


def test_the_family_metric_tables_themselves_agree(monkeypatch):
    """Belt and braces for the behavioural tests above: the two per-family maps
    must enumerate the same metrics for the same families, so a family enabled on
    one surface can never be missing from the other (that omission is the whole
    defect E1-5 fixes)."""
    from mcp_servers.performance.tools import forecast_capacity as fc

    agent_map = {}
    for fam in ("relational", "documentdb", "rds_instance", "dynamodb", "elasticache"):
        allowed = set()
        if fc._STORAGE_SERIES.get(fam):
            allowed.add("storage")
        if fc._CONNECTION_SERIES.get(fam):
            allowed.add("connections")
        if fc._AAS_SERIES.get(fam):
            allowed.add("aas")
        for logical, per_fam in fc._THROUGHPUT_SERIES.items():
            if per_fam.get(fam):
                allowed.add(logical)
        if fc._MEMORY_SERIES.get(fam):
            allowed.add("memory")
        agent_map[fam] = allowed

    assert agent_map == _dash._CAPACITY_METRICS_BY_FAMILY
    # and the vocabulary itself is the same set of names
    assert set(fc._VALID_METRICS) == set(_dash._CAPACITY_METRICS)
    assert list(fc._VALID_METRICS) == list(_dash._CAPACITY_METRICS)

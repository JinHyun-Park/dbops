"""metric_snapshots dimension-filter contract (E1-1).

`metric_snapshots` stores the SAME `metric_type` at several dimensionalities:
a cluster-level row (dimensions '{}'), per-instance rows ({instance,role}), per
PI wait-event rows ({db.wait_event.name}) and per-GSI rows ({gsi}). A
cluster-level aggregate that does not filter mixes a total with its own
fractions: no error, just a wrong number. This has bitten the project three
times.

Two layers of guard here:

  1. Drift guards: the api/ copy of the constants stays byte-identical to the
     canonical mcp-servers module, and no reader may resurrect the WEAK form
     `NOT jsonb_exists(dimensions,'instance')` (PI wait-event and GSI rows carry
     no 'instance' key, so they survive it).

  2. RESULT tests: a MIXED-row fixture (cluster total + 2 per-instance rows +
     2 wait-event rows for the same metric_type) is pushed through the REAL
     production SQL of the highest-value readers, and the computed number is
     asserted to be the cluster-level answer. `_apply_dim_filter` models the two
     predicates faithfully and picks the one the executed SQL actually contains,
     so dropping or weakening the filter changes the returned NUMBER and the
     assertion fails. Every result test also asserts the fixture is
     discriminating (the weak/no-filter answer differs), so the test cannot
     silently degrade into a tautology.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_CANONICAL = _ROOT / "mcp-servers" / "mcp_servers" / "shared" / "metric_filters.py"
_API_COPY = _ROOT / "api" / "dashboard" / "metric_filters.py"

STRICT = "AND (dimensions IS NULL OR dimensions::text = '{}')"
WEAK = "AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))"


# ===========================================================================
# 1. Drift guards
# ===========================================================================


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_api_copy_of_the_constants_is_identical_to_the_canonical_module():
    """api/ cannot import mcp_servers (no shared Lambda layer), so the constants
    are duplicated. If the two ever drift, the dashboard and the agent answer the
    same question differently."""
    canonical = _load(_CANONICAL, "metric_filters_canonical")
    api_copy = _load(_API_COPY, "metric_filters_api_copy")
    assert canonical.CLUSTER_LEVEL_ONLY == api_copy.CLUSTER_LEVEL_ONLY
    assert canonical.EXCLUDE_PER_INSTANCE == api_copy.EXCLUDE_PER_INSTANCE
    assert canonical.CLUSTER_LEVEL_ONLY == STRICT
    assert canonical.EXCLUDE_PER_INSTANCE == WEAK
    # Same verbatim-duplication contract engine_family.py has.
    assert _CANONICAL.read_text() == _API_COPY.read_text()


def test_no_reader_uses_the_weak_per_instance_only_filter():
    """The weak form is INSUFFICIENT for a cluster-level aggregate: PI
    wait-event rows ({db.wait_event.name}) and DynamoDB GSI rows ({gsi}) have no
    'instance' key, so they pass it. It survives only as the named
    EXCLUDE_PER_INSTANCE constant, for the readers that deliberately return the
    dimensioned detail rows (wait-event stacked chart, per-GSI panel)."""
    hits = subprocess.run(
        ["grep", "-rln", "--include=*.py", "NOT jsonb_exists(dimensions, 'instance')",
         "api", "data-pipeline", "mcp-servers", "agent"],
        cwd=_ROOT, capture_output=True, text=True,
    ).stdout.split()
    allowed = {"mcp-servers/mcp_servers/shared/metric_filters.py",
               "api/dashboard/metric_filters.py"}
    assert set(hits) <= allowed, f"weak dimension filter resurrected in: {sorted(set(hits) - allowed)}"


# ===========================================================================
# 2. Mixed-row fixture + faithful predicate model
# ===========================================================================

# One metric_type ('aas'), five writers' worth of rows, as production stores them:
#   ts=10 / ts=20  cw-style: cluster total '{}' + two per-instance rows
#   ts=30          pi_collector: '{}' total + one row per wait event
# Cluster-level truth: [2.0, 6.0, 6.0] -> avg 4.666.., max 6.0, n=3, latest 6.0
_AAS_ROWS = [
    {"ts": 10, "metric_type": "aas", "value": 2.0, "dimensions": {}},
    {"ts": 10, "metric_type": "aas", "value": 1.2, "dimensions": {"instance": "i-1", "role": "writer"}},
    {"ts": 10, "metric_type": "aas", "value": 0.8, "dimensions": {"instance": "i-2", "role": "reader"}},
    {"ts": 20, "metric_type": "aas", "value": 6.0, "dimensions": {}},
    {"ts": 20, "metric_type": "aas", "value": 4.0, "dimensions": {"instance": "i-1", "role": "writer"}},
    {"ts": 20, "metric_type": "aas", "value": 2.0, "dimensions": {"instance": "i-2", "role": "reader"}},
    {"ts": 30, "metric_type": "aas", "value": 6.0, "dimensions": {}},
    {"ts": 30, "metric_type": "aas", "value": 5.0, "dimensions": {"db.wait_event.name": "CPU"}},
    {"ts": 30, "metric_type": "aas", "value": 1.0, "dimensions": {"db.wait_event.name": "IO:DataFileRead"}},
]

_CLUSTER_AAS = [2.0, 6.0, 6.0]
_CLUSTER_AVG = sum(_CLUSTER_AAS) / len(_CLUSTER_AAS)   # 4.666...
_CLUSTER_MAX = 6.0

# What the WEAK filter would have produced (wait-event rows survive it).
_WEAK_AAS = [2.0, 6.0, 6.0, 5.0, 1.0]
_WEAK_AVG = sum(_WEAK_AAS) / len(_WEAK_AAS)            # 4.0

assert _CLUSTER_AVG != _WEAK_AVG, "fixture must discriminate strict from weak"


def _apply_dim_filter(sql, rows=_AAS_ROWS):
    """Apply the dimension predicate the SQL ACTUALLY contains. This is the one
    place the two predicates are modelled; every result test below goes through
    it, so a dropped/weakened filter in production SQL changes the numbers the
    tests assert on."""
    flat = " ".join(sql.split())
    if STRICT in flat:
        keep = lambda d: d is None or d == {}                       # noqa: E731
    elif WEAK in flat:
        keep = lambda d: d is None or "instance" not in d           # noqa: E731
    else:
        keep = lambda d: True                                       # noqa: E731
    return [r for r in rows if keep(r["dimensions"])]


def _agg(sql, rows=_AAS_ROWS):
    """(avg, max, min, count, latest_candidates) of the surviving 'aas' rows."""
    kept = [r for r in _apply_dim_filter(sql, rows) if r["metric_type"] == "aas"]
    if not kept:
        return None
    vals = [r["value"] for r in kept]
    top_ts = max(r["ts"] for r in kept)
    return {
        "avg": sum(vals) / len(vals),
        "max": max(vals),
        "min": min(vals),
        "count": len(vals),
        "latest_candidates": sorted(r["value"] for r in kept if r["ts"] == top_ts),
    }


def test_the_predicate_model_itself_discriminates():
    """Guard the guard: if _apply_dim_filter stopped distinguishing the forms,
    every result test below would pass vacuously."""
    base = "SELECT AVG(value) FROM metric_snapshots WHERE cluster_id = :cid "
    assert _agg(base + STRICT)["avg"] == pytest.approx(_CLUSTER_AVG)
    assert _agg(base + WEAK)["avg"] == pytest.approx(_WEAK_AVG)
    assert _agg(base)["avg"] == pytest.approx(sum(r["value"] for r in _AAS_ROWS) / len(_AAS_ROWS))


# ===========================================================================
# 3. RESULT tests: api/dashboard
# ===========================================================================

_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
_dashboard = _load(_DASHBOARD_DIR / "handler.py", "dashboard_handler_metric_filters")


def test_dashboard_overview_metrics_report_the_cluster_level_average():
    """/overview headline cards. Before E1-1 this used the weak filter, so the
    PI wait-event rows were averaged in alongside the cluster total."""
    def _query(sql, params=None):
        if "cluster_meta" in sql:
            return [{"cluster_id": "c", "engine": "aurora-postgresql", "status": "available"}]
        if "metric_snapshots" in sql:
            a = _agg(sql)
            return [{"metric_type": "aas", "avg_val": a["avg"], "max_val": a["max"]}]
        return []

    row = _dashboard._overview(_query, "c")["metrics"][0]
    assert row["avg_val"] == pytest.approx(_CLUSTER_AVG)
    assert row["avg_val"] != pytest.approx(_WEAK_AVG)
    assert row["max_val"] == pytest.approx(_CLUSTER_MAX)


def test_fleet_latest_value_has_exactly_one_candidate_row():
    """Fleet cards read `(array_agg(value ORDER BY ts DESC))[1]`, "the latest
    value". With the weak filter the newest timestamp holds THREE rows (total +
    two wait events), so PostgreSQL is free to hand the card a single wait
    event's 5.0 as the cluster's AAS. Strict leaves exactly one candidate, so
    the number is well-defined."""
    captured = {}

    def _query(sql, params=None):
        if "latest_metrics" in sql:
            captured["sql"] = sql
        return []

    _dashboard._multi_cluster_overview(_query)
    a = _agg(captured["sql"])
    assert a["latest_candidates"] == [_CLUSTER_MAX]
    assert len(_agg(captured["sql"].replace(STRICT, WEAK))["latest_candidates"]) == 3


def test_dashboard_capacity_forecast_regresses_only_cluster_level_rows(monkeypatch):
    """The api/ twin of the Performance MCP `forecast_capacity` that was fixed in
    E-0. Sample count and `latest` must come from the cluster-level series."""
    captured = {}

    def _query(sql, params=None):
        if "metric_snapshots" in sql:
            captured["sql"] = sql
            a = _agg(sql)
            return [{"slope": 0.1, "latest": a["latest_candidates"][-1],
                     "first_ts": "2026-07-01", "last_ts": "2026-07-24", "samples": a["count"]}]
        return []

    monkeypatch.setattr(_dashboard, "_registry_engine", lambda _cid: "aurora-postgresql")
    out = _dashboard._capacity_forecast(_query, "c", "aas", 30)
    assert out["samples"] == len(_CLUSTER_AAS)
    assert out["samples"] != len(_WEAK_AAS)
    assert out["current"] == pytest.approx(_CLUSTER_MAX)
    assert STRICT in captured["sql"]


# A cluster whose ONLY recent rows are dimensioned: per-instance rows plus PI
# wait-event rows, for the same metric_type a cluster-level row would use. The
# anomaly scoring query (strict filter) sees NOTHING here, so the reader's
# recent-samples probe must see nothing either. With the weak filter the two
# wait-event rows survive, the probe reports "samples exist", and the operator is
# told to wait ~2 weeks for a baseline instead of checking why no cluster-level
# row is being written.
_DIMENSIONED_ONLY = [r for r in _AAS_ROWS if r["dimensions"]]

assert [r for r in _DIMENSIONED_ONLY if "instance" not in r["dimensions"]], (
    "fixture must keep rows that survive the WEAK filter, or it cannot discriminate"
)


def test_dashboard_anomaly_probe_counts_only_cluster_level_samples():
    """/anomalies distinguishes "no baseline yet" (wait) from "no samples at all"
    (fix collection) with an existence probe. Dimensioned rows are invisible to
    the scoring query, so they must not count as samples."""
    def _query(sql, params=None, mangle=lambda s: s):
        if "metric_baselines" in sql:
            return []                                   # nothing scored
        return _apply_dim_filter(mangle(sql), _DIMENSIONED_ONLY)

    assert _dashboard._anomalies(_query, "c", 4, 2.5)["baseline_mode"] == "no_samples"
    weak = lambda sql, params=None: _query(sql, params, lambda s: s.replace(STRICT, WEAK))  # noqa: E731
    assert _dashboard._anomalies(weak, "c", 4, 2.5)["baseline_mode"] == "none"


# ===========================================================================
# 4. RESULT tests: MCP server tools (agent-facing)
# ===========================================================================


def test_agent_anomaly_probe_counts_only_cluster_level_samples():
    """The agent's twin of the probe above: same filter, or the chat answer and
    the dashboard disagree about whether the cluster is judgeable at all."""
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
    from mcp_servers.shared.models import QueryResult

    class _Cache:
        def __init__(self, mangle=lambda s: s):
            self._mangle = mangle

        def execute(self, sql, params=None):
            if "metric_baselines" in sql:
                return QueryResult(columns=[], rows=[], row_count=0)
            kept = _apply_dim_filter(self._mangle(sql), _DIMENSIONED_ONLY)
            return QueryResult(columns=["?column?"], rows=kept, row_count=len(kept))

    assert detect_anomalies_impl(_Cache(), "c")["baseline_mode"] == "no_samples"
    weakened = _Cache(lambda s: s.replace(STRICT, WEAK))
    assert detect_anomalies_impl(weakened, "c")["baseline_mode"] == "none"


def test_agent_health_status_reports_the_cluster_level_average():
    """The agent's health tool must not disagree with the dashboard card."""
    from mcp_servers.incident.tools.health_status import get_health_status_impl
    from mcp_servers.shared.models import QueryResult

    class _Cache:
        def execute(self, sql, params=None):
            if "cluster_meta" in sql:
                return QueryResult(columns=["status"],
                                   rows=[{"cluster_id": "c", "status": "available",
                                          "engine": "aurora-postgresql"}], row_count=1)
            a = _agg(sql)
            return QueryResult(columns=["metric_type", "avg_val", "max_val"],
                               rows=[{"metric_type": "aas", "avg_val": a["avg"],
                                      "max_val": a["max"]}], row_count=1)

    metrics = get_health_status_impl(_Cache(), "c")["current_metrics"][0]
    assert metrics["avg_val"] == pytest.approx(_CLUSTER_AVG)
    assert metrics["avg_val"] != pytest.approx(_WEAK_AVG)


def test_agent_performance_summary_kpis_are_cluster_level():
    """avg_aas / max_aas / peak_connections are three separate subselects, each
    one needs the filter, so the aggregate is computed per subselect here."""
    from mcp_servers.performance.tools.performance_summary import get_performance_summary_impl
    from mcp_servers.shared.models import QueryResult

    class _Cache:
        def execute(self, sql, params=None):
            # One statement, three metric_snapshots subselects: each must carry
            # the filter, so model them by counting occurrences.
            assert sql.count(STRICT) == 3, sql
            a = _agg(sql)
            return QueryResult(columns=["avg_aas", "max_aas", "slow_count", "peak_connections"],
                               rows=[{"avg_aas": a["avg"], "max_aas": a["max"],
                                      "slow_count": 0, "peak_connections": a["max"]}],
                               row_count=1)

    kpis = get_performance_summary_impl(_Cache(), "c")["kpis"]
    assert kpis["avg_aas"] == pytest.approx(_CLUSTER_AVG)
    assert kpis["avg_aas"] != pytest.approx(_WEAK_AVG)


def test_agent_compare_periods_averages_cluster_level_rows():
    from mcp_servers.performance.tools.compare_periods import compare_periods_impl
    from mcp_servers.shared.models import QueryResult

    class _Cache:
        def execute(self, sql, params=None):
            a = _agg(sql)
            return QueryResult(
                columns=["avg_value", "max_value", "min_value", "sample_count"],
                rows=[{"avg_value": a["avg"], "max_value": a["max"],
                       "min_value": a["min"], "sample_count": a["count"]}], row_count=1)

    out = compare_periods_impl(_Cache(), "c", "a1", "a2", "b1", "b2", metric_type="aas")
    assert out["period_a"]["avg_value"] == pytest.approx(_CLUSTER_AVG)
    assert out["period_a"]["sample_count"] == len(_CLUSTER_AAS)


# ===========================================================================
# 5. RESULT tests: data-pipeline
# ===========================================================================


def test_daily_report_aas_stats_are_cluster_level():
    """report_generator._build_report_data read `aas` with NO dimension filter at
    all, so avg/max/p95/samples in every daily report mixed the cluster total
    with its own per-wait-event fractions."""
    handler_path = _ROOT / "data-pipeline" / "report_generator" / "handler.py"
    sys.path.insert(0, str(handler_path.parent))
    rg = _load(handler_path, "report_generator_metric_filters")

    def _cache_query(sql, params=None):
        if "metric_snapshots" not in sql:
            return []
        a = _agg(sql)
        if a is None:
            return []
        return [{"avg_aas": a["avg"], "max_aas": a["max"], "p95_aas": a["max"],
                 "samples": a["count"], "ts": "2026-07-24T00:00:00Z", "value": a["max"],
                 "cnt": a["count"], "max_conn": a["max"], "avg_conn": a["avg"],
                 "start_bytes": 1.0, "end_bytes": 2.0, "delta_bytes": 1.0}]

    data = rg._build_report_data(_cache_query, "c")
    assert data["aas"]["avg_aas"] == pytest.approx(_CLUSTER_AVG)
    assert data["aas"]["avg_aas"] != pytest.approx(_WEAK_AVG)
    assert data["aas"]["samples"] == len(_CLUSTER_AAS)
    assert data["aas_busy_minutes_above_threshold"] == len(_CLUSTER_AAS)


def test_alert_operand_evaluates_the_cluster_level_aggregate():
    """An alert firing on a single PI wait event instead of the cluster total is
    a false page. MAX over the window must be the cluster series' max."""
    handler_path = _ROOT / "data-pipeline" / "alert_evaluator" / "handler.py"
    sys.path.insert(0, str(handler_path.parent))
    ae = _load(handler_path, "alert_evaluator_metric_filters")

    def _query(sql, params=None):
        return [{"v": _agg(sql)["avg"]}]

    matched, obs, _summary = ae._evaluate_operand(
        _query, "c", {"metric_type": "aas", "comparison": ">", "threshold": 4.5, "agg": "avg"})
    assert obs == pytest.approx(_CLUSTER_AVG)
    assert matched is True          # 4.67 > 4.5
    # With the weak filter the observed average is 4.0 and the alert would NOT
    # have fired: the wrong number changes the decision, not just the display.
    assert _WEAK_AVG < 4.5


def test_outcome_evaluator_compares_cluster_level_to_cluster_level_baselines():
    """metric_baselines is trained cluster-level only (pg_baseline_trainer), so
    the observed value must be cluster-level too or the learning loop grades
    remediations against a mismatched scale."""
    ev_path = _ROOT / "data-pipeline" / "outcome_evaluator" / "evaluator.py"
    sys.path.insert(0, str(ev_path.parent))
    ev = _load(ev_path, "outcome_evaluator_metric_filters")

    def _query(sql, params=None):
        if "metric_baselines" in sql:
            # median/iqr tuned so the CLUSTER answer (4.67) is inside the band
            # and the WEAK answer (4.0) is outside it.
            return [{"median": 4.6, "iqr": 0.2}]
        return [{"v": _agg(sql)["avg"]}]

    verdict = ev._evaluate_metric(_query, {"cluster_id": "c", "watch_metric": "aas"})
    assert verdict == "resolved"


# ===== deletion guard (E1-1 verification gap) =====
#
# The grep guard above forbids the WEAK literal, and the mixed-row result tests
# cover 9 readers numerically. Neither catches a filter being DELETED at one of
# the other sites: the adversarial verifier proved it by removing the strict
# filter from pg_baseline_trainer and from proactive_monitor and watching the
# whole suite stay green. Those two are the worst places for it to go unnoticed,
# because the trainer feeds every seasonal anomaly score and the monitor raises
# the alerts.
#
# So this is a census: how many cluster-level predicates each reader file is
# known to need. Deleting one drops the count and fails here. Adding a new
# cluster-level query raises it, which fails too and forces the author to record
# the new site deliberately (and, better, to add a result test for it).
import pathlib as _pathlib
import re as _re

_STRICT_LITERAL = "dimensions IS NULL OR dimensions::text = '{}'"
_ALIASED_STRICT = _re.compile(r"\w+\.dimensions IS NULL OR \w+\.dimensions::text = '\{\}'")
# Word-boundaried, and it accepts the leading-underscore module alias
# (`_CLUSTER_LEVEL_ONLY = CLUSTER_LEVEL_ONLY`, used by forecast_capacity) because
# that alias is what the f-string SQL interpolates. Unbounded, the alias
# ASSIGNMENT line matched twice (once inside `_CLUSTER_LEVEL_ONLY`, once for the
# right-hand side), so forecast_capacity's recorded 4 was really 2 SQL predicates
# plus 2 phantom matches. The numbers have to BE predicate counts, otherwise the
# next author adding a query cannot derive the new expected value.
_CONST_USE = _re.compile(r"\b_?CLUSTER_LEVEL_ONLY\b|\bcluster_level_only\(")
# `X = CLUSTER_LEVEL_ONLY` / `_X = mod.CLUSTER_LEVEL_ONLY`: a rebinding, not a query.
_ALIAS_ASSIGN = _re.compile(r"^\s*_?\w*CLUSTER_LEVEL_ONLY\s*=\s*[\w.]*CLUSTER_LEVEL_ONLY\s*$")

_EXPECTED_CLUSTER_LEVEL_PREDICATES = {
    # 8 + the anomaly reader's recent-samples existence probe.
    "api/dashboard/handler.py": 9,
    "api/simulation/handler.py": 4,
    "data-pipeline/alert_evaluator/handler.py": 2,
    "data-pipeline/etl_collector/collectors/capacity_forecast.py": 3,
    "data-pipeline/etl_collector/collectors/cost_check.py": 2,
    "data-pipeline/etl_collector/collectors/docdb_findings.py": 3,
    "data-pipeline/etl_collector/collectors/dynamodb_findings.py": 6,
    "data-pipeline/etl_collector/collectors/elasticache_findings.py": 2,
    "data-pipeline/etl_collector/collectors/mysql_param_fitness.py": 2,
    "data-pipeline/etl_collector/collectors/pg_baseline_trainer.py": 1,
    "data-pipeline/etl_collector/collectors/pg_param_fitness.py": 2,
    "data-pipeline/outcome_evaluator/evaluator.py": 1,
    "data-pipeline/proactive_monitor/handler.py": 2,
    "data-pipeline/report_generator/handler.py": 7,
    "mcp-servers/mcp_servers/incident/tools/correlate_signals.py": 1,
    "mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py": 2,
    "mcp-servers/mcp_servers/incident/tools/health_status.py": 1,
    "mcp-servers/mcp_servers/performance/tools/compare_periods.py": 1,
    # 2 in the scoring SQL (recent + flat CTEs) + the recent-samples probe.
    "mcp-servers/mcp_servers/performance/tools/detect_anomalies.py": 3,
    # 2 in the aggregate/current_value pair + 1 in the DocumentDB
    # db_connections_limit ceiling lookup.
    "mcp-servers/mcp_servers/performance/tools/forecast_capacity.py": 3,
    "mcp-servers/mcp_servers/performance/tools/performance_summary.py": 3,
    "mcp-servers/mcp_servers/simulation/tools/capacity_cost.py": 3,
    "mcp-servers/mcp_servers/simulation/tools/rds_rightsizing.py": 1,
}


_IMPORT_LINE = _re.compile(r"^\s*(from\s+\S+\s+)?import\s")


def _code_only(text):
    """Drop import lines, comments and alias-assignment lines before counting.

    Cross-model review (Codex) caught this: counting the bare symbol anywhere
    also counts `from ... import CLUSTER_LEVEL_ONLY`, an alias assignment and
    any comment that merely NAMES the constant. The census is supposed to count
    QUERY PREDICATES, so with those included someone could delete a real SQL
    predicate, add a comment mentioning the constant, and keep the total stable,
    which is exactly the deletion this guard exists to catch."""
    kept = []
    for line in text.splitlines():
        if _IMPORT_LINE.match(line):
            continue
        line = _re.sub(r"#.*$", "", line)
        if _ALIAS_ASSIGN.match(line):
            continue
        if line.strip():
            kept.append(line)
    return "\n".join(kept)


def _count_strict(text):
    code = _code_only(text)
    return (
        code.count(_STRICT_LITERAL)
        + len(_ALIASED_STRICT.findall(code))
        + len(_CONST_USE.findall(code))
    )


def test_every_known_cluster_level_reader_still_carries_its_filters():
    """Census guard: catches DELETION, which the weak-literal grep cannot."""
    root = _pathlib.Path(_ROOT)
    drift = []
    for rel, expected in sorted(_EXPECTED_CLUSTER_LEVEL_PREDICATES.items()):
        path = root / rel
        assert path.exists(), f"reader moved or was deleted: {rel}"
        found = _count_strict(path.read_text(encoding="utf-8"))
        if found != expected:
            drift.append(f"{rel}: expected {expected} cluster-level predicates, found {found}")
    assert not drift, (
        "cluster-level dimension filter census changed:\n  "
        + "\n  ".join(drift)
        + "\n\nIf you REMOVED a query, lower the number. If you ADDED a cluster-level "
        "query, raise it and add a mixed-row result test for the new reader. If a "
        "filter went missing, restore it: an unfiltered aggregate mixes a cluster "
        "total with its per-instance / per-wait-event / per-GSI rows and returns a "
        "silently wrong number."
    )


def test_the_census_covers_every_file_that_reads_metric_snapshots():
    """A new reader file must not slip in unrecorded.

    Detection is `FROM metric_snapshots`, not the bare table name: every
    collector INSERTs into the table and would otherwise be flagged. Files that
    touch the table without aggregating a VALUE (freshness MAX(ts), row counts,
    the retention purge) are listed as dimension-agnostic with the reason."""
    root = _pathlib.Path(_ROOT)
    reads_from = _re.compile(r"FROM\s+metric_snapshots", _re.I)
    dimension_agnostic = {
        # per-rule MAX(ts) freshness only, no value aggregate
        "api/alerts/handler.py",
        # ETL freshness MAX(ts) + total row count
        "api/clusters/handler.py",
        # 90-day retention purge (DELETE), not a read
        "data-pipeline/etl_collector/handler.py",
    }
    unrecorded = []
    for sub in ("api", "mcp-servers/mcp_servers", "data-pipeline"):
        for path in (root / sub).rglob("*.py"):
            rel = str(path.relative_to(root))
            if "__pycache__" in rel or rel in dimension_agnostic:
                continue
            if not reads_from.search(path.read_text(encoding="utf-8")):
                continue
            if rel not in _EXPECTED_CLUSTER_LEVEL_PREDICATES:
                unrecorded.append(rel)
    assert not unrecorded, (
        "these files SELECT from metric_snapshots but are not in the census: "
        + ", ".join(sorted(unrecorded))
        + "\nAdd them with their cluster-level predicate count, or list them as "
        "dimension-agnostic with a one-line reason."
    )

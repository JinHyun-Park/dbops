"""Failure-scenario demo runner.

The behaviours here are the ones whose failure is SILENT in a demo:

  - the bookkeeping event must use a source diagnose_root_cause excludes, or the RCA's
    top-ranked root cause is the row announcing the scenario
  - the RCA must be enqueued with dedupe off and anchored on the injection, or the
    second scenario of a demo returns no report and the first diagnoses the recovery
  - injected metric samples must not land on the ETL's own minute boundary, or the
    UNIQUE index drops them via ON CONFLICT DO NOTHING and the report has no metric
    evidence
  - the one-at-a-time lock must expire on elapsed time, not on a status
  - the purge must delete only the rows the manifest names
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
_DIR = ROOT / "api" / "scenarios"
sys.path.insert(0, str(_DIR))
sys.path.insert(0, str(ROOT / "mcp-servers"))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
os.environ.setdefault("SCENARIO_CLUSTER_ID", "pgtsd-demo-aurora-pg")

_spec = importlib.util.spec_from_file_location("scenarios_handler", _DIR / "handler.py")
handler = importlib.util.module_from_spec(_spec)
sys.modules["scenarios_handler"] = handler
_spec.loader.exec_module(handler)

CID = "pgtsd-demo-aurora-pg"


class FakeDB:
    """Records every statement and answers the reads the runner makes.

    Reads are matched on a SQL fragment, so a statement the runner gains later comes
    back empty rather than silently reusing another read's rows.
    """

    def __init__(self, *, engine="aurora-postgresql", active=None, stale_runs=()):
        self.calls = []
        self.engine = engine
        self.active = active
        self.stale_runs = list(stale_runs)

    def __call__(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if "FROM cluster_meta" in sql:
            return [{"engine": self.engine}]
        if "FROM scenario_runs" in sql and "resolved_at IS NULL AND started_at <" in sql:
            return self.stale_runs
        if "FROM scenario_runs" in sql and "started_at > NOW()" in sql:
            return [self.active] if self.active else []
        if "FROM scenario_runs" in sql:
            return []
        if "RETURNING id" in sql:
            return [{"id": 42}]
        if "FROM schema_snapshots" in sql:
            return [{"read_scope": "dbops/16384"}]
        return []

    def written(self, table):
        return [(s, p) for s, p in self.calls if f"INSERT INTO {table}" in s]

    def deleted(self, table):
        return [(s, p) for s, p in self.calls if f"DELETE FROM {table}" in s]


def _event(method="POST", path="/api/scenarios/cpu_saturation/run", scenario="cpu_saturation"):
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"email": "demo@dbops.dev"}}},
        },
        "pathParameters": {"id": scenario},
    }


def _run(db, scenario="cpu_saturation", enqueue_returns="task-1"):
    handler.enqueue_auto_rca = MagicMock(return_value=enqueue_returns)
    resp = handler._run(db, _event(scenario=scenario), scenario, CID)
    return resp, json.loads(resp["body"])


# ---------------------------------------------------------------------------
# The self-reference trap
# ---------------------------------------------------------------------------

def test_the_bookkeeping_event_uses_a_source_the_rca_excludes():
    """Otherwise the RCA's #1 root cause is the row announcing the scenario.

    The bookkeeping row is written at the anchor, which is where the recency factor is
    at its maximum, and `event` carries base weight 4.0 against metric_spike's 2.0. So
    under any non-excluded source it outranks the symptoms the scenario just injected.
    This is the exact defect alert_evaluator's own `alert` row caused in the one real
    auto-RCA this deployment produced: rank 1, score 3.6.
    """
    from mcp_servers.incident.tools.diagnose_root_cause import SELF_EVENT_SOURCES

    db = FakeDB()
    _run(db)
    bookkeeping = [
        p for s, p in db.written("event_log") if "dbops-scenario-runner" in s
    ]
    assert len(bookkeeping) == 1, "the run must write exactly one bookkeeping row"
    assert "dbops-scenario-runner" in SELF_EVENT_SOURCES, (
        "the runner's source is not in diagnose_root_cause.SELF_EVENT_SOURCES, so the "
        "RCA will rank the scenario announcement as the incident's cause"
    )


def test_the_failover_symptom_event_is_not_an_excluded_source():
    """The symptom must be rankable. Excluding it would leave an empty report.

    The mirror of the test above, and the reason the two kinds of row cannot share a
    source: one is DBOps talking about itself, the other is the incident. Asserted on
    failover because that is the only scenario whose signal IS an event, see
    test_only_the_failover_scenario_writes_a_symptom_event.
    """
    from mcp_servers.incident.tools.diagnose_root_cause import SELF_EVENT_SOURCES

    db = FakeDB()
    _run(db, scenario="failover")
    symptom_sources = {
        p["src"] for s, p in db.written("event_log") if "src" in p
    }
    assert symptom_sources, "the failover scenario wrote no symptom event"
    assert not (symptom_sources & set(SELF_EVENT_SOURCES)), (
        f"symptom events use excluded sources {symptom_sources & set(SELF_EVENT_SOURCES)}"
    )


def test_only_the_failover_scenario_writes_a_symptom_event():
    """A companion event row outranks the signal it accompanies.

    `event` carries base weight 4.0 against metric_spike's 2.0 and slow_query's 2.0,
    and a `critical` severity multiplies it by 1.5 again. MEASURED against the real
    ranker on a real PostgreSQL, with a CloudWatch alarm row injected beside the CPU
    series: event 5.58 vs metric_spike 2.60, so the top-ranked root cause of a CPU
    incident was "a CPU alarm fired". That explains nothing. Only the scenario whose
    signal genuinely IS an engine event may write one.
    """
    for scenario in handler._BY_ID:
        db = FakeDB()
        _run(db, scenario=scenario)
        symptom = [p for s, p in db.written("event_log") if "src" in p]
        if scenario == "failover":
            assert symptom, "failover must write its event: it has no other signal"
        else:
            assert not symptom, (
                f"{scenario} writes a companion event row, which will outrank its own "
                f"{handler._BY_ID[scenario]['category']} signal: {symptom}"
            )


# ---------------------------------------------------------------------------
# Enqueue contract
# ---------------------------------------------------------------------------

def test_the_rca_is_enqueued_anchored_on_the_injection_with_dedupe_off():
    db = FakeDB()
    resp, body = _run(db)
    assert resp["statusCode"] == 202
    kwargs = handler.enqueue_auto_rca.call_args.kwargs
    assert kwargs["dedupe"] is False, (
        "with dedupe on, the second scenario inside 15 minutes returns None and the "
        "demo silently produces no report"
    )
    assert kwargs["observed_at"] == body["anchor_at"], (
        "without the anchor the worker diagnoses around EXECUTION time, which is a "
        "different window from the one the signals were written into"
    )
    assert kwargs["trigger"] == "scenario:cpu_saturation"
    assert body["task_id"] == "task-1"
    assert body["rca_enqueued"] is True


def test_a_failed_enqueue_is_reported_rather_than_implied():
    """The signals are written either way, so a 202 alone would promise a report.

    enqueue_auto_rca returns None when AGENT_TASKS_TABLE is unset or DynamoDB refuses,
    and it never raises. Reporting rca_enqueued False is what lets the UI say "signals
    injected, no RCA queued" instead of spinning forever.
    """
    db = FakeDB()
    resp, body = _run(db, enqueue_returns=None)
    assert resp["statusCode"] == 202
    assert body["rca_enqueued"] is False
    assert body["task_id"] is None
    assert body["signals_written"], "signals must still be recorded"


# ---------------------------------------------------------------------------
# Metric injection shape
# ---------------------------------------------------------------------------

def test_metric_samples_avoid_the_collectors_minute_boundary():
    """metric_snapshots is UNIQUE on (cluster_id, ts, metric_type, md5(dimensions)).

    The ETL writes on the minute. An injected sample on the same second collides with a
    collected one and loses to ON CONFLICT DO NOTHING, which is silent: the insert
    "succeeds", the row is absent, and the RCA reports no metric evidence for a metric
    incident.
    """
    db = FakeDB()
    _run(db)
    stamps = [p["ts"] for s, p in db.written("metric_snapshots")]
    assert stamps, "no metric samples were written"
    seconds = {s.split("T")[1][6:8] for s in stamps}
    assert seconds == {f"{handler.INJECT_SECOND:02d}"}, (
        f"samples landed on seconds {seconds}; the ETL grid is :00"
    )


def test_the_metric_window_gets_more_than_one_elevated_sample():
    """One elevated sample is indistinguishable from a corrupt reading.

    The ranker discounts a peak carried by a single sample (LONE_PEAK_CONFIDENCE) and
    labels it `lone_sample`, which is correct behaviour and a terrible demo: the report
    reads as a false positive. The scenario therefore writes a run of elevated samples.
    """
    db = FakeDB()
    _run(db)
    values = [p["val"] for s, p in db.written("metric_snapshots")]
    baseline_like = [v for v in values if v < 50]
    elevated = [v for v in values if v >= 50]
    assert len(elevated) >= 2, f"only {len(elevated)} elevated sample(s): {values}"
    assert len(baseline_like) >= 2, (
        "the prior window needs its own samples: a ratio against whatever the "
        "collectors happened to store is not reproducible between rehearsals"
    )


def test_sample_times_respect_the_window_boundary_at_every_anchor_second():
    """Exhaustive, because the defect was seconds-dependent.

    Pinning the seconds to INJECT_SECOND moves a timestamp forward whenever the
    anchor's own seconds are lower, which pushed the newest baseline sample inside the
    RCA window. A test that runs at whatever second the clock happens to hold catches
    that 17 times in 60. So walk all 60 and assert the two halves stay in their own
    windows: baseline in [anchor-60m, anchor-30m), elevated in [anchor-30m, anchor).
    """
    for second in range(60):
        anchor = datetime(2026, 9, 15, 12, 30, second, tzinfo=timezone.utc)
        boundary = anchor - timedelta(minutes=handler.WINDOW_MINUTES)
        baseline_start = anchor - timedelta(minutes=2 * handler.WINDOW_MINUTES)
        for ts in handler._sample_times(anchor, 6, offset_minutes=handler.WINDOW_MINUTES):
            assert baseline_start <= ts < boundary, f"second={second} baseline {ts}"
        for ts in handler._sample_times(anchor, 4, offset_minutes=2):
            assert boundary <= ts < anchor, f"second={second} elevated {ts}"


def test_the_baseline_half_lands_before_the_rca_window():
    """A baseline sample inside the RCA window raises window_avg instead of the ratio.

    diagnose_root_cause takes the WINDOW_MINUTES before the window as the baseline, so
    the low samples must be older than that boundary and the high ones newer.
    """
    db = FakeDB()
    resp, body = _run(db)
    anchor = datetime.strptime(body["anchor_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    boundary = anchor - timedelta(minutes=handler.WINDOW_MINUTES)
    for _sql, p in db.written("metric_snapshots"):
        ts = datetime.strptime(p["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if p["val"] >= 50:
            assert ts >= boundary, f"elevated sample {p['ts']} is older than the window"
        else:
            assert ts < boundary, f"baseline sample {p['ts']} is inside the window"


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def test_a_second_scenario_is_refused_while_one_holds_the_lock():
    active = {"id": 7, "scenario_id": "lock_contention",
              "started_at": "2026-09-15T01:00:00Z", "task_id": "task-0"}
    db = FakeDB(active=active)
    handler.enqueue_auto_rca = MagicMock(return_value="task-9")
    resp = handler._run(db, _event(), "cpu_saturation", CID)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 409
    assert body["running"]["scenario_id"] == "lock_contention"
    assert handler.enqueue_auto_rca.call_count == 0
    assert db.written("metric_snapshots") == [], "signals were injected despite the lock"


def test_the_lock_expires_on_elapsed_time_not_on_status():
    """A Lambda that dies mid-injection never writes a terminal status.

    If the guard read only `status`, that run would hold the lock forever and every
    later scenario would 409. The predicate has to bound started_at.
    """
    db = FakeDB()
    handler._active_run(db, CID)
    sql = next(s for s, p in db.calls if "FROM scenario_runs" in s and "NOW()" in s)
    assert "started_at > NOW() -" in sql, (
        "the lock query does not bound started_at, so a crashed run wedges it"
    )
    assert "status =" not in sql, (
        "the lock must not key on status. The writer flips `running` to `injected` "
        "milliseconds after the last INSERT, so a status-keyed lock is held for the "
        "injection and nothing else: the next scenario starts immediately, into a "
        "window still full of this one's signals."
    )


def test_the_schema_scenario_refuses_a_mysql_cluster():
    """schema_snapshots is PostgreSQL-only BY DECISION, not by gap.

    On MySQL diagnose_root_cause returns `unsupported_engine` for that source, so the
    button would inject rows and then produce a report with no schema evidence and no
    stated reason. Refusing up front says why.
    """
    db = FakeDB(engine="aurora-mysql")
    handler.enqueue_auto_rca = MagicMock(return_value="t")
    resp = handler._run(db, _event(scenario="schema_change"), "schema_change", CID)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 409
    assert "PostgreSQL" in body["error"]
    assert db.written("schema_snapshots") == []
    assert handler.enqueue_auto_rca.call_count == 0


def test_the_schema_scenario_runs_on_postgres():
    """Negative control for the gate above: without it, a gate that refuses everything
    passes that test."""
    db = FakeDB(engine="aurora-postgresql")
    resp, body = _run(db, scenario="schema_change")
    assert resp["statusCode"] == 202
    written = db.written("schema_snapshots")
    assert len(written) == 1
    diff = json.loads(written[0][1]["diff"])
    assert diff["modified"], "the stored diff must be non-empty or the RCA skips the row"
    assert written[0][1]["scope"] is None, (
        "read_scope must stay NULL: it records WHICH CATALOG A READ REACHED, and no "
        "read reached anything here. Copying a real row's scope onto a synthetic row "
        "fabricates provenance, and reading schema_snapshots to get it also violates "
        "the selection contract (test_schema_snapshot_parity)."
    )


def test_an_unknown_scenario_is_a_404_and_writes_nothing():
    db = FakeDB()
    handler.enqueue_auto_rca = MagicMock(return_value="t")
    resp = handler._run(db, _event(scenario="drop_everything"), "drop_everything", CID)
    assert resp["statusCode"] == 404
    assert db.calls == [], "an unknown scenario touched the database"
    assert handler.enqueue_auto_rca.call_count == 0


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

def test_the_purge_deletes_only_the_rows_the_manifest_names():
    stale = [{
        "id": 3,
        "injected_json": json.dumps([
            {"table": "metric_snapshots", "times": ["2026-09-15T00:00:17Z"], "metric_type": "cpu"},
            {"table": "event_log", "times": ["2026-09-15T00:05:00Z"]},
        ]),
    }]
    db = FakeDB(stale_runs=stale)
    assert handler._purge_old_runs(db, CID) == 1
    metric_delete = db.deleted("metric_snapshots")
    assert len(metric_delete) == 1
    sql, params = metric_delete[0]
    # The cast is part of the contract, not incidental: the Data API binds these as
    # typed text and PostgreSQL refuses `timestamptz = text` (SQLState 42883), which
    # made every live POST a 500 until the cast was added.
    assert "ts IN (:t0::timestamptz)" in sql and "metric_type = :mt" in sql
    assert params["t0"] == "2026-09-15T00:00:17Z" and params["mt"] == "cpu"
    # Two event_log deletes: the manifest's own bookkeeping row, and the anomaly row
    # the real detector derived from the cpu samples above (see
    # test_the_anomaly_the_detector_derived_is_purged_with_its_samples).
    event_deletes = db.deleted("event_log")
    assert len(event_deletes) == 2, [s for s, _ in event_deletes]
    assert any("SET status = 'resolved'" in s for s, p in db.calls)


def test_the_purge_ignores_a_table_outside_the_allowlist():
    """Table and column names are interpolated, so the set has to be closed.

    A manifest is read back out of the database. Nothing today can put an arbitrary
    table name in one, and that is exactly the assumption that stops being true the
    first time something else writes a manifest.
    """
    stale = [{
        "id": 4,
        "injected_json": json.dumps([
            {"table": "cluster_meta", "times": ["2026-09-15T00:00:17Z"]},
            {"table": "clusters; DROP TABLE event_log", "times": ["2026-09-15T00:00:17Z"]},
        ]),
    }]
    db = FakeDB(stale_runs=stale)
    handler._purge_old_runs(db, CID)
    assert not any("DELETE FROM" in s for s, p in db.calls), (
        "the purge issued a DELETE against a table outside _PURGEABLE"
    )


def test_a_malformed_manifest_does_not_break_the_purge():
    db = FakeDB(stale_runs=[{"id": 5, "injected_json": "not json"}])
    assert handler._purge_old_runs(db, CID) == 1
    assert any("SET status = 'resolved'" in s for s, p in db.calls)


# ---------------------------------------------------------------------------
# Catalog / routing
# ---------------------------------------------------------------------------

def test_the_catalog_lists_every_scenario_with_its_ranking_weight():
    resp = handler.lambda_handler(
        {"requestContext": {"http": {"method": "GET", "path": "/api/scenarios"}}}, None
    )
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["enabled"] is True
    ids = {s["id"] for s in body["scenarios"]}
    assert ids == set(handler._BY_ID)
    for s in body["scenarios"]:
        assert s["category"] and s["weight"] > 0
        assert "engines" not in s, "engines is an internal gate, not UI copy"


def test_the_catalog_covers_more_than_one_ranking_category():
    """A row of buttons that all produce the same category is one scenario with five
    names: every report would rank its cause identically."""
    categories = {s["category"] for s in handler.SCENARIOS}
    assert len(categories) >= 4, f"only {categories} covered"


def test_the_catalog_answers_even_when_no_cluster_is_configured(monkeypatch):
    """The UI needs the list to render the disabled state and say why."""
    monkeypatch.setenv("SCENARIO_CLUSTER_ID", "")
    resp = handler.lambda_handler(
        {"requestContext": {"http": {"method": "GET", "path": "/api/scenarios"}}}, None
    )
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["enabled"] is False
    assert body["scenarios"], "the catalog is static and must still be listed"


def test_a_post_without_a_configured_cluster_is_refused(monkeypatch):
    monkeypatch.setenv("SCENARIO_CLUSTER_ID", "")
    resp = handler.lambda_handler(_event(), None)
    assert resp["statusCode"] == 503
    assert "SCENARIO_CLUSTER_ID" in json.loads(resp["body"])["error"]


def test_the_error_path_does_not_leak_sql(monkeypatch):
    """A Data API error quotes the failing statement, table and column names included.

    api/ responses must not carry str(e); three handlers leaked it before.
    """
    boom = MagicMock(side_effect=RuntimeError(
        "ERROR: relation \"scenario_runs\" does not exist; SQL: INSERT INTO scenario_runs"
    ))
    monkeypatch.setattr(handler, "_make_query", lambda *a, **k: boom)
    monkeypatch.setattr(handler.boto3, "client", lambda *a, **k: MagicMock())
    resp = handler.lambda_handler(_event(), None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 500
    assert "scenario_runs" not in body["error"]
    assert "SQL" not in body["error"]


# ---------------------------------------------------------------------------
# _manifest_of: the shape the driver hands back
# ---------------------------------------------------------------------------

def test_the_manifest_is_read_from_either_driver_shape():
    """jsonb arrives as a STRING from the Data API and as a list from row_to_json.

    The obvious `json.loads(injected_json)` is correct for one of them and raises
    TypeError on the other, and the `except` that used to wrap it returned [] so the
    purge deleted nothing and still reported success. Caught by the real-engine test;
    pinned here per shape so each branch has to work.
    """
    entry = [{"table": "event_log", "times": ["2026-09-15T00:00:00Z"]}]
    assert handler._manifest_of({"injected_json": json.dumps(entry)}) == entry
    assert handler._manifest_of({"injected_json": entry}) == entry


def test_an_unreadable_manifest_reads_as_empty_and_says_so(capsys):
    """An empty manifest and an unreadable one lead to the same deletes, so the log
    line is the only thing that distinguishes "this run injected nothing" from "this
    run's rows are now orphaned"."""
    assert handler._manifest_of({"injected_json": None}) == []
    assert handler._manifest_of({"injected_json": ""}) == []
    assert capsys.readouterr().out == "", "an absent manifest is not an error"

    assert handler._manifest_of({"id": 7, "injected_json": "not json"}) == []
    out = capsys.readouterr().out
    assert "7" in out and "unreadable" in out, out

    # A JSON scalar parses but is not a manifest, and iterating it would raise.
    assert handler._manifest_of({"id": 8, "injected_json": "42"}) == []


# ---------------------------------------------------------------------------
# Derived rows: what the real detector concluded from fabricated samples
# ---------------------------------------------------------------------------

def test_the_anomaly_the_detector_derived_is_purged_with_its_samples():
    """proactive_monitor reads fabricated samples exactly as collected ones.

    MEASURED LIVE on 2026-09-15 running the six scenarios in sequence: the cpu
    scenario's injected samples produced a REAL `anomaly_cpu` row at critical
    severity (`Current: 19.00, baseline: 5.19, z-score: 9.6`), written by a different
    Lambda and therefore in no manifest. It survived every purge, appeared in five
    consecutive reports over twenty minutes, and took FIRST PLACE in two of them:
    `event` is base weight 4.0 against metric_spike's and slow_query's 2.0, and
    critical multiplies by 1.5 again.

    It is deleted because it is a conclusion ABOUT rows that no longer exist, not
    because it is inconvenient. Scoped to this cluster, this metric, and this run's
    span plus the detector's lag.
    """
    stale = [{
        "id": 9,
        "injected_json": json.dumps([
            {"table": "metric_snapshots",
             "times": ["2026-09-15T00:00:17Z", "2026-09-15T00:05:17Z"],
             "metric_type": "cpu"},
        ]),
    }]
    db = FakeDB(stale_runs=stale)
    handler._purge_old_runs(db, CID)
    derived = [(s, p) for s, p in db.deleted("event_log") if "dbops-monitor" in s]
    assert len(derived) == 1, db.deleted("event_log")
    sql, params = derived[0]
    assert params["etype"] == "anomaly_cpu", params
    assert params["from_ts"] == "2026-09-15T00:00:17Z"
    # Newest sample plus the detector lag, which is when a derived row can still land.
    assert params["to_ts"] == "2026-09-15T00:25:17Z", params
    assert "event_type = :etype" in sql and "source = 'dbops-monitor'" in sql
    # Bounded on BOTH sides: an unbounded delete would take real anomalies from
    # before the scenario ever ran.
    assert "event_time >= :from_ts::timestamptz" in sql
    assert "event_time < :to_ts::timestamptz" in sql


def test_a_non_metric_manifest_entry_derives_no_anomaly_delete():
    """Only metric_snapshots entries feed the anomaly detector, so only they may
    trigger this delete. Without the guard, a lock or schema scenario would issue an
    `anomaly_None` delete against real rows."""
    stale = [{
        "id": 10,
        "injected_json": json.dumps([
            {"table": "blocking_locks", "times": ["2026-09-15T00:03:00Z"]},
            {"table": "schema_snapshots", "times": ["2026-09-15T00:02:00Z"],
             "schema_name": "public"},
        ]),
    }]
    db = FakeDB(stale_runs=stale)
    handler._purge_old_runs(db, CID)
    assert not [s for s, _ in db.deleted("event_log") if "dbops-monitor" in s], (
        "a non-metric scenario issued an anomaly delete"
    )


def test_only_a_metric_snapshots_entry_derives_an_anomaly_delete():
    """The table check is separate from the metric_type check on purpose.

    No manifest entry today carries a metric_type for anything but
    metric_snapshots, so `not metric` alone happens to be sufficient and a mutation
    removing the table check passed. Manifests are data read back out of the
    database, and the entry that eventually carries a metric_type for a different
    table would issue this DELETE against event_log rows it has no claim on. Only
    metric_snapshots feeds the anomaly detector.
    """
    stale = [{
        "id": 12,
        "injected_json": json.dumps([
            {"table": "query_stats", "times": ["2026-09-15T00:03:00Z"], "metric_type": "cpu"},
        ]),
    }]
    db = FakeDB(stale_runs=stale)
    handler._purge_old_runs(db, CID)
    assert not [s for s, _ in db.deleted("event_log") if "dbops-monitor" in s], (
        "a non-metric_snapshots entry issued an anomaly delete"
    )


def test_a_malformed_manifest_timestamp_does_not_break_the_purge():
    """The manifest is read back out of the database, so a value this module did not
    write must not raise inside the purge: that would 500 the POST that called it."""
    stale = [{
        "id": 11,
        "injected_json": json.dumps([
            {"table": "metric_snapshots", "times": ["not-a-timestamp"], "metric_type": "cpu"},
        ]),
    }]
    db = FakeDB(stale_runs=stale)
    assert handler._purge_old_runs(db, CID) == 1
    assert not [s for s, _ in db.deleted("event_log") if "dbops-monitor" in s]

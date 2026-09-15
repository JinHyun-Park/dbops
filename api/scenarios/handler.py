"""Failure-scenario demo runner: inject a realistic incident, then let the real
RCA explain it.

WHAT IT DOES. Each scenario writes a burst of signal rows into the cache tables
``diagnose_root_cause`` already reads, then enqueues an auto-RCA anchored on that
burst. From there nothing is simulated: the same deterministic ranker scores the same
signal categories with the same weights, and the same single Bedrock call writes the
Korean narrative and the recommendations. A presenter gets a real report about a
synthetic symptom.

WHAT IT DOES NOT DO, and why. It never touches a target database. The registered
clusters are permanent read-only fixtures shared by every other demo, so a button that
could saturate a real instance's CPU or hold a real lock is not a demo, it is an
outage with a nice UI. Injecting the OBSERVATIONS instead exercises every stage of the
pipeline that a viewer can actually see, and the one stage it skips (the collectors)
is the stage that has nothing to do with root-cause analysis.

THE SELF-REFERENCE TRAP. The bookkeeping row this runner writes to ``event_log`` uses
source ``dbops-scenario-runner``, which ``diagnose_root_cause.SELF_EVENT_SOURCES``
excludes. Without that, the row saying "a CPU scenario was started" lands 1-2 seconds
before the anchor, exactly where the recency factor peaks, and the RCA's top-ranked
root cause becomes the announcement of the incident rather than the incident. That is
not hypothetical: it is precisely what alert_evaluator's own ``alert`` row did to the
one real auto-RCA this deployment has produced (rank 1, score 3.6). The injected
SYMPTOM rows deliberately carry realistic sources instead, because they ARE the thing
under investigation.

AUTHORIZATION. Ordinary authenticated users, not just admins: the demo is meant to be
clickable by whoever is watching. The route keeps the API's default JWT authorizer
rather than going public, because this endpoint writes rows into the shared cache
database, and "anyone in the room" and "anyone on the internet" are different
permissions.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
from task_enqueue import enqueue_auto_rca

# The RCA's own defaults, which the injected timestamps have to land inside:
# diagnose_root_cause anchors on `observed_at`, looks back WINDOW_MINUTES, and takes
# the WINDOW_MINUTES before that as the baseline a ratio is measured against.
WINDOW_MINUTES = 30
# Injected samples are spaced like the ETL's own cadence so a chart of the window
# looks like a collected series rather than a single spike glued on.
SAMPLE_INTERVAL_MINUTES = 5
# Seconds past the minute for every injected metric sample. metric_snapshots carries a
# UNIQUE index on (cluster_id, ts, metric_type, md5(dimensions)), and the collectors
# write on the minute, so writing on the minute too means a real sample and an injected
# one collide and one of them silently loses to ON CONFLICT DO NOTHING.
INJECT_SECOND = 17
# How long one run owns the cluster: its injection, its RCA, and the reading of its
# report. A second scenario inside this window is REFUSED rather than queued, because
# its analysis window would still contain the previous scenario's signals and the
# report would explain the previous button (measured: lock_contention followed by
# slow_query_surge ranked `blocking 5.58` above every slow query). Once it expires the
# next run purges the predecessor's rows, so this is also the grace period an RCA has
# to finish reading them.
#
# Two minutes, not six: the worker's RCA takes seconds, and every extra minute here is
# a presenter standing in front of an audience waiting for a button to re-arm.
LOCK_MINUTES = 2

# Tables this runner is allowed to write to and purge from. Table and column names are
# interpolated into SQL (the Data API binds values, never identifiers), so the set is
# closed and checked rather than trusted.
_PURGEABLE = {
    "metric_snapshots": "ts",
    "event_log": "event_time",
    "blocking_locks": "snapshot_time",
    "query_stats": "snapshot_time",
    "schema_snapshots": "snapshot_time",
}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _cors():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _response(status, body):
    return {"statusCode": status, "headers": _cors(), "body": json.dumps(body, default=str)}


def _make_query(rds_data, cluster_arn, secret_arn, database):
    """RDS Data API caller returning name-keyed rows.

    `includeResultMetadata=True` is not optional: without it the response carries no
    columnMetadata, so every name-keyed row comes back an empty dict and every caller
    reads None. That was a latent bug in api/simulation for months.
    """
    def query(sql, params=None):
        sql_params = []
        for k, v in (params or {}).items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            elif v is None:
                sql_params.append({"name": k, "value": {"isNull": True}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=f"/* source=dbops-scenario-runner */ {sql}",
            parameters=sql_params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        rows = []
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                if f.get("isNull"):
                    row[col] = None
                    continue
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows
    return query


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sample_times(anchor: datetime, count: int, *, offset_minutes: int):
    """`count` timestamps at or before `anchor - offset_minutes`, ETL-spaced.

    Returned oldest-first so a caller can pair them with a rising series.

    Pinning the seconds to INJECT_SECOND can move a timestamp FORWARD, and for the
    baseline series `offset_minutes` IS the window boundary: with an anchor at :03:10
    the newest baseline sample landed at :03:17, seven seconds inside the RCA window,
    where a baseline-level value raises window_avg instead of the ratio it is supposed
    to be measured against. So step back a minute whenever the pin overshoots. It fires
    for any anchor whose seconds are below INJECT_SECOND, which is most of them.
    """
    target = anchor - timedelta(minutes=offset_minutes)
    newest = target.replace(second=INJECT_SECOND, microsecond=0)
    if newest >= target:
        newest -= timedelta(minutes=1)
    return [
        newest - timedelta(minutes=SAMPLE_INTERVAL_MINUTES * i)
        for i in reversed(range(count))
    ]


# ---------------------------------------------------------------------------
# Signal writers. Each returns a manifest entry list: what was written, where,
# and the timestamps that identify it, so the purge is an explicit delete of
# known rows instead of a pattern match against collected data.
# ---------------------------------------------------------------------------

def _write_metric_series(query, cluster_id, anchor, metric_type, baseline, elevated):
    """A baseline series in the prior window, an elevated series in the RCA window.

    Both halves are injected rather than leaning on whatever the collectors happen to
    have stored, so the ratio the RCA computes is the same every time the button is
    pressed. A demo whose numbers depend on how busy the cluster was ten minutes ago is
    not a demo you can rehearse.
    """
    written = []
    # Baseline: the window BEFORE the RCA window (offset one full window).
    base_times = _sample_times(anchor, 6, offset_minutes=WINDOW_MINUTES)
    for i, ts in enumerate(base_times):
        value = baseline * (1.0 + 0.04 * (i % 3 - 1))  # gentle, non-flat jitter
        query(
            "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
            "VALUES (:cid, :ts::timestamptz, :mt, :val, '{}'::jsonb) "
            "ON CONFLICT DO NOTHING",
            {"cid": cluster_id, "ts": _iso(ts), "mt": metric_type, "val": float(round(value, 3))},
        )
        written.append(_iso(ts))
    # Elevated: inside the RCA window, four consecutive samples. Four rather than one
    # on purpose. A single elevated sample is exactly what a corrupt reading looks
    # like, so the ranker discounts it as a lone peak and labels it as such, which is
    # correct but makes the demo look like a false positive rather than an incident.
    hot_times = _sample_times(anchor, 4, offset_minutes=2)
    for i, ts in enumerate(hot_times):
        value = elevated * (0.97 + 0.01 * i)
        query(
            "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
            "VALUES (:cid, :ts::timestamptz, :mt, :val, '{}'::jsonb) "
            "ON CONFLICT DO NOTHING",
            {"cid": cluster_id, "ts": _iso(ts), "mt": metric_type, "val": float(round(value, 3))},
        )
        written.append(_iso(ts))
    return [{"table": "metric_snapshots", "times": written, "metric_type": metric_type}]


def _write_event(query, cluster_id, anchor, *, event_type, source, severity, message,
                 minutes_before=3):
    ts = _iso(anchor - timedelta(minutes=minutes_before))
    query(
        "INSERT INTO event_log (cluster_id, event_time, event_type, source, message, severity) "
        "VALUES (:cid, :ts::timestamptz, :et, :src, :msg, :sev)",
        {"cid": cluster_id, "ts": ts, "et": event_type, "src": source,
         "msg": message, "sev": severity},
    )
    return [{"table": "event_log", "times": [ts]}]


def _write_blocking(query, cluster_id, anchor):
    ts = _iso(anchor - timedelta(minutes=3))
    holders = [
        ("UPDATE orders SET status = 'shipped' WHERE id = $1",
         "SELECT * FROM orders WHERE id = $1 FOR UPDATE", 184.6),
        ("UPDATE orders SET status = 'shipped' WHERE id = $1",
         "UPDATE order_items SET qty = qty - 1 WHERE order_id = $1", 171.2),
        ("UPDATE orders SET status = 'shipped' WHERE id = $1",
         "DELETE FROM order_items WHERE order_id = $1", 96.4),
    ]
    for i, (blocking_q, blocked_q, waited) in enumerate(holders):
        query(
            "INSERT INTO blocking_locks (cluster_id, snapshot_time, blocked_pid, blocked_user, "
            " blocking_pid, blocking_user, blocked_query, blocking_query, locktype, "
            " blocked_mode, blocking_mode, relation, blocked_duration_sec) "
            "VALUES (:cid, :ts::timestamptz, :bpid, 'app_rw', :kpid, 'batch_rw', :bq, :kq, "
            " 'transactionid', 'ShareLock', 'ExclusiveLock', 'orders', :waited)",
            {"cid": cluster_id, "ts": ts, "bpid": 20100 + i, "kpid": 20099,
             "bq": blocked_q, "kq": blocking_q, "waited": waited},
        )
    return [{"table": "blocking_locks", "times": [ts]}]


def _write_slow_queries(query, cluster_id, anchor):
    ts = _iso(anchor - timedelta(minutes=4))
    rows = [
        ("a1b2c3d4e5f60718", "SELECT o.*, c.name FROM orders o JOIN customers c ON c.id = "
         "o.customer_id WHERE o.created_at > $1 ORDER BY o.created_at DESC", 1840, 214000.0),
        ("b2c3d4e5f6071829", "SELECT COUNT(*) FROM order_items WHERE order_id IN "
         "(SELECT id FROM orders WHERE status = $1)", 920, 86400.0),
        ("c3d4e5f607182930", "UPDATE inventory SET reserved = reserved + $1 WHERE sku = $2",
         4100, 41000.0),
    ]
    for qhash, qtext, calls, total_ms in rows:
        query(
            "INSERT INTO query_stats (cluster_id, snapshot_time, query_hash, query_text, "
            " calls, total_time_ms, mean_time_ms, rows_returned, shared_blks_hit, shared_blks_read) "
            "VALUES (:cid, :ts::timestamptz, :qh, :qt, :calls, :total, :mean, :rows, :hit, :read)",
            {"cid": cluster_id, "ts": ts, "qh": qhash, "qt": qtext, "calls": calls,
             "total": total_ms, "mean": round(total_ms / calls, 3),
             "rows": calls * 12, "hit": calls * 300, "read": calls * 140},
        )
    return [{"table": "query_stats", "times": [ts]}]


def _write_schema_change(query, cluster_id, anchor):
    """One snapshot carrying a non-empty stored diff.

    The RCA replays STORED diffs (`diff_from_previous_json`) under ALL_ROWS, which
    applies no scope filter, so exactly one row inside the window is enough and no
    predecessor row is needed.

    read_scope is left NULL DELIBERATELY. An earlier version copied it from the
    cluster's newest real snapshot so the tool's schema-observation section would
    report the schema as confirmed. That was two mistakes. It read schema_snapshots
    directly, which the selection contract forbids for exactly the reason it exists
    (tests/unit/data_pipeline/test_schema_snapshot_parity.py caught it), and more
    importantly a read_scope is a claim about WHICH CATALOG A READ ACTUALLY REACHED.
    No read reached anything here. Copying the label onto a synthetic row fabricates
    provenance, and an unknown scope is precisely what this row has: NULL is the
    honest value and the observation section is right to say so.
    """
    ts = _iso(anchor - timedelta(minutes=6))
    tables = {
        "orders": ["id", "customer_id", "status", "created_at", "total_amount"],
        "order_items": ["id", "order_id", "sku", "qty"],
        "customers": ["id", "name", "email"],
    }
    diff = {
        "added": [],
        "dropped": [],
        # A dropped index is not visible in a column diff, so the realistic shape for
        # "a migration made the table slower" is a column set that changed.
        "modified": [{
            "table": "orders",
            "added_columns": ["total_amount"],
            "dropped_columns": ["order_total"],
        }],
        "rename_candidates": [],
    }
    query(
        "INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, tables_json, "
        " diff_from_previous_json, read_scope, last_seen_at) "
        "VALUES (:cid, :ts::timestamptz, :schema, :tables::jsonb, :diff::jsonb, :scope, "
        " :ts::timestamptz) "
        "ON CONFLICT (cluster_id, schema_name, snapshot_time) DO NOTHING",
        {"cid": cluster_id, "ts": ts, "schema": "public",
         "tables": json.dumps(tables), "diff": json.dumps(diff), "scope": None},
    )
    return [{"table": "schema_snapshots", "times": [ts], "schema_name": "public"}]


# ---------------------------------------------------------------------------
# Catalog. One entry per button.
# ---------------------------------------------------------------------------
# `category` is the diagnose_root_cause candidate category the scenario is designed to
# produce, and `weight` its BASE_WEIGHTS entry. They are shown in the UI so a viewer
# can see WHY one scenario's report ranks its cause differently from another's, rather
# than the ranking looking arbitrary. Kept as data here, not imported: api/ cannot
# import mcp_servers, and a wrong number here is a wrong label, not a wrong diagnosis.
SCENARIOS = [
    {
        "id": "schema_change",
        "title": "스키마 변경 후 성능 저하",
        "summary": "마이그레이션이 orders 테이블의 컬럼 구성을 바꾼 직후 지연이 증가한 상황",
        "category": "schema_change",
        "weight": 5.0,
        "engines": ("postgres",),
        "signals": ("schema_snapshots",),
    },
    {
        "id": "failover",
        "title": "라이터 페일오버",
        "summary": "라이터 인스턴스가 교체되며 연결이 끊기고 캐시가 비워진 상황",
        "category": "event",
        "weight": 4.0,
        "engines": (),
        "signals": ("event_log",),
    },
    {
        "id": "lock_contention",
        "title": "락 경합",
        "summary": "배치 트랜잭션이 orders 행 잠금을 길게 유지해 애플리케이션 쿼리가 대기하는 상황",
        "category": "blocking",
        "weight": 3.0,
        "engines": (),
        "signals": ("blocking_locks",),
    },
    {
        "id": "cpu_saturation",
        "title": "CPU 포화",
        "summary": "CPU 사용률이 베이스라인 대비 8배로 올라 20분간 유지된 상황",
        "category": "metric_spike",
        "weight": 2.0,
        "engines": (),
        "signals": ("metric_snapshots",),
    },
    {
        "id": "connection_exhaustion",
        "title": "커넥션 고갈",
        "summary": "커넥션 수가 max_connections 근처까지 치솟아 신규 연결이 거부되는 상황",
        "category": "metric_spike",
        "weight": 2.0,
        "engines": (),
        "signals": ("metric_snapshots",),
    },
    {
        "id": "slow_query_surge",
        "title": "슬로우 쿼리 급증",
        "summary": "인덱스를 타지 못하는 조인이 상위 쿼리를 점유하며 총 실행시간이 급증한 상황",
        "category": "slow_query",
        "weight": 2.0,
        "engines": (),
        "signals": ("query_stats",),
    },
]
_BY_ID = {s["id"]: s for s in SCENARIOS}


def _inject(scenario_id, query, cluster_id, anchor):
    """Write the scenario's signals. Returns the cleanup manifest.

    NO COMPANION EVENT ROW, except for the scenario that IS an event. Every one of
    these used to also write a matching event_log row (a CloudWatch alarm beside the
    CPU spike, a `ddl` notice beside the schema diff) on the theory that production
    produces both. MEASURED against the real ranker on a real PostgreSQL: the
    companion won every time and the actual cause was pushed down the list.
      cpu_saturation         event 5.58  >  metric_spike 2.60
      connection_exhaustion  event 5.58  >  metric_spike 2.60
      schema_change          event 5.44  >  schema_change 4.30
    `event` carries base weight 4.0 against metric_spike's 2.0 and slow_query's 2.0,
    and a `critical` severity multiplies it by another 1.5, so an alarm row outscores
    the measurement it is merely restating. A demo whose top-ranked root cause is
    "a CloudWatch alarm fired" has explained nothing: that is the symptom being
    announced, not the thing that caused it. The signal-table row IS the evidence, so
    it ships alone and the report names the measurement.

    (The ranker treating a `CPUUtilization >= 90%` alarm as a stronger cause than the
    CPU series it was derived from is a real weakness in ranking policy, not a demo
    artifact, and deliberately not papered over here. It is worth deciding on its own
    merits, because it affects genuine diagnoses: event_processor writes those rows.)
    """
    manifest = []
    if scenario_id == "cpu_saturation":
        manifest += _write_metric_series(query, cluster_id, anchor, "cpu", 11.0, 96.0)
    elif scenario_id == "connection_exhaustion":
        manifest += _write_metric_series(query, cluster_id, anchor, "db_connections", 42.0, 478.0)
    elif scenario_id == "lock_contention":
        manifest += _write_blocking(query, cluster_id, anchor)
    elif scenario_id == "slow_query_surge":
        manifest += _write_slow_queries(query, cluster_id, anchor)
    elif scenario_id == "schema_change":
        manifest += _write_schema_change(query, cluster_id, anchor)
    elif scenario_id == "failover":
        # The only scenario whose signal genuinely IS an engine event.
        manifest += _write_event(
            query, cluster_id, anchor, event_type="failover", source="rds.event",
            severity="critical", minutes_before=4,
            message="Completed failover to DB instance in a different availability zone",
        )
    else:
        raise ValueError(f"unknown scenario {scenario_id}")
    return manifest


def _manifest_of(run) -> list:
    """The run's cleanup manifest, as a list, however the driver handed it over.

    `injected_json` is a jsonb column and the two things that read it disagree about
    the Python type. The RDS Data API returns it as a STRING (its stringValue branch),
    so the obvious `json.loads` is right there and wrong elsewhere: a driver that
    deserialises jsonb hands back a list, `json.loads` raises TypeError, and an
    `except: manifest = []` then turns the purge into a silent no-op that reports
    success. Caught by the real-engine test, where row_to_json nests the value.

    An unreadable manifest is LOGGED rather than swallowed. The run is still marked
    resolved by the caller (there is nothing further this code can do about it), but
    the rows it named are now orphaned and only the log says why.
    """
    raw = run.get("injected_json")
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        print(f"[scenarios] run {run.get('id')} manifest unreadable "
              f"({type(e).__name__}); its injected rows will not be purged")
        return []
    return parsed if isinstance(parsed, list) else []


def _purge_old_runs(query, cluster_id):
    """Delete the injected rows of every previous run that no longer holds the lock.

    Self-cleaning on each run rather than on a schedule: a scenario runner that needs
    its own Lambda to tidy up has two things to deploy and one of them fails silently.
    Deletes are keyed on the manifest, so collected rows at neighbouring timestamps are
    untouched.

    THE CUTOFF IS THE LOCK, not an hour and a half. It used to be a separate
    90-minute constant, well past the 30-minute analysis window, on the reasoning that
    rows outside the window are harmless. They are not harmless while
    they are INSIDE it: a scenario run two minutes after another one analysed a window
    containing both, and the earlier scenario's signals outranked the later one's.
    MEASURED against the real ranker: running lock_contention and then
    slow_query_surge put `blocking 5.58` on top of the slow-query report, and the
    schema-change report ranked three of the lock rows above its own DDL diff. The
    presenter presses a button and the report explains the previous button.

    So a new run starts by clearing every unresolved predecessor whose lock has
    expired, which is exactly the set whose RCA has had LOCK_MINUTES to read its rows.
    A run still holding the lock is never touched, and _active_run refuses the new run
    outright in that case, so nothing is deleted from under an analysis in flight.
    """
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(minutes=LOCK_MINUTES))
    stale = query(
        "SELECT id, injected_json FROM scenario_runs "
        "WHERE cluster_id = :cid AND resolved_at IS NULL AND started_at < :cutoff::timestamptz "
        "ORDER BY started_at LIMIT 20",
        {"cid": cluster_id, "cutoff": cutoff},
    )
    purged = 0
    for run in stale:
        manifest = _manifest_of(run)
        for entry in manifest:
            table = entry.get("table")
            times = entry.get("times") or []
            ts_col = _PURGEABLE.get(table)
            if not ts_col or not times or not _IDENT.match(table):
                continue
            # ::timestamptz ON EVERY ELEMENT. The Data API binds these as typed TEXT
            # parameters, and PostgreSQL refuses `timestamp with time zone = text`
            # outright: "operator does not exist ... You might need to add explicit
            # type casts" (SQLState 42883), which made the purge, and therefore the
            # whole POST, a 500 against the live cache. The real-engine test did not
            # catch it because its harness inlines values as UNTYPED literals, which
            # PostgreSQL happily coerces; a typed text parameter is not coerced.
            # Every other statement in this module already casts.
            placeholders = ", ".join(f":t{i}::timestamptz" for i in range(len(times)))
            params = {"cid": cluster_id}
            params.update({f"t{i}": t for i, t in enumerate(times)})
            extra = ""
            if entry.get("metric_type"):
                extra = " AND metric_type = :mt"
                params["mt"] = entry["metric_type"]
            elif entry.get("schema_name"):
                extra = " AND schema_name = :sn"
                params["sn"] = entry["schema_name"]
            query(
                f"DELETE FROM {table} WHERE cluster_id = :cid "
                f"  AND {ts_col} IN ({placeholders}){extra}",
                params,
            )
        query(
            "UPDATE scenario_runs SET status = 'resolved', resolved_at = NOW() WHERE id = :id",
            {"id": int(run["id"])},
        )
        purged += 1
    return purged


def _active_run(query, cluster_id):
    """The run still holding the lock, or None.

    Two things this predicate must NOT do.

    It must not key on `status`. It used to require `status = 'running'`, which the
    writer flips to `injected` the moment the last INSERT lands, milliseconds later.
    So the lock was held for the duration of the injection and nothing else: a second
    scenario could start immediately, its window still full of the first one's
    signals, and the first one's RCA could still be queued. The guard read like a
    concurrency guard and blocked nothing.

    It must not key on status for a second reason either: a Lambda that times out
    mid-injection never gets to write a terminal status, so a status-only check would
    wedge the lock permanently. Both failures point the same way, which is why the
    only conditions here are "unresolved" and "younger than LOCK_MINUTES".

    LOCK_MINUTES therefore covers the whole life of a run, injection plus the RCA that
    reads it, and _purge_old_runs clears exactly the runs this predicate no longer
    protects.
    """
    rows = query(
        "SELECT id, scenario_id, status, started_at, task_id FROM scenario_runs "
        "WHERE cluster_id = :cid AND resolved_at IS NULL "
        "  AND started_at > NOW() - (:mins || ' minutes')::interval "
        "ORDER BY started_at DESC LIMIT 1",
        {"cid": cluster_id, "mins": str(LOCK_MINUTES)},
    )
    return rows[0] if rows else None


def _requester(event) -> str:
    claims = (
        (event.get("requestContext") or {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    )
    return str(claims.get("email") or claims.get("cognito:username") or "")


def _engine_of(query, cluster_id) -> str:
    rows = query(
        "SELECT engine FROM cluster_meta WHERE cluster_id = :cid LIMIT 1",
        {"cid": cluster_id},
    )
    return str((rows[0].get("engine") if rows else "") or "").lower()


def _run(query, event, scenario_id, cluster_id):
    scenario = _BY_ID.get(scenario_id)
    if not scenario:
        return _response(404, {"error": f"unknown scenario '{scenario_id}'"})

    # Engine gate, stated rather than discovered. The schema-change scenario relies on
    # schema_snapshots, which is PostgreSQL-only BY DECISION (MySQL's
    # information_schema is privilege-filtered, so a REVOKE is byte-identical to a DROP
    # in every diff bucket). On a MySQL cluster diagnose_root_cause returns
    # `unsupported_engine` for that source, so the button would inject rows and then
    # produce a report with no schema evidence in it and no stated reason.
    if scenario["engines"]:
        engine = _engine_of(query, cluster_id)
        if not any(e in engine for e in scenario["engines"]):
            return _response(409, {
                "error": f"'{scenario_id}' requires a PostgreSQL cluster; "
                         f"{cluster_id} reports engine '{engine or 'unknown'}'",
            })

    _purge_old_runs(query, cluster_id)

    active = _active_run(query, cluster_id)
    if active:
        return _response(409, {
            "error": "another scenario is still running on this cluster",
            "running": {
                "scenario_id": active.get("scenario_id"),
                "started_at": active.get("started_at"),
                "task_id": active.get("task_id"),
            },
            "retry_after_minutes": LOCK_MINUTES,
        })

    anchor = datetime.now(timezone.utc).replace(microsecond=0)
    anchor_iso = _iso(anchor)
    inserted = query(
        "INSERT INTO scenario_runs (scenario_id, cluster_id, anchor_at, status, requested_by) "
        "VALUES (:sid, :cid, :anchor::timestamptz, 'running', :who) RETURNING id",
        {"sid": scenario_id, "cid": cluster_id, "anchor": anchor_iso, "who": _requester(event)},
    )
    run_id = int(inserted[0]["id"])

    manifest = _inject(scenario_id, query, cluster_id, anchor)

    # Bookkeeping row, written LAST and under a source the RCA excludes. See the
    # module docstring: under any other source this becomes the top-ranked cause.
    query(
        "INSERT INTO event_log (cluster_id, event_time, event_type, source, message, severity) "
        "VALUES (:cid, :ts::timestamptz, 'scenario_started', 'dbops-scenario-runner', :msg, 'info')",
        {"cid": cluster_id, "ts": anchor_iso,
         "msg": f"데모 장애 시나리오 실행: {scenario['title']} (run {run_id})"},
    )
    manifest.append({"table": "event_log", "times": [anchor_iso]})

    # dedupe=False: the runner's own one-at-a-time lock is the guard here, and the
    # 15-minute window would silently swallow the second scenario of a demo.
    task_id = enqueue_auto_rca(
        cluster_id,
        f"scenario:{scenario_id}",
        title=f"시나리오 RCA: {scenario['title']}",
        trigger=f"scenario:{scenario_id}",
        observed_at=anchor_iso,
        dedupe=False,
    )

    query(
        "UPDATE scenario_runs SET status = 'injected', task_id = :tid, injected_json = :man::jsonb "
        "WHERE id = :id",
        {"id": run_id, "tid": task_id or "", "man": json.dumps(manifest)},
    )

    return _response(202, {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "cluster_id": cluster_id,
        "anchor_at": anchor_iso,
        "task_id": task_id,
        "signals_written": [
            {"table": e["table"], "rows": len(e.get("times") or [])} for e in manifest
        ],
        # Absent task_id means the enqueue did not happen (AGENT_TASKS_TABLE unset, or
        # DynamoDB refused). The signals ARE written either way, so say which half
        # succeeded instead of returning a 202 that implies a report is coming.
        "rca_enqueued": bool(task_id),
    })


def _history(query, cluster_id, limit=20):
    rows = query(
        "SELECT id, scenario_id, cluster_id, started_at, anchor_at, status, task_id, "
        "       requested_by, resolved_at "
        "FROM scenario_runs WHERE cluster_id = :cid "
        "ORDER BY started_at DESC LIMIT :lim",
        {"cid": cluster_id, "lim": int(limit)},
    )
    for r in rows:
        meta = _BY_ID.get(r.get("scenario_id")) or {}
        r["title"] = meta.get("title") or r.get("scenario_id")
    return rows


def lambda_handler(event, context):
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    raw_path = (
        (event.get("requestContext") or {}).get("http", {}).get("path")
        or event.get("rawPath")
        or ""
    )

    cluster_id = os.environ.get("SCENARIO_CLUSTER_ID", "").strip()
    cluster_arn = os.environ.get("CACHE_DB_CLUSTER_ARN", "")
    secret_arn = os.environ.get("CACHE_DB_SECRET_ARN", "")
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    if method == "GET" and raw_path.endswith("/scenarios"):
        # The catalog is static, so it answers even with no cluster configured. The
        # UI needs it to render the disabled state and say WHY the buttons are off.
        return _response(200, {
            "cluster_id": cluster_id,
            "enabled": bool(cluster_id and cluster_arn),
            "window_minutes": WINDOW_MINUTES,
            "lock_minutes": LOCK_MINUTES,
            "scenarios": [
                {k: v for k, v in s.items() if k != "engines"} for s in SCENARIOS
            ],
        })

    if not cluster_id:
        return _response(503, {
            "error": "scenario demos are not configured: set Settings.SCENARIO_CLUSTER_ID "
                     "to a registered cluster and redeploy the agent stack",
        })
    if not cluster_arn or not secret_arn:
        return _response(503, {"error": "cache database is not configured for this Lambda"})

    query = _make_query(boto3.client("rds-data"), cluster_arn, secret_arn, database)

    try:
        if method == "GET" and raw_path.endswith("/runs"):
            return _response(200, {"cluster_id": cluster_id, "runs": _history(query, cluster_id)})
        if method == "POST":
            scenario_id = (event.get("pathParameters") or {}).get("id") or ""
            if not scenario_id:
                return _response(400, {"error": "scenario id is required in the path"})
            return _run(query, event, scenario_id, cluster_id)
    except Exception as e:
        # No str(e): a Data API error quotes the failing SQL, which would put table and
        # column names into a response any authenticated user can read.
        print(f"[scenarios] {method} {raw_path} failed: {type(e).__name__}: {e}")
        return _response(500, {"error": "scenario request failed; see CloudWatch logs"})

    return _response(405, {"error": f"method {method} not allowed"})

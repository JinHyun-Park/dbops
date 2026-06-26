"""MySQL InnoDB engine status → metric_snapshots + cluster_health_findings.

Parses `SHOW ENGINE INNODB STATUS` (a single text blob) for the internal signals
CloudWatch does NOT expose:
  - History list length   — un-purged row versions; a sustained climb means the
    purge thread is falling behind a long-running transaction (→ undo bloat).
  - Buffer pool hit rate  — reads served from the buffer pool vs disk.
  - Checkpoint age (MB)   — distance between the log sequence number and the last
    checkpoint; redo-log pressure.
  - Pending I/O           — pending preads + pwrites (I/O backlog).

The blob's exact wording varies across MySQL/Aurora versions, so each field is
parsed independently and simply skipped when its pattern is absent — nothing
raises. Findings are threshold checks on the parsed values.
"""

import re

INSERT_METRIC = (
    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
    "VALUES (:cluster_id, NOW(), :metric_type, :value, '{}'::jsonb) "
    "ON CONFLICT DO NOTHING"
)

INSERT_FINDING = (
    "INSERT INTO cluster_health_findings "
    "(cluster_id, snapshot_time, check_type, severity, subject, value_str, "
    " threshold_str, recommendation, details) "
    "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
    " :value_str, :threshold_str, :recommendation, :details::jsonb)"
)

_RE_HLL = re.compile(r"History list length\s+(\d+)")
_RE_HIT = re.compile(r"Buffer pool hit rate\s+(\d+)\s*/\s*1000")
# Log sequence / checkpoint exist on RDS MySQL but NOT on Aurora MySQL (its log
# is the distributed storage volume, no local redo) — parsed when present,
# silently absent on Aurora.
_RE_LSN = re.compile(r"Log sequence number\s+(\d+)")
_RE_CKP = re.compile(r"Last checkpoint at\s+(\d+)")
# Pending I/O — Aurora MySQL format: "Pending normal aio reads: [..] , aio
# writes: [..] ," plus "Pending flushes (fsync) log: N; buffer pool: M".
_RE_AIO = re.compile(r"Pending normal aio reads:\s*\[([\d,\s]*)\]\s*,\s*aio writes:\s*\[([\d,\s]*)\]")
_RE_FSYNC = re.compile(r"Pending flushes \(fsync\)\s*log:\s*(\d+);\s*buffer pool:\s*(\d+)")
# Row throughput — first "X inserts/s, Y updates/s, Z deletes/s, W reads/s" line
# (user rows; the system-rows line comes after).
_RE_ROWOPS = re.compile(
    r"([\d.]+) inserts/s,\s*([\d.]+) updates/s,\s*([\d.]+) deletes/s,\s*([\d.]+) reads/s")

_HLL_WARN = 1_000_000
_HLL_CRIT = 10_000_000


def parse_innodb_status(blob):
    """Extract the tracked gauges from a SHOW ENGINE INNODB STATUS blob.

    Returns a dict of metric_type -> value for whatever was parseable (a field
    whose pattern is absent is simply omitted). Pure + side-effect-free so it is
    unit-testable against canned blobs."""
    out = {}
    if not blob:
        return out
    m = _RE_HLL.search(blob)
    if m:
        out["innodb_history_list_length"] = float(int(m.group(1)))
    m = _RE_HIT.search(blob)
    if m:
        out["innodb_buffer_pool_hit_rate"] = int(m.group(1)) / 10.0  # /1000 → %
    lsn = _RE_LSN.search(blob)
    ckp = _RE_CKP.search(blob)
    if lsn and ckp:
        age = int(lsn.group(1)) - int(ckp.group(1))
        if age >= 0:
            out["innodb_checkpoint_age_mb"] = age / 1048576.0
    # Pending I/O: aio read + write queues + the fsync backlog (log + buffer pool).
    pending = 0
    found_pending = False
    aio = _RE_AIO.search(blob)
    if aio:
        found_pending = True
        for grp in (aio.group(1), aio.group(2)):
            pending += sum(int(n) for n in re.findall(r"\d+", grp))
    fsync = _RE_FSYNC.search(blob)
    if fsync:
        found_pending = True
        pending += int(fsync.group(1)) + int(fsync.group(2))
    if found_pending:
        out["innodb_pending_io"] = float(pending)
    rows = _RE_ROWOPS.search(blob)
    if rows:
        out["innodb_row_ops_per_sec"] = sum(float(rows.group(i)) for i in range(1, 5))
    return out


def collect_mysql_innodb_status(
    rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
    cluster_id, database, snapshot_ts,
):
    errors = []
    try:
        resp = rds_data_client.execute_statement(
            resourceArn=target_cluster_arn, secretArn=target_secret_arn, database=database,
            sql="/* source=dbops-etl */ SHOW ENGINE INNODB STATUS",
            includeResultMetadata=True,
        )
    except Exception as e:
        return {"cluster_id": cluster_id, "metrics_inserted": 0, "findings": 0,
                "errors": [f"show innodb status: {e}"]}

    rows = resp.get("records", [])
    # SHOW ENGINE INNODB STATUS → one row: [Type, Name, Status]; Status is the blob.
    blob = ""
    if rows and len(rows[0]) >= 3:
        blob = rows[0][2].get("stringValue", "") if not rows[0][2].get("isNull") else ""

    metrics = parse_innodb_status(blob)
    for mtype, value in metrics.items():
        cache_execute(INSERT_METRIC, {
            "cluster_id": cluster_id, "metric_type": mtype, "value": float(value),
        })

    findings = 0
    hll = metrics.get("innodb_history_list_length")
    if hll is not None and hll > _HLL_WARN:
        sev = "critical" if hll > _HLL_CRIT else "warning"
        cache_execute(INSERT_FINDING, {
            "cluster_id": cluster_id, "ts": snapshot_ts,
            "check_type": "innodb_history_list_high", "severity": sev,
            "subject": "InnoDB History List Length", "value_str": f"{int(hll):,}",
            "threshold_str": f"≤ {_HLL_WARN:,}",
            "recommendation": ("un-purge된 행 버전이 많습니다 — 장기 트랜잭션이 purge를 막고 있을 수 "
                               "있습니다. 오래된 트랜잭션(information_schema.innodb_trx)을 확인/종료하세요."),
            "details": "{}",
        })
        findings += 1

    return {"cluster_id": cluster_id, "metrics_inserted": len(metrics),
            "findings": findings, "errors": errors}

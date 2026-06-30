"""Pull-based case opener. Scans the two tables emitters already write
(cluster_health_findings, event_log anomalies) and opens one remediation_cases
row per live symptom. Idempotent via the partial unique index — re-emission while
a case is open only bumps last_seen_at.
"""
import time

from boto3.dynamodb.conditions import Attr
from remediation_classify import classify_action

# How far back to scan each run. A little wider than the evaluator cadence so
# nothing slips between runs; ON CONFLICT makes overlap harmless.
SCAN_WINDOW = "INTERVAL '1 hour'"
WIN_METRIC_MIN = 360    # 6h  — metric-symptom cases
WIN_FINDING_MIN = 1440  # 24h — recurring-finding cases

# DynamoDB scan window for completed RCA tasks.
# Invariant: cadence (20 min) < RCA_SCAN_WINDOW < WIN_METRIC_MIN (6h = 360 min).
# WHY the upper bound: if the window >= WIN_METRIC_MIN a task stays visible AFTER its
# case has been evaluated and closed (status moves out of 'open'), freeing the partial
# unique index to re-insert → phantom re-open. Staying strictly below 6h makes that
# structurally impossible.
# WHY 4h (not 1h): tolerates multi-hour evaluator outages; tasks remain scannable
# even if the Lambda is down for up to ~4h before recovery.
# agent-tasks rows store completed_at as epoch millis (13-digit string); lexicographic
# >= on equal-width strings equals numeric compare for same-era values.
RCA_SCAN_WINDOW_MS = 4 * 60 * 60 * 1000  # 4 hours in millis — tolerates outages, < 6h minimum

_INSERT = (
    "INSERT INTO remediation_cases "
    "(cluster_id, symptom_class, symptom_subject, watch_metric, severity_at_open, "
    " recommendation_text, action_class, source, evaluate_after) "
    "VALUES (:cluster_id, :symptom_class, :symptom_subject, :watch_metric, :severity_at_open, "
    " :recommendation_text, :action_class, :source, "
    " NOW() + (:win_min || ' minutes')::interval) "
    "ON CONFLICT (cluster_id, symptom_class, symptom_subject) WHERE status = 'open' "
    "DO UPDATE SET last_seen_at = NOW()"
)


def open_cases(query) -> int:
    opened = 0

    findings = query(
        "SELECT cluster_id, check_type, subject, severity, recommendation "
        "FROM cluster_health_findings "
        f"WHERE snapshot_time > NOW() - {SCAN_WINDOW}"
    )
    for f in findings or []:
        query(_INSERT, {
            "cluster_id": f["cluster_id"],
            "symptom_class": f"finding:{f['check_type']}",
            "symptom_subject": (f.get("subject") or "")[:255],
            "watch_metric": None,  # findings are judged by recurrence, not a metric
            "severity_at_open": f.get("severity"),
            "recommendation_text": f.get("recommendation"),
            "action_class": classify_action(f.get("recommendation") or ""),
            "source": "finding_collector",
            "win_min": WIN_FINDING_MIN,
        })
        opened += 1

    anomalies = query(
        "SELECT cluster_id, event_type, message "
        "FROM event_log "
        f"WHERE event_type LIKE 'anomaly_%' AND event_time > NOW() - {SCAN_WINDOW}"
    )
    for a in anomalies or []:
        metric = (a["event_type"] or "")[len("anomaly_"):]  # 'anomaly_cpu' -> 'cpu'
        if not metric:  # malformed 'anomaly_' with no suffix — skip, nothing to watch
            continue
        query(_INSERT, {
            "cluster_id": a["cluster_id"],
            "symptom_class": f"anomaly:{metric}",
            "symptom_subject": metric,
            "watch_metric": metric,  # judged by baseline recovery
            "severity_at_open": None,
            "recommendation_text": a.get("message"),
            # anomaly alerts carry no prescribed action; 'manual' = "resolved on its own / unspecified"
            "action_class": "manual",
            "source": "proactive_monitor",
            "win_min": WIN_METRIC_MIN,
        })
        opened += 1

    return opened


def open_rca_cases(query, ddb_table) -> int:
    """Open rca:<category> cases from recently-completed RCA tasks. Best-effort;
    a missing/empty result just yields no case."""
    if ddb_table is None:
        return 0
    cutoff = str(int(time.time() * 1000) - RCA_SCAN_WINDOW_MS)
    items, scan_kwargs = [], {
        "FilterExpression": Attr("completed_at").gte(cutoff),
    }
    while True:  # paginate — never trust a single scan page; no Limit+FilterExpression
        resp = ddb_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    opened = 0
    for it in items:
        if it.get("kind") not in ("auto_rca", "manual_rca") or it.get("status") != "done":
            continue
        res = it.get("result") or {}
        cands = res.get("candidates") or []
        recs = res.get("recommendations") or []
        if not cands:
            continue
        category = cands[0].get("category") or "unknown"
        metric = cands[0].get("metric")
        query(_INSERT, {
            "cluster_id": it.get("cluster_id"),
            "symptom_class": f"rca:{category}",
            "symptom_subject": (category or "")[:255],
            "watch_metric": metric,
            "severity_at_open": None,
            "recommendation_text": recs[0] if recs else None,
            "action_class": classify_action(recs[0] if recs else "", category),
            "source": "rca_worker",
            "win_min": WIN_METRIC_MIN if metric else WIN_FINDING_MIN,
        })
        opened += 1
    return opened

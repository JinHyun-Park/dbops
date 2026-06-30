"""Pull-based case opener. Scans the two tables emitters already write
(cluster_health_findings, event_log anomalies) and opens one remediation_cases
row per live symptom. Idempotent via the partial unique index — re-emission while
a case is open only bumps last_seen_at.
"""
from outcome_evaluator.remediation_classify import classify_action

# How far back to scan each run. A little wider than the evaluator cadence so
# nothing slips between runs; ON CONFLICT makes overlap harmless.
SCAN_WINDOW = "INTERVAL '1 hour'"
WIN_METRIC_MIN = 360    # 6h  — metric-symptom cases
WIN_FINDING_MIN = 1440  # 24h — recurring-finding cases

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

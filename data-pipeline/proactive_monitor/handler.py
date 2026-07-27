import json
import os

import boto3


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    sns_topic = os.environ.get("ALERT_TOPIC_ARN", "")

    def cache_query(sql, params=None):
        sql_params = []
        if params:
            for k, v in params.items():
                if isinstance(v, (int, float)):
                    sql_params.append({"name": k, "value": {"doubleValue": float(v)}})
                else:
                    sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=database,
            sql=f"/* source=dbops-monitor */ {sql}", parameters=sql_params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        rows = []
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows

    anomaly_sql = """
        WITH recent AS (
            SELECT cluster_id, metric_type, AVG(value) as current_avg
            FROM metric_snapshots
            WHERE ts > NOW() - INTERVAL '15 minutes'
              AND (dimensions IS NULL OR dimensions::text = '{}')
            GROUP BY cluster_id, metric_type
        ),
        baseline AS (
            SELECT cluster_id, metric_type, AVG(value) as baseline_avg, STDDEV(value) as baseline_std
            FROM metric_snapshots
            WHERE ts > NOW() - INTERVAL '7 days' AND ts <= NOW() - INTERVAL '15 minutes'
              AND (dimensions IS NULL OR dimensions::text = '{}')
            GROUP BY cluster_id, metric_type
        )
        SELECT r.cluster_id, r.metric_type, r.current_avg, b.baseline_avg, b.baseline_std,
               CASE WHEN b.baseline_std > 0 THEN (r.current_avg - b.baseline_avg) / b.baseline_std ELSE 0 END as z_score
        FROM recent r JOIN baseline b ON r.cluster_id = b.cluster_id AND r.metric_type = b.metric_type
        WHERE ABS(CASE WHEN b.baseline_std > 0 THEN (r.current_avg - b.baseline_avg) / b.baseline_std ELSE 0 END) > 3
          -- ponytail: dedup via event_log cooldown — one alert per (cluster,metric)
          -- per 60min even if the anomaly persists, so a sustained deviation
          -- doesn't spam SNS every run. Reuses the anomaly_<metric> event_type
          -- written below; no new table. Widen the interval if still too chatty.
          AND NOT EXISTS (
            SELECT 1 FROM event_log e
            WHERE e.cluster_id = r.cluster_id
              AND e.event_type = 'anomaly_' || r.metric_type
              AND e.event_time > NOW() - INTERVAL '60 minutes'
          )
    """

    anomalies = cache_query(anomaly_sql)
    alerts_sent = 0

    if anomalies and sns_topic:
        sns = boto3.client("sns")
        for anomaly in anomalies:
            cluster_id = anomaly.get("cluster_id", "unknown")
            metric = anomaly.get("metric_type", "unknown")
            z_score = anomaly.get("z_score", 0)
            current = anomaly.get("current_avg", 0)
            baseline = anomaly.get("baseline_avg", 0)
            sev = "critical" if abs(z_score) >= 5 else "warning"

            message = (
                f"[DBOps Anomaly Detected]\n"
                f"Cluster: {cluster_id}\n"
                f"Metric: {metric}\n"
                f"Current: {current:.2f} (baseline: {baseline:.2f}, z-score: {z_score:.1f})\n"
            )

            # Record the cooldown row FIRST so a publish failure or a Lambda retry
            # can't re-alert the same anomaly — the next run's dedup query keys off
            # this event_log row. Worst case is one missed alert (publish threw
            # after the row was written) — the safe direction for an alert path.
            cache_query(
                "INSERT INTO event_log (cluster_id, event_time, event_type, source, message, severity) "
                "VALUES (:cid, NOW(), :etype, 'dbops-monitor', :msg, :sev)",
                {"cid": cluster_id, "etype": f"anomaly_{metric}", "msg": message, "sev": sev},
            )
            try:
                sns.publish(
                    TopicArn=sns_topic,
                    Subject=f"[DBOps {sev.upper()}] {cluster_id}: {metric} z={z_score:.1f}",
                    Message=message,
                )
                alerts_sent += 1
            except Exception as e:
                print(f"[proactive-monitor] SNS publish failed for {cluster_id}/{metric}: {type(e).__name__}: {e}")

    return {"statusCode": 200, "body": json.dumps({"anomalies_found": len(anomalies), "alerts_sent": alerts_sent})}

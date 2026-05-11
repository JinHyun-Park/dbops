import json
import os
import boto3


COMP_FN = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _query(rds_data, cluster_arn, secret_arn, database, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=f"/* source=dbops-alert-eval */ {sql}",
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


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    def q(sql, params=None):
        return _query(rds_data, cluster_arn, secret_arn, database, sql, params)

    rules = q(
        "SELECT id, cluster_id, name, metric_type, comparison, threshold "
        "FROM alert_rules WHERE enabled = true"
    )

    sns_topic = os.environ.get("ALERT_SNS_TOPIC_ARN", "")
    sns_client = boto3.client("sns") if sns_topic else None

    triggered = 0
    skipped = 0

    for rule in rules:
        metric_rows = q(
            "SELECT MAX(value) AS latest_value "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "AND metric_type = :mt "
            "AND ts > NOW() - INTERVAL '10 minutes'",
            {"cid": rule["cluster_id"], "mt": rule["metric_type"]},
        )
        if not metric_rows or metric_rows[0].get("latest_value") is None:
            skipped += 1
            continue

        latest = float(metric_rows[0]["latest_value"])
        threshold = float(rule["threshold"])
        comp_fn = COMP_FN.get(rule["comparison"])
        if not comp_fn or not comp_fn(latest, threshold):
            continue

        rule_id = int(rule["id"])
        message = (
            f"{rule['name']}: {rule['metric_type']} = {latest:.2f} "
            f"{rule['comparison']} {threshold}"
        )

        q(
            "INSERT INTO event_log (cluster_id, event_time, event_type, source, severity, message, raw_event) "
            "VALUES (:cid, NOW(), 'alert', 'dbops-alert-evaluator', 'warning', :msg, :raw::jsonb)",
            {
                "cid": rule["cluster_id"],
                "msg": message,
                "raw": json.dumps({
                    "rule_id": rule_id,
                    "metric_type": rule["metric_type"],
                    "value": latest,
                    "threshold": threshold,
                    "comparison": rule["comparison"],
                }),
            },
        )

        q(
            "UPDATE alert_rules SET last_triggered_at = NOW() WHERE id = :id",
            {"id": rule_id},
        )

        if sns_client:
            try:
                sns_client.publish(
                    TopicArn=sns_topic,
                    Subject=f"DBOps Alert: {rule['cluster_id']}",
                    Message=message,
                )
            except Exception as e:
                print(f"SNS publish failed for rule {rule_id}: {e}")

        triggered += 1

    return {
        "statusCode": 200,
        "body": json.dumps({
            "rules_evaluated": len(rules),
            "triggered": triggered,
            "skipped": skipped,
        }),
    }

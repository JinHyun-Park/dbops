import json
import os
import boto3


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    source = event.get("source", "unknown")
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    if source == "aws.rds":
        cluster_id = detail.get("SourceIdentifier", "")
        event_type = detail.get("EventCategories", ["unknown"])[0] if detail.get("EventCategories") else "unknown"
        message = detail.get("Message", "")
        severity = "warning" if event_type in ("failover", "failure") else "info"
    elif source == "aws.cloudwatch":
        cluster_id = _extract_cluster_from_alarm(detail)
        event_type = "alarm_" + detail.get("state", {}).get("value", "unknown").lower()
        message = detail.get("alarmName", "") + ": " + detail.get("state", {}).get("reason", "")
        severity = "critical" if detail.get("state", {}).get("value") == "ALARM" else "info"
    else:
        cluster_id = "unknown"
        event_type = detail_type
        message = json.dumps(detail)[:500]
        severity = "info"

    sql = """
        INSERT INTO event_log (cluster_id, event_time, event_type, source, message, severity, raw_event)
        VALUES (:cluster_id, NOW(), :event_type, :source, :message, :severity, :raw_event::jsonb)
    """
    params = {
        "cluster_id": cluster_id, "event_type": event_type,
        "source": source, "message": message,
        "severity": severity, "raw_event": json.dumps(event),
    }
    sql_params = [{"name": k, "value": {"stringValue": str(v)}} for k, v in params.items()]
    rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=database,
        sql=f"/* source=dbops-event-processor */ {sql}", parameters=sql_params,
    )

    if severity in ("warning", "critical"):
        sns_topic = os.environ.get("ALERT_TOPIC_ARN")
        if sns_topic:
            boto3.client("sns").publish(
                TopicArn=sns_topic,
                Subject=f"[DBOps {severity.upper()}] {cluster_id}: {event_type}",
                Message=message,
            )

    return {"statusCode": 200, "body": json.dumps({"processed": True, "cluster_id": cluster_id})}


def _extract_cluster_from_alarm(detail):
    dims = detail.get("configuration", {}).get("metrics", [{}])[0].get("metricStat", {}).get("metric", {}).get("dimensions", {})
    return dims.get("DBClusterIdentifier", dims.get("DBInstanceIdentifier", "unknown"))

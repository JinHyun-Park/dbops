"""EventBridge → cache DB event_log writer.

We get three flavors of events:
  - aws.rds native events ("backup", "failover", "failure", "maintenance"…)
    The category lives in detail.EventCategories[0].
  - aws.rds CloudTrail wrappers ("AWS API Call via CloudTrail").
    The action is in detail.eventName (CreateDBCluster, RebootDBCluster, …).
  - aws.cloudwatch alarm state changes.

Older versions of this handler blindly read EventCategories[0] for everything
under aws.rds, which produced a flood of "unknown" / "" event_types for
CloudTrail wrappers. The classifier below covers both shapes and falls back
to detail-type as a last resort.
"""

import json
import os
import boto3


# CloudTrail API names that change cluster state — we surface these as warnings
# so an operator sees the destructive ones at a glance.
_WRITE_API_PATTERNS = (
    "create", "delete", "modify", "reboot", "failover", "restore", "stop", "start",
    "promote", "remove", "add", "apply",
)


def _classify_rds(detail: dict, detail_type: str) -> tuple[str, str, str]:
    """Return (event_type, message, severity) for an aws.rds event."""
    # CloudTrail wrapper: detail-type starts with "AWS API Call".
    if detail.get("eventSource") == "rds.amazonaws.com" or "AWS API Call" in detail_type:
        action = detail.get("eventName") or "RdsApiCall"
        # Build a short message: action + identifier from requestParameters.
        req = detail.get("requestParameters") or {}
        identifier = (
            req.get("dBClusterIdentifier")
            or req.get("dBInstanceIdentifier")
            or req.get("dBSnapshotIdentifier")
            or req.get("dBClusterSnapshotIdentifier")
            or req.get("dBParameterGroupName")
            or req.get("dBSubnetGroupName")
            or ""
        )
        actor = (detail.get("userIdentity") or {}).get("invokedBy") or (
            (detail.get("userIdentity") or {}).get("userName") or "unknown actor"
        )
        msg_parts = [action]
        if identifier:
            msg_parts.append(f"on {identifier}")
        msg_parts.append(f"by {actor}")
        message = " ".join(msg_parts)
        # If the API call failed, AWS sets errorCode/errorMessage — bump severity.
        if detail.get("errorCode"):
            return action, f"{message} — error: {detail['errorCode']}", "warning"
        # Write actions get warning, read actions stay info.
        if any(p in action.lower() for p in _WRITE_API_PATTERNS):
            return action, message, "warning"
        return action, message, "info"

    # Native RDS event with EventCategories.
    categories = detail.get("EventCategories") or []
    category = categories[0] if categories else (detail_type or "rds-event")
    message = detail.get("Message") or detail.get("message") or ""
    severity = "warning" if category.lower() in ("failover", "failure", "low storage") else "info"
    return category, message, severity


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    source = event.get("source", "unknown")
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    if source == "aws.rds":
        cluster_id = (
            detail.get("SourceIdentifier")
            or (detail.get("requestParameters") or {}).get("dBClusterIdentifier")
            or (detail.get("requestParameters") or {}).get("dBInstanceIdentifier")
            or ""
        )
        event_type, message, severity = _classify_rds(detail, detail_type)
    elif source == "aws.cloudwatch":
        cluster_id = _extract_cluster_from_alarm(detail)
        event_type = "alarm_" + detail.get("state", {}).get("value", "unknown").lower()
        alarm_name = detail.get("alarmName", "")
        reason = detail.get("state", {}).get("reason", "")
        message = f"{alarm_name}: {reason}".strip(": ")
        severity = "critical" if detail.get("state", {}).get("value") == "ALARM" else "info"
    else:
        cluster_id = "unknown"
        event_type = detail_type or "event"
        message = json.dumps(detail)[:500]
        severity = "info"

    sql = """
        INSERT INTO event_log (cluster_id, event_time, event_type, source, message, severity, raw_event)
        VALUES (:cluster_id, NOW(), :event_type, :source, :message, :severity, :raw_event::jsonb)
    """
    params = {
        "cluster_id": cluster_id or "unknown",
        "event_type": event_type or "event",
        "source": source,
        "message": message[:1000],
        "severity": severity,
        "raw_event": json.dumps(event),
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

    return {"statusCode": 200, "body": json.dumps({"processed": True, "cluster_id": cluster_id, "event_type": event_type})}


def _extract_cluster_from_alarm(detail):
    dims = detail.get("configuration", {}).get("metrics", [{}])[0].get("metricStat", {}).get("metric", {}).get("dimensions", {})
    return dims.get("DBClusterIdentifier", dims.get("DBInstanceIdentifier", "unknown"))

import json
import os
from datetime import datetime

import boto3


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    s3_bucket = os.environ.get("ARCHIVE_BUCKET", "")

    def cache_query(sql, params=None):
        sql_params = []
        if params:
            for k, v in params.items():
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=database,
            sql=f"/* source=dbops-report */ {sql}", parameters=sql_params,
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

    clusters = cache_query("SELECT cluster_id FROM cluster_meta")
    report_date = datetime.utcnow().strftime("%Y-%m-%d")
    report_type = event.get("report_type", "daily")
    reports_generated = []

    for cluster in clusters:
        cid = cluster["cluster_id"]

        summary = cache_query(
            "SELECT AVG(value) as avg_aas, MAX(value) as max_aas FROM metric_snapshots "
            "WHERE cluster_id = :cid AND metric_type = 'aas' AND ts > NOW() - INTERVAL '24 hours'",
            {"cid": cid},
        )
        slow_count = cache_query(
            "SELECT COUNT(*) as cnt FROM slow_queries WHERE cluster_id = :cid AND ts > NOW() - INTERVAL '24 hours'",
            {"cid": cid},
        )
        events_count = cache_query(
            "SELECT COUNT(*) as cnt FROM event_log WHERE cluster_id = :cid AND event_time > NOW() - INTERVAL '24 hours'",
            {"cid": cid},
        )

        report_data = {
            "cluster_id": cid,
            "date": report_date,
            "type": report_type,
            "aas": summary[0] if summary else {},
            "slow_query_count": slow_count[0].get("cnt", 0) if slow_count else 0,
            "event_count": events_count[0].get("cnt", 0) if events_count else 0,
        }

        s3_key = f"reports/{cid}/{report_date}-{report_type}.json"
        if s3_bucket:
            boto3.client("s3").put_object(
                Bucket=s3_bucket, Key=s3_key,
                Body=json.dumps(report_data, default=str),
                ContentType="application/json",
            )

        cache_query(
            "INSERT INTO reports (cluster_id, report_type, report_date, summary, data, s3_key) "
            "VALUES (:cid, :report_type, :report_date, :summary, :data::jsonb, :s3_key)",
            {
                "cid": cid, "report_type": report_type, "report_date": report_date,
                "summary": f"{report_type} report for {cid} on {report_date}",
                "data": json.dumps(report_data, default=str), "s3_key": s3_key,
            },
        )
        reports_generated.append(cid)

    return {"statusCode": 200, "body": json.dumps({"reports": reports_generated, "date": report_date})}

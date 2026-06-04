import json
import os

import boto3


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    def query(sql, params=None):
        sql_params = []
        if params:
            for k, v in params.items():
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=database,
            sql=f"/* source=dbops-reports-api */ {sql}", parameters=sql_params,
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

    path = event.get("pathParameters", {})
    report_id = path.get("id") if path else None
    qsp = event.get("queryStringParameters") or {}
    cluster_id = qsp.get("cluster_id")

    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    try:
        if report_id:
            # RDS Data API binds named params as text, and reports.id is
            # BIGSERIAL — `bigint = text` has no operator in PostgreSQL and
            # 500s. Validate numeric, then cast the param to bigint.
            if not str(report_id).isdigit():
                return {"statusCode": 400, "headers": headers,
                        "body": json.dumps({"error": "invalid report id"})}
            rows = query("SELECT * FROM reports WHERE id = :id::bigint", {"id": report_id})
            body = rows[0] if rows else None
            status = 200 if body else 404
        else:
            sql = "SELECT id, cluster_id, report_type, report_date, summary, created_at FROM reports"
            params = {}
            if cluster_id:
                sql += " WHERE cluster_id = :cluster_id"
                params["cluster_id"] = cluster_id
            sql += " ORDER BY report_date DESC LIMIT 50"
            body = query(sql, params)
            status = 200
    except Exception as e:
        # Never surface a raw boto3/PG fault as a generic 500 page.
        print(f"[reports] query failed (report_id={report_id}): {e}")
        return {"statusCode": 500, "headers": headers,
                "body": json.dumps({"error": "리포트를 불러오지 못했습니다."})}

    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, default=str),
    }

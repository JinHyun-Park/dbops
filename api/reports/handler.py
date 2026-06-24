import json
import os
import re

import boto3
import tenancy
from botocore.exceptions import ClientError

_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


def _cluster_item(cluster_id: str) -> dict:
    """Fetch {cluster_id, team_id} from the clusters registry for a single
    cluster. Returns {} on miss or infra error (caller's cluster_visible treats
    missing team_id as default-open)."""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not cluster_id or not table_name:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[reports] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _norm_ts(s):
    """Normalize an RDS Data API timestamp string to unambiguous ISO 8601 UTC.

    The Data API returns TIMESTAMP / TIMESTAMPTZ as a space-separated, tz-less
    string in UTC (e.g. "2026-06-09 10:24:28.123"). The browser's `new Date()`
    parses that space form as LOCAL time, so every rendered timestamp came out
    shifted by the viewer's UTC offset (~9h in KST). Emit "...T...Z" so the
    client parses it as UTC and renders it in local time correctly. Strings
    that already carry a zone/offset are left untouched.
    """
    if not s or not isinstance(s, str):
        return s
    iso = s.replace(" ", "T", 1)
    if _TZ_SUFFIX_RE.search(iso):
        return iso
    return iso + "Z"


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
        meta = resp.get("columnMetadata", [])
        cols = [c["name"] for c in meta]
        # typeName per column, so we normalize ONLY timestamp columns (leaving
        # text that happens to look date-ish untouched).
        col_is_ts = ["timestamp" in (c.get("typeName") or "").lower() for c in meta]
        rows = []
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        val = f[typ]
                        if typ == "stringValue" and i < len(col_is_ts) and col_is_ts[i]:
                            val = _norm_ts(val)
                        row[col] = val
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows

    path = event.get("pathParameters", {})
    report_id = path.get("id") if path else None
    raw_path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path", "")
    qsp = event.get("queryStringParameters") or {}
    cluster_id = qsp.get("cluster_id")

    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    # GET /api/reports/{id}/html — presigned URL for the HTML twin
    if report_id and raw_path.endswith("/html"):
        if not str(report_id).isdigit():
            return {"statusCode": 400, "headers": headers,
                    "body": json.dumps({"error": "invalid report id"})}
        try:
            rows = query("SELECT cluster_id, s3_key FROM reports WHERE id = :id::bigint", {"id": report_id})
            if not rows:
                return {"statusCode": 404, "headers": headers,
                        "body": json.dumps({"error": "리포트를 찾을 수 없습니다."})}
            row_cluster_id = rows[0].get("cluster_id")
            if not tenancy.cluster_visible(event, _cluster_item(row_cluster_id)):
                return {"statusCode": 403, "headers": headers,
                        "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."})}
            s3_key = rows[0].get("s3_key")
            if not s3_key or not str(s3_key).endswith(".json"):
                return {
                    "statusCode": 404,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "이 리포트는 HTML 생성 이전에 만들어졌습니다.",
                    }),
                }
            html_key = s3_key[:-5] + ".html"
            bucket = os.environ["ARCHIVE_BUCKET"]
            s3 = boto3.client("s3")
            try:
                s3.head_object(Bucket=bucket, Key=html_key)
            except ClientError as ce:
                code = ce.response["Error"]["Code"]
                if code in ("404", "NoSuchKey"):
                    return {
                        "statusCode": 404,
                        "headers": headers,
                        "body": json.dumps({
                            "error": "HTML 리포트 파일이 아직 생성되지 않았습니다.",
                        }),
                    }
                raise
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": html_key},
                ExpiresIn=300,
            )
            return {"statusCode": 200, "headers": headers,
                    "body": json.dumps({"url": url})}
        except Exception as e:
            print(f"[reports] html presign failed (report_id={report_id}): {e}")
            return {"statusCode": 500, "headers": headers,
                    "body": json.dumps({"error": "HTML 리포트를 불러오지 못했습니다."})}

    try:
        if report_id:
            # RDS Data API binds named params as text, and reports.id is
            # BIGSERIAL — `bigint = text` has no operator in PostgreSQL and
            # 500s. Validate numeric, then cast the param to bigint.
            if not str(report_id).isdigit():
                return {"statusCode": 400, "headers": headers,
                        "body": json.dumps({"error": "invalid report id"})}
            rows = query("SELECT * FROM reports WHERE id = :id::bigint", {"id": report_id})
            if not rows:
                body = None
                status = 404
            else:
                row = rows[0]
                if not tenancy.cluster_visible(event, _cluster_item(row.get("cluster_id"))):
                    return {"statusCode": 403, "headers": headers,
                            "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."})}
                body = row
                status = 200
        else:
            sql = "SELECT id, cluster_id, report_type, report_date, summary, created_at FROM reports"
            params = {}
            if cluster_id:
                sql += " WHERE cluster_id = :cluster_id"
                params["cluster_id"] = cluster_id
            sql += " ORDER BY report_date DESC LIMIT 50"
            rows = query(sql, params)
            visible = tenancy.visible_set_from_registry(event)
            if visible is not None:
                rows = [r for r in rows if r.get("cluster_id") in visible]
            body = rows
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

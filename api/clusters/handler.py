"""Clusters API.

Routes:
  GET  /api/clusters              — list registered clusters (existing)
  POST /api/clusters              — register one cluster (existing)
  POST /api/clusters/discover     — list candidate clusters in an account+region
  POST /api/clusters/bulk-register — register multiple discovered clusters
"""

import base64
import json
import os
import re
from datetime import datetime

import boto3
import seeder
from botocore.exceptions import ClientError

_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


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


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload (base64) — no signature verification.
    Cognito-issued tokens originate from a trusted client we control, and a
    follow-up task wires API Gateway JWT authorizer for proper verification.
    Here we just want the `cognito:groups` claim for RBAC."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    """Return True if the caller's token does not place them in dbops-viewer.
    Tokens without any group claim default to admin (one-admin deploys), matching
    the frontend isAdmin() semantics. Anonymous (no token) requests are NOT
    considered admin — they fall through to 403 in callers that gate writes."""
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    # Token present + not explicitly viewer → admin.
    return True


def _forbid_viewer(event: dict):
    """Return a 403 response if the caller is a viewer, else None."""
    if _is_admin(event):
        return None
    return {
        "statusCode": 403,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": "forbidden", "reason": "admin role required"}),
    }


def _enrich_with_meta(clusters):
    if not clusters:
        return clusters
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ.get("CACHE_DB_CLUSTER_ARN", "")
    secret_arn = os.environ.get("CACHE_DB_SECRET_ARN", "")
    db = os.environ.get("CACHE_DB_NAME", "dbops")
    if not (cluster_arn and secret_arn):
        return clusters

    try:
        ids = [c["cluster_id"] for c in clusters]
        in_clause = ",".join([f":id{i}" for i in range(len(ids))])
        params = [{"name": f"id{i}", "value": {"stringValue": cid}} for i, cid in enumerate(ids)]
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db,
            sql=f"SELECT cluster_id, status, engine_version, storage_size_gb FROM cluster_meta WHERE cluster_id IN ({in_clause})",
            parameters=params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        meta_by_id = {}
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i]
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
            if row.get("cluster_id"):
                meta_by_id[row["cluster_id"]] = row
        for c in clusters:
            m = meta_by_id.get(c["cluster_id"], {})
            if m.get("status"):
                c["status"] = m["status"]
            if m.get("engine_version"):
                c["engine_version"] = m["engine_version"]
            if m.get("storage_size_gb") is not None:
                c["storage_size_gb"] = m["storage_size_gb"]
    except Exception as e:
        print(f"enrich error: {e}")

    # ETL health: per cluster, what's the freshest metric_snapshots row?
    # Anything older than 15 minutes is suspect — the ETL collector runs
    # every 5 minutes by default, so two consecutive misses = stale.
    try:
        resp2 = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db,
            sql=(
                "SELECT cluster_id, MAX(ts) AS latest_ts, COUNT(*) AS row_count "
                f"FROM metric_snapshots WHERE cluster_id IN ({in_clause}) "
                "AND ts > NOW() - INTERVAL '24 hours' "
                "GROUP BY cluster_id"
            ),
            parameters=params,
            includeResultMetadata=True,
        )
        meta2 = resp2.get("columnMetadata", [])
        cols2 = [c["name"] for c in meta2]
        # normalize ONLY timestamp columns (latest_ts) to ISO 8601 UTC so the
        # clusters page renders ETL freshness in local time, not 9h-skewed.
        col2_is_ts = [
            "timestamp" in (c.get("typeName") or "").lower() for c in meta2
        ]
        etl_by_id: dict = {}
        for rec in resp2.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols2[i]
                for typ in (
                    "stringValue",
                    "longValue",
                    "doubleValue",
                    "booleanValue",
                ):
                    if typ in f:
                        val = f[typ]
                        if (
                            typ == "stringValue"
                            and i < len(col2_is_ts)
                            and col2_is_ts[i]
                        ):
                            val = _norm_ts(val)
                        row[col] = val
                        break
            if row.get("cluster_id"):
                etl_by_id[row["cluster_id"]] = row

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for c in clusters:
            etl = etl_by_id.get(c["cluster_id"])
            if not etl or not etl.get("latest_ts"):
                c["etl_status"] = "no_data"
                c["etl_latest_ts"] = None
                c["etl_rows_24h"] = 0
                continue
            latest_str = etl["latest_ts"]
            c["etl_latest_ts"] = latest_str
            c["etl_rows_24h"] = etl.get("row_count") or 0
            try:
                latest = datetime.fromisoformat(
                    str(latest_str).replace("Z", "+00:00")
                )
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                age_sec = (now - latest).total_seconds()
                c["etl_status"] = (
                    "fresh" if age_sec <= 15 * 60 else "stale"
                )
            except (ValueError, TypeError):
                c["etl_status"] = "no_data"
    except Exception as e:
        print(f"etl enrich error: {e}")
        for c in clusters:
            c.setdefault("etl_status", "unknown")

    return clusters


def _cors():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _resp(status, body, max_age: int = 0):
    """Build the API Gateway response envelope. `max_age` (seconds)
    adds Cache-Control: private, max-age=N for 2xx GETs only — used
    by the cluster registry list, which is loaded on every sidebar
    nav and changes slowly."""
    headers = _cors()
    if max_age > 0 and 200 <= status < 300:
        headers = {**headers, "Cache-Control": f"private, max-age={int(max_age)}"}
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, default=str)}


def _session_for(region: str, role_arn: str = "") -> boto3.session.Session:
    """Return a boto3 Session for the target account+region. If role_arn is
    given, assume it cross-account so the same session can spawn rds /
    secretsmanager / etc. clients that all run as the spoke role."""
    if not role_arn:
        return boto3.session.Session(region_name=region)
    sts = boto3.client("sts")
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"dbops-discover-{datetime.utcnow().strftime('%H%M%S')}",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _rds_client_for(region: str, role_arn: str = ""):
    """Convenience wrapper kept for callers that only need RDS access
    (e.g. single-cluster register validation)."""
    return _session_for(region, role_arn).client("rds")


def _convention_secret_for(session: boto3.session.Session, cluster_id: str) -> str:
    """Look up the dbops convention secret for a cluster.

    Convention: `dbops/<cluster_id>/readonly`. If the secret exists in the
    target account+region we return its ARN — that's the credential the
    cluster will be registered with. If the secret is missing we return
    empty so the caller can fall back to the master user secret (and warn
    the user to run the dedicated-user setup).
    """
    secret_name = f"dbops/{cluster_id}/readonly"
    sm = session.client("secretsmanager")
    try:
        resp = sm.describe_secret(SecretId=secret_name)
        return resp.get("ARN", "")
    except sm.exceptions.ResourceNotFoundException:
        return ""
    except Exception as e:
        # Permission errors / throttling — log and fall back gracefully.
        print(f"[discover] convention secret lookup failed for {cluster_id}: {e}")
        return ""


def _list_clusters_in_region(region: str, role_arn: str = "") -> list[dict]:
    """Enumerate Aurora clusters in a region. For each cluster we attach:

      - `secret_arn`: convention secret if available, else master fallback
      - `secret_source`:
          - "convention"      → `dbops/<cluster_id>/readonly` exists in SM
          - "master_fallback" → no convention secret, using master user secret
          - "missing"         → neither found; cluster needs manual setup
      - `master_secret_arn`: kept for transparency (UI can show "currently using master")
    """
    session = _session_for(region, role_arn)
    rds = session.client("rds")
    out = []
    paginator = rds.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for c in page.get("DBClusters", []):
            engine = c.get("Engine", "")
            if not engine.startswith("aurora"):
                # Skip non-Aurora RDS (we only support Aurora MySQL/PG today).
                continue
            cluster_id = c.get("DBClusterIdentifier", "")
            master_secret = (c.get("MasterUserSecret") or {}).get("SecretArn", "")

            convention_secret = _convention_secret_for(session, cluster_id) if cluster_id else ""
            if convention_secret:
                resolved_secret = convention_secret
                source = "convention"
            elif master_secret:
                resolved_secret = master_secret
                source = "master_fallback"
            else:
                resolved_secret = ""
                source = "missing"

            out.append({
                "cluster_id": cluster_id,
                "cluster_arn": c.get("DBClusterArn", ""),
                "engine": engine,
                "engine_version": c.get("EngineVersion", ""),
                "endpoint": c.get("Endpoint", ""),
                "status": c.get("Status", ""),
                "db_name": c.get("DatabaseName", "") or "postgres",
                "secret_arn": resolved_secret,
                "master_secret_arn": master_secret,
                "secret_source": source,
                "region": region,
            })
    return out


def _handle_list(table):
    response = table.scan()
    items = response.get("Items", [])
    items = _enrich_with_meta(items)
    # 30s browser cache — cluster registry doesn't change between
    # admin actions, and EVERY page navigation hits this list.
    return _resp(200, items, max_age=30)


def _handle_register(table, body: dict):
    required = ["cluster_id", "account_id", "region"]
    for field in required:
        if field not in body:
            return _resp(400, {"error": f"{field} required"})

    cluster_id = body["cluster_id"]
    account_id = body["account_id"]
    region = body["region"]
    spoke_role_arn = body.get("spoke_role_arn", "")
    connection_status = "untested"
    connection_error = ""
    # Fields we resolve from RDS DescribeDBClusters so EXPLAIN / RDS Data API
    # calls work without a manual backfill. Caller-supplied values win.
    resolved_arn = ""
    resolved_secret = ""
    resolved_db = ""
    resolved_engine = ""

    # Validate access AND collect ARN/secret/db_name in one round trip.
    try:
        rds = _rds_client_for(region, spoke_role_arn)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = resp.get("DBClusters") or []
        if clusters:
            c = clusters[0]
            resolved_arn = c.get("DBClusterArn", "")
            resolved_secret = (c.get("MasterUserSecret") or {}).get("SecretArn", "")
            resolved_db = c.get("DatabaseName", "") or ""
            resolved_engine = c.get("Engine", "")
        connection_status = "ok"
    except Exception as e:
        connection_status = "failed"
        connection_error = str(e)[:300]

    item = {
        "cluster_id": cluster_id,
        "account_id": account_id,
        "region": region,
        "engine": body.get("engine") or resolved_engine or "aurora-postgresql",
        "spoke_role_arn": spoke_role_arn,
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": connection_status,
        "connection_error": connection_error,
        "connection_validated_at": (datetime.utcnow().isoformat() + "Z") if connection_status != "untested" else "",
    }
    # Auto-resolved ARN/secret/db_name from RDS describe go in unless the
    # caller (bulk-register path) supplied explicit overrides.
    cluster_arn = body.get("cluster_arn") or resolved_arn
    secret_arn = body.get("secret_arn") or resolved_secret
    db_name = body.get("db_name") or resolved_db
    if cluster_arn:
        item["cluster_arn"] = cluster_arn
    if secret_arn:
        item["secret_arn"] = secret_arn
    if db_name:
        item["db_name"] = db_name

    table.put_item(Item=item)

    status_code = 201 if connection_status != "failed" else 207
    return _resp(status_code, {
        "status": "registered" if connection_status != "failed" else "registered_with_warning",
        "cluster_id": cluster_id,
        "connection_status": connection_status,
        "connection_error": connection_error,
    })


def _handle_discover(table, body: dict):
    region = body.get("region")
    regions = body.get("regions") or ([region] if region else [])
    if not regions:
        return _resp(400, {"error": "region or regions required"})
    role_arn = body.get("role_arn", "")
    account_id = body.get("account_id", "")  # informational; only used in output

    # Build a set of already-registered cluster_ids to flag duplicates.
    existing_ids = set()
    try:
        scan = table.scan(ProjectionExpression="cluster_id")
        existing_ids = {row["cluster_id"] for row in scan.get("Items", []) if row.get("cluster_id")}
    except Exception as e:
        print(f"[discover] dedupe scan failed: {e}")

    all_clusters = []
    errors_by_region = {}
    for r in regions:
        try:
            rows = _list_clusters_in_region(r, role_arn)
            for row in rows:
                row["already_registered"] = row["cluster_id"] in existing_ids
                row["account_id"] = account_id
            all_clusters.extend(rows)
        except ClientError as e:
            errors_by_region[r] = e.response.get("Error", {}).get("Code", str(e))
        except Exception as e:
            errors_by_region[r] = str(e)[:200]

    return _resp(200, {
        "clusters": all_clusters,
        "errors": errors_by_region,
        "scanned_regions": regions,
    })


def _handle_bulk_register(table, body: dict):
    clusters = body.get("clusters") or []
    if not isinstance(clusters, list) or not clusters:
        return _resp(400, {"error": "clusters[] required"})

    registered, skipped, failed = [], [], []
    for c in clusters:
        try:
            cluster_id = c.get("cluster_id")
            if not cluster_id:
                failed.append({"cluster_id": "(missing)", "error": "cluster_id missing"})
                continue
            # Skip already-registered unless caller passes force=true.
            existing = table.get_item(Key={"cluster_id": cluster_id}).get("Item")
            if existing and not c.get("force"):
                skipped.append({"cluster_id": cluster_id, "reason": "already_registered"})
                continue
            sub_body = {
                "cluster_id": cluster_id,
                "account_id": c.get("account_id", ""),
                "region": c.get("region"),
                "engine": c.get("engine", "aurora-postgresql"),
                "spoke_role_arn": c.get("spoke_role_arn", ""),
                "cluster_arn": c.get("cluster_arn", ""),
                "secret_arn": c.get("secret_arn", ""),
                "db_name": c.get("db_name", ""),
            }
            resp = _handle_register(table, sub_body)
            payload = json.loads(resp["body"])
            registered.append({
                "cluster_id": cluster_id,
                "connection_status": payload.get("connection_status"),
            })
        except Exception as e:
            failed.append({"cluster_id": c.get("cluster_id", "?"), "error": str(e)[:200]})

    return _resp(200, {
        "registered": registered,
        "skipped": skipped,
        "failed": failed,
        "counts": {
            "registered": len(registered),
            "skipped": len(skipped),
            "failed": len(failed),
        },
    })


def _cache_db_env():
    return (
        os.environ.get("CACHE_DB_CLUSTER_ARN", ""),
        os.environ.get("CACHE_DB_SECRET_ARN", ""),
        os.environ.get("CACHE_DB_NAME", "dbops"),
    )


def _handle_seed_sample(table):
    """P1.4 Sample data / demo mode. Idempotent — re-running upserts the demo cluster."""
    cluster_arn, secret_arn, db_name = _cache_db_env()
    if not (cluster_arn and secret_arn):
        return _resp(500, {"error": "cache DB not configured"})

    cluster_id = seeder.SAMPLE_CLUSTER_ID
    rds_data = boto3.client("rds-data")
    try:
        counts = seeder.seed_demo_data(rds_data, cluster_arn, secret_arn, db_name, cluster_id)
    except Exception as e:
        return _resp(500, {"error": "seed_failed", "detail": str(e)[:300]})

    item = {
        "cluster_id": cluster_id,
        "account_id": "000000000000",
        "region": "ap-northeast-2",
        "engine": seeder.SAMPLE_ENGINE,
        "spoke_role_arn": "",
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": "ok",
        "connection_error": "",
        "connection_validated_at": datetime.utcnow().isoformat() + "Z",
        "is_demo": True,
    }
    table.put_item(Item=item)
    return _resp(201, {
        "status": "seeded",
        "cluster_id": cluster_id,
        "is_demo": True,
        "rows": counts,
    })


def _handle_delete(table, cluster_id: str):
    """DELETE /api/clusters/{cluster_id}. For demo clusters, also wipes cache rows."""
    existing = table.get_item(Key={"cluster_id": cluster_id}).get("Item")
    if not existing:
        return _resp(404, {"error": "not_found", "cluster_id": cluster_id})
    if existing.get("is_demo"):
        cluster_arn, secret_arn, db_name = _cache_db_env()
        if cluster_arn and secret_arn:
            try:
                seeder.cleanup_demo_data(boto3.client("rds-data"), cluster_arn, secret_arn, db_name, cluster_id)
            except Exception as e:
                print(f"[delete] cleanup_demo_data failed: {e}")
    table.delete_item(Key={"cluster_id": cluster_id})
    return _resp(200, {"status": "deleted", "cluster_id": cluster_id, "was_demo": bool(existing.get("is_demo"))})


def _test_connection(body: dict) -> dict:
    """Pre-flight: AssumeRole + DescribeDBClusters without persisting
    anything. Returns a structured verdict so the UI can show which
    specific step failed (vs the existing register-then-show-error
    path which just stamps `connection_status=failed` on a saved row).

    Steps run in order, and the first failure short-circuits the rest:
      1. assume_role  — only for cross-account; same-account skips this
      2. describe_cluster — confirms the role can see the cluster id
      3. master_user_secret — confirms Aurora has a managed secret
         (the agent needs this for RDS Data API)
    """
    cluster_id = (body.get("cluster_id") or "").strip()
    region = (body.get("region") or "").strip()
    spoke_role_arn = (body.get("spoke_role_arn") or "").strip()

    if not cluster_id or not region:
        return _resp(400, {"error": "cluster_id and region required"})

    steps = []

    # Step 1: AssumeRole (cross-account only)
    session: boto3.session.Session | None = None
    if spoke_role_arn:
        try:
            session = _session_for(region, spoke_role_arn)
            steps.append({"name": "assume_role", "status": "ok"})
        except Exception as e:
            steps.append({
                "name": "assume_role",
                "status": "failed",
                "error": str(e)[:300],
            })
            return _resp(200, {"ok": False, "steps": steps})
    else:
        session = _session_for(region)
        steps.append({
            "name": "assume_role",
            "status": "skipped",
            "note": "same-account — Lambda execution role used directly",
        })

    # Step 2: DescribeDBClusters
    try:
        rds = session.client("rds")
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster_list = resp.get("DBClusters") or []
        if not cluster_list:
            steps.append({
                "name": "describe_cluster",
                "status": "failed",
                "error": "cluster id not found in this account/region",
            })
            return _resp(200, {"ok": False, "steps": steps})
        cluster = cluster_list[0]
        steps.append({
            "name": "describe_cluster",
            "status": "ok",
            "engine": cluster.get("Engine", ""),
            "version": cluster.get("EngineVersion", ""),
            "endpoint": cluster.get("Endpoint", ""),
        })
    except Exception as e:
        steps.append({
            "name": "describe_cluster",
            "status": "failed",
            "error": str(e)[:300],
        })
        return _resp(200, {"ok": False, "steps": steps})

    # Step 3: master user secret (needed for RDS Data API path)
    secret_arn = (cluster.get("MasterUserSecret") or {}).get("SecretArn", "")
    if secret_arn:
        steps.append({
            "name": "master_user_secret",
            "status": "ok",
            "secret_arn": secret_arn,
        })
    else:
        steps.append({
            "name": "master_user_secret",
            "status": "warning",
            "note": (
                "Aurora cluster has no managed master secret — RDS Data API "
                "calls will fail. Enable Secrets Manager-managed credentials "
                "on the cluster or supply secret_arn manually."
            ),
        })

    # Step 4: Data API(HttpEndpoint). 컨트롤 플레인 점검만으로는 잡히지 않는
    # 가장 흔한 함정 — 꺼져 있으면 라이브 SQL 수집·에이전트 SQL이 전부 막히는데
    # 등록 자체는 성공하므로, 여기서 미리 경고해야 DBA가 영문 모를 빈 패널을
    # 보며 기다리는 사태를 막는다. 실패가 아닌 warning: CloudWatch 기반
    # 지표·이벤트 수집은 Data API 없이도 정상 동작한다.
    if cluster.get("HttpEndpointEnabled"):
        steps.append({"name": "data_api", "status": "ok"})
    else:
        steps.append({
            "name": "data_api",
            "status": "warning",
            "note": (
                "RDS Data API(HttpEndpoint)가 비활성입니다 — CloudWatch 지표는 수집되지만 "
                "라이브 SQL 기반 기능(테이블 통계·커넥션·Top Queries·에이전트 SQL)은 동작하지 않습니다. "
                f"활성화: aws rds modify-db-cluster --db-cluster-identifier {cluster_id} "
                "--enable-http-endpoint (다운타임 없음)"
            ),
        })

    return _resp(200, {"ok": True, "steps": steps})


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path", "")

    # Sub-route: POST /api/clusters/test-connection — pre-flight that
    # runs AssumeRole + DescribeDBClusters without saving. Read-only;
    # viewer allowed since the body is non-persisting.
    if method == "POST" and path.endswith("/test-connection"):
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return _resp(400, {"error": "body must be valid JSON"})
        return _test_connection(body)

    # Sub-route: POST /api/clusters/discover (read-only enumeration, allow viewer)
    if method == "POST" and path.endswith("/discover"):
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "invalid JSON"})
        return _handle_discover(table, body)

    # Sub-route: POST /api/clusters/sample — write, admin only
    if method == "POST" and path.endswith("/sample"):
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        return _handle_seed_sample(table)

    # Sub-route: DELETE /api/clusters/{cluster_id} — write, admin only
    if method == "DELETE":
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        # cluster_id from path parameters (API Gateway v2 {id} variable) or query string fallback.
        params = event.get("pathParameters") or {}
        cluster_id = params.get("id") or (event.get("queryStringParameters") or {}).get("cluster_id")
        if not cluster_id:
            return _resp(400, {"error": "cluster_id required"})
        return _handle_delete(table, cluster_id)

    # Sub-route: POST /api/clusters/bulk-register — write, admin only
    if method == "POST" and path.endswith("/bulk-register"):
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "invalid JSON"})
        return _handle_bulk_register(table, body)

    if method == "GET":
        return _handle_list(table)

    if method == "POST":
        # Single-cluster registration — write, admin only.
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "invalid JSON"})
        return _handle_register(table, body)

    return _resp(405, {"error": "method not allowed"})

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
from engine_family import dynamodb_cluster_id, engine_family

_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


def _scan_all(table, **kwargs) -> list:
    """LastEvaluatedKey를 끝까지 따라가는 scan. 단일 scan은 1MB에서 조용히
    잘려, fleet이 커지면 등록 목록·디스커버리 중복판별이 일부 클러스터만
    보게 된다(approvals _scan_all·approval_guard Limit=1과 같은 잘림 패밀리,
    Codex 감사 적발)."""
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


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
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and "dbops-admin" not in groups:
        return False
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


def _ddb_client_for(region: str, role_arn: str = ""):
    return _session_for(region, role_arn).client("dynamodb")


def _docdb_client_for(region: str, role_arn: str = ""):
    return _session_for(region, role_arn).client("docdb")


def _elasticache_client_for(region: str, role_arn: str = ""):
    return _session_for(region, role_arn).client("elasticache")


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


def _list_clusters_in_region(region: str, role_arn: str = "", account_id: str = "") -> list[dict]:
    """Enumerate Aurora clusters in a region. For each cluster we attach:

      - `secret_arn`: convention secret if available, else master fallback
      - `secret_source`:
          - "convention"      → `dbops/<cluster_id>/readonly` exists in SM
          - "master_fallback" → no convention secret, using master user secret
          - "missing"         → neither found; cluster needs manual setup
      - `master_secret_arn`: kept for transparency (UI can show "currently using master")

    `account_id` is threaded in from _handle_discover so that DynamoDB slug
    computation uses the same account_id that _register_dynamodb will use —
    this guarantees discovery and registration produce the same `ddb-*` cluster_id.
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

    # DynamoDB tables — best-effort; missing permission doesn't break Aurora discovery.
    # account_id is threaded from _handle_discover so the slug here matches the one
    # _register_dynamodb will compute (same account_id → same ddb-* cluster_id).
    try:
        dynamo = _session_for(region, role_arn).client("dynamodb")
        ddb_paginator = dynamo.get_paginator("list_tables")
        for ddb_page in ddb_paginator.paginate():
            for name in ddb_page.get("TableNames", []):
                out.append({
                    "cluster_id": dynamodb_cluster_id(account_id, region, name),
                    "resource_name": name,
                    "engine": "dynamodb",
                    "engine_family": "dynamodb",
                    "resource_type": "dynamodb-table",
                    "region": region,
                    "secret_source": "n/a",
                })
    except Exception as e:
        print(f"[discover] dynamodb list_tables failed in {region}: {e}")

    # DocumentDB clusters — best-effort.
    try:
        docdb = _session_for(region, role_arn).client("docdb")
        docdb_paginator = docdb.get_paginator("describe_db_clusters")
        for docdb_page in docdb_paginator.paginate():
            for c in docdb_page.get("DBClusters", []):
                # The docdb client shares the RDS control plane, so this call
                # returns EVERY cluster in the account (Aurora / RDS / Neptune /
                # DocumentDB) — not just DocumentDB. Without this guard, every
                # Aurora cluster already found via the rds paginator above is
                # added a second time mislabeled as "docdb". Keep only real
                # DocumentDB clusters.
                if c.get("Engine") != "docdb":
                    continue
                cid = c.get("DBClusterIdentifier", "")
                out.append({
                    "cluster_id": cid,
                    "engine": "docdb",
                    "engine_family": "documentdb",
                    "engine_version": c.get("EngineVersion", ""),
                    "resource_name": cid,
                    "resource_type": "docdb",
                    "status": c.get("Status", ""),
                    "region": region,
                    "secret_source": "n/a",
                })
    except Exception as e:
        print(f"[discover] docdb describe_db_clusters failed in {region}: {e}")

    # ElastiCache — replication groups (Redis/Valkey) then standalone cache clusters
    # (Memcached or non-cluster-mode Redis). Members of a replication group are
    # skipped in the cache-cluster pass to avoid duplicates.
    try:
        ec = _elasticache_client_for(region, role_arn)
        for rg in (ec.get_paginator("describe_replication_groups")
                   .paginate()):
            for g in rg.get("ReplicationGroups", []):
                rgid = g["ReplicationGroupId"]
                out.append({
                    "cluster_id": rgid,
                    "engine": "redis", "engine_family": "elasticache",
                    "resource_name": rgid,
                    "resource_type": "elasticache-redis",
                    "status": g.get("Status", ""),
                    "region": region,
                    "secret_source": "n/a",
                })
        for cc in (ec.get_paginator("describe_cache_clusters")
                   .paginate(ShowCacheNodeInfo=False)):
            for c in cc.get("CacheClusters", []):
                # replication-group members are already covered above; skip them
                if c.get("ReplicationGroupId"):
                    continue
                eng = (c.get("Engine") or "redis").lower()
                ccid = c["CacheClusterId"]
                out.append({
                    "cluster_id": ccid,
                    "engine": eng, "engine_family": "elasticache",
                    "resource_name": ccid,
                    "resource_type": f"elasticache-{eng}",
                    "status": c.get("CacheClusterStatus", ""),
                    "region": region,
                    "secret_source": "n/a",
                })
    except Exception as e:
        print(f"[discover] elasticache failed in {region}: {e}")

    return out


def _handle_list(table):
    items = _enrich_with_meta(_scan_all(table))
    # 30s browser cache — cluster registry doesn't change between
    # admin actions, and EVERY page navigation hits this list.
    return _resp(200, items, max_age=30)


def _register_dynamodb(table, body):
    for f in ("account_id", "region", "resource_name"):
        if not body.get(f):
            return _resp(400, {"error": f"{f} required"})
    account_id, region, name = body["account_id"], body["region"], body["resource_name"]
    status, err = "ok", ""
    try:
        _ddb_client_for(region, body.get("spoke_role_arn", "")).describe_table(TableName=name)
    except Exception as e:
        status, err = "failed", str(e)[:300]
    cid = dynamodb_cluster_id(account_id, region, name)
    item = {
        "cluster_id": cid, "account_id": account_id, "region": region,
        "engine": "dynamodb", "engine_family": "dynamodb",
        "resource_name": name, "resource_type": "dynamodb-table",
        "requires_secret_for_foundation": False,
        "spoke_role_arn": body.get("spoke_role_arn", ""),
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": status, "connection_error": err,
    }
    table.put_item(Item=item)
    return _resp(201 if status == "ok" else 207,
                 {"status": "registered" if status == "ok" else "registered_with_warning",
                  "cluster_id": cid, "connection_status": status})


def _register_docdb(table, body):
    for f in ("cluster_id", "account_id", "region"):
        if not body.get(f):
            return _resp(400, {"error": f"{f} required"})
    cluster_id, account_id, region = body["cluster_id"], body["account_id"], body["region"]
    status, err, version = "ok", "", ""
    try:
        resp = _docdb_client_for(region, body.get("spoke_role_arn", "")).describe_db_clusters(
            DBClusterIdentifier=cluster_id)
        cl = (resp.get("DBClusters") or [])
        if cl:
            version = cl[0].get("EngineVersion", "")
    except Exception as e:
        status, err = "failed", str(e)[:300]
    item = {
        "cluster_id": cluster_id, "account_id": account_id, "region": region,
        "engine": "docdb", "engine_family": "documentdb", "engine_version": version,
        "resource_name": cluster_id, "resource_type": "docdb",
        "requires_secret_for_foundation": False,
        "spoke_role_arn": body.get("spoke_role_arn", ""),
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": status, "connection_error": err,
    }
    table.put_item(Item=item)
    return _resp(201 if status == "ok" else 207,
                 {"status": "registered" if status == "ok" else "registered_with_warning",
                  "cluster_id": cluster_id, "connection_status": status})


def _register_elasticache(table, body):
    for f in ("account_id", "region", "resource_name"):
        if not body.get(f):
            return _resp(400, {"error": f"{f} required"})
    account_id, region, name = body["account_id"], body["region"], body["resource_name"]
    role_arn = body.get("spoke_role_arn", "")
    auth_secret_arn = body.get("auth_secret_arn", "")
    cli = _elasticache_client_for(region, role_arn)
    status, err = "ok", ""
    engine = (body.get("engine") or "redis").lower()
    details = {}
    # Try a Redis/Valkey replication group first; fall back to a standalone /
    # Memcached cache cluster (a name can be either).
    try:
        rg = (cli.describe_replication_groups(ReplicationGroupId=name)
              .get("ReplicationGroups") or [])
        if rg:
            g = rg[0]
            node_groups = g.get("NodeGroups") or []
            members = g.get("MemberClusters") or []
            details = {
                "engine": engine, "status": g.get("Status", ""),
                "cluster_mode": bool(g.get("ClusterEnabled", False)),
                "num_node_groups": len(node_groups),
                "replicas_per_node_group": max(0, (len(members) // max(1, len(node_groups))) - 1),
                "node_type": g.get("CacheNodeType", ""),
                "auth_enabled": bool(g.get("AuthTokenEnabled", False)),
                "tls_enabled": bool(g.get("TransitEncryptionEnabled", False)),
                "auth_secret_arn": auth_secret_arn,
            }
        else:
            raise Exception("no replication group")
    except Exception:
        try:
            cc = (cli.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)
                  .get("CacheClusters") or [])
            if cc:
                c = cc[0]
                engine = (c.get("Engine") or engine).lower()
                details = {
                    "engine": engine, "status": c.get("CacheClusterStatus", ""),
                    "engine_version": c.get("EngineVersion", ""),
                    "node_type": c.get("CacheNodeType", ""),
                    "num_cache_nodes": c.get("NumCacheNodes", 0),
                    "cluster_mode": False,
                    "auth_enabled": bool(c.get("AuthTokenEnabled", False)),
                    "tls_enabled": bool(c.get("TransitEncryptionEnabled", False)),
                    "auth_secret_arn": auth_secret_arn,
                }
            else:
                status, err = "failed", "not found"
        except Exception as e:
            status, err = "failed", str(e)[:300]
    item = {
        "cluster_id": name, "account_id": account_id, "region": region,
        "engine": engine, "engine_family": "elasticache",
        "resource_name": name, "resource_type": f"elasticache-{engine}",
        "resource_details": details,
        "requires_secret_for_foundation": False,
        "spoke_role_arn": role_arn,
        "auth_secret_arn": auth_secret_arn,
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": status, "connection_error": err,
    }
    table.put_item(Item=item)
    return _resp(201 if status == "ok" else 207,
                 {"status": "registered" if status == "ok" else "registered_with_warning",
                  "cluster_id": name, "connection_status": status})


def _handle_register(table, body: dict):
    fam = engine_family(body.get("engine", ""))
    if fam == "dynamodb":
        return _register_dynamodb(table, body)
    if fam == "documentdb":
        return _register_docdb(table, body)
    if fam == "elasticache":
        return _register_elasticache(table, body)
    # relational (Aurora) — existing path unchanged below

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
        existing_ids = {
            row["cluster_id"]
            for row in _scan_all(table, ProjectionExpression="cluster_id")
            if row.get("cluster_id")
        }
    except Exception as e:
        print(f"[discover] dedupe scan failed: {e}")

    all_clusters = []
    errors_by_region = {}
    # DBOps 자기 자신의 캐시 DB를 식별 — 디스커버리 결과에 같이 잡히는데
    # 기본 체크되면 모니터링 대상으로 실수 등록하기 쉽다(자기 자신을 자기가
    # 모니터링). UI가 자동 선택에서 빼고 배지를 달 수 있도록 마킹만 한다.
    cache_arn, _, _ = _cache_db_env()
    cache_cluster_id = cache_arn.rsplit(":", 1)[-1] if cache_arn else ""
    # DBOps's own DynamoDB control-plane tables (the cluster registry itself,
    # plus sessions / approvals / alert-dedup) all share the dbops-<env>- name
    # prefix — derive it from the registry table name. Without this,
    # list_tables surfaces the platform's own tables (even the registry backing
    # THIS very call) as monitorable databases. Flag them internal like the
    # cache DB so the UI de-selects + badges them instead of listing them as
    # candidates. Other tables (incl. other projects') stay discoverable.
    _clusters_table = os.environ.get("CLUSTERS_TABLE", "")
    dbops_ddb_prefix = (
        _clusters_table[: -len("clusters")]
        if _clusters_table.endswith("clusters")
        else ""
    )

    for r in regions:
        try:
            rows = _list_clusters_in_region(r, role_arn, account_id)
            for row in rows:
                row["already_registered"] = row["cluster_id"] in existing_ids
                row["account_id"] = account_id
                is_cache = bool(
                    cache_cluster_id and row["cluster_id"] == cache_cluster_id
                )
                is_dbops_ddb = bool(
                    row.get("engine") == "dynamodb"
                    and dbops_ddb_prefix
                    and (row.get("resource_name") or "").startswith(dbops_ddb_prefix)
                )
                row["is_internal"] = is_cache or is_dbops_ddb
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
                # Required for DynamoDB registration — _register_dynamodb checks
                # resource_name (the table name) as a required field.
                "resource_name": c.get("resource_name", ""),
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
                # Sv2·프로비저닝은 EnableHttpEndpoint(resource-arn) API다.
                # modify-db-cluster --enable-http-endpoint는 legacy Serverless
                # v1 전용으로, 그 외에선 조용히 무시된다(실측).
                f"활성화: aws rds enable-http-endpoint --resource-arn "
                f"{cluster.get('DBClusterArn', '<cluster-arn>')} (다운타임 없음, CLI v2)"
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

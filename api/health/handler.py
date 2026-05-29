"""DBOps self-monitoring health endpoint.

Aggregates the operational state of the things DBOps itself depends on
so the DBA can answer "is DBOps healthy?" without 4 AWS console tabs.

Surfaces:
  - Lambdas — the dbops-* function set, last-update + state
  - Aurora cache — cluster status, engine version, ACU range
  - DDB tables — sessions / clusters / approvals item-count + state

Each section is best-effort: an IAM hiccup against one source doesn't
500 the whole endpoint, just marks that section with `error`. The UI
renders whatever sections came back.

Route: GET /api/health
"""

from __future__ import annotations

import json
import os
import time

import boto3
from botocore.exceptions import ClientError


def _resp(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            # Generous cache — health state moves slowly, polling every
            # 10s is plenty.
            "Cache-Control": "private, max-age=10",
        },
        "body": json.dumps(body, default=str),
    }


def _list_lambdas() -> dict:
    """Return every Lambda whose name starts with the dbops env prefix.
    The DBOps stacks own four prefixes via CloudFormation logical IDs;
    rather than enumerate them, filter by the dbops- prefix which is
    consistent in CDK-generated names."""
    try:
        client = boto3.client("lambda")
        paginator = client.get_paginator("list_functions")
        funcs: list[dict] = []
        for page in paginator.paginate():
            for f in page.get("Functions", []):
                name = f.get("FunctionName", "")
                if not name.startswith("dbops-"):
                    continue
                funcs.append({
                    "name": name,
                    "runtime": f.get("Runtime"),
                    "state": f.get("State", "Active"),
                    "last_modified": f.get("LastModified"),
                    "memory_mb": f.get("MemorySize"),
                    "timeout_s": f.get("Timeout"),
                })
        funcs.sort(key=lambda x: x["name"])
        # Functional summary so the UI can render a 1-line health pill.
        active = sum(1 for f in funcs if f.get("state") == "Active")
        return {"count": len(funcs), "active": active, "items": funcs}
    except ClientError as e:
        return {"error": f"{e.response.get('Error', {}).get('Code')}: {str(e)[:200]}"}
    except Exception as e:
        return {"error": str(e)[:300]}


def _aurora_cache() -> dict:
    """Status of the cache DB (Aurora PostgreSQL Serverless v2) — the
    one Aurora cluster DBOps reads/writes to. Identified via env."""
    cluster_arn = os.environ.get("CACHE_DB_CLUSTER_ARN", "")
    if not cluster_arn:
        return {"error": "CACHE_DB_CLUSTER_ARN not configured"}
    # cluster arn = arn:aws:rds:<region>:<acct>:cluster:<id>
    parts = cluster_arn.split(":")
    if len(parts) < 7:
        return {"error": f"unexpected CACHE_DB_CLUSTER_ARN shape: {cluster_arn[:80]}"}
    cluster_id = parts[6]
    try:
        rds = boto3.client("rds")
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = resp.get("DBClusters") or []
        if not clusters:
            return {"error": "cluster not found"}
        c = clusters[0]
        sv2 = c.get("ServerlessV2ScalingConfiguration") or {}
        return {
            "cluster_id": cluster_id,
            "status": c.get("Status"),
            "engine": c.get("Engine"),
            "engine_version": c.get("EngineVersion"),
            "endpoint": c.get("Endpoint"),
            "serverless_min_acu": sv2.get("MinCapacity"),
            "serverless_max_acu": sv2.get("MaxCapacity"),
            "multi_az": c.get("MultiAZ"),
            "deletion_protection": c.get("DeletionProtection"),
        }
    except ClientError as e:
        return {"error": f"{e.response.get('Error', {}).get('Code')}: {str(e)[:200]}"}


def _ddb_tables() -> dict:
    """Status + approximate item count for the DBOps-owned DDB tables."""
    # Env vars are set by the agent stack on the health Lambda.
    names = []
    for env_key in ("CLUSTERS_TABLE", "SESSIONS_TABLE", "APPROVALS_TABLE"):
        v = os.environ.get(env_key)
        if v:
            names.append((env_key.lower().replace("_table", ""), v))
    if not names:
        return {"error": "no DDB table envs configured"}

    out = []
    try:
        ddb = boto3.client("dynamodb")
        for label, table_name in names:
            try:
                resp = ddb.describe_table(TableName=table_name)
                t = resp.get("Table") or {}
                out.append({
                    "label": label,
                    "name": table_name,
                    "status": t.get("TableStatus"),
                    "item_count": t.get("ItemCount"),  # ~6h stale per AWS
                    "size_bytes": t.get("TableSizeBytes"),
                })
            except ClientError as e:
                out.append({
                    "label": label,
                    "name": table_name,
                    "error": e.response.get("Error", {}).get("Code", str(e)),
                })
    except Exception as e:
        return {"error": str(e)[:300]}
    return {"tables": out}


def lambda_handler(event, context):
    started = time.time()
    payload = {
        "checked_at": int(started * 1000),
        "lambdas": _list_lambdas(),
        "aurora": _aurora_cache(),
        "ddb": _ddb_tables(),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    return _resp(200, payload)

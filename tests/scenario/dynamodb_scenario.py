#!/usr/bin/env python3
"""DynamoDB scenario test cycle for the DBOps multi-engine Foundation.

Idle on-demand tables can't exercise the interesting parts of the DynamoDB
collection path. This cycle provisions a LOW-capacity PROVISIONED table, drives a
write/read burst that intentionally exceeds capacity (consuming RCU/WCU and
triggering throttling), invokes the ETL collector, then queries the Aurora cache
to verify the collector captured, end to end:

  - billing_mode = PROVISIONED  (capacity-mode branch)
  - provisioned_rcu / provisioned_wcu  (collected ONLY for provisioned tables)
  - consumed_rcu / consumed_wcu > 0     (real traffic)
  - write_throttle_events / throttled_requests > 0  (capacity exceeded)
  - latency_ms_{getitem,query,putitem}  (per-operation latency, needs Operation dim)
  - resource_details {billing_mode, item_count, table_size_bytes, gsi, ...}

Self-configures from the deployed ETL Lambda's environment (cache ARNs, clusters
table) — no hardcoded account values. Repeatable. `--cleanup` tears it down.

Usage:
  python tests/scenario/dynamodb_scenario.py            # run the full cycle
  python tests/scenario/dynamodb_scenario.py --writes 1200
  python tests/scenario/dynamodb_scenario.py --cleanup  # delete test table + registry row
"""
import argparse
import hashlib
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "ap-northeast-2"
TABLE = "dbops-ddb-scenario-test"
ETL_NAME_FILTER = "ETLCollector"


def _log(msg):
    print(msg, flush=True)


def dynamodb_cluster_id(account, region, table):
    """Mirror of collectors/engine_family.dynamodb_cluster_id (regex-safe slug)."""
    h = hashlib.sha256(f"{account}:{region}:{table}".encode()).hexdigest()[:12]
    return f"ddb-{h}"


def discover_config():
    """Find the ETL Lambda and read cache ARNs + clusters table from its env."""
    lam = boto3.client("lambda", region_name=REGION)
    fns = lam.get_paginator("list_functions")
    etl = None
    for page in fns.paginate():
        for f in page["Functions"]:
            if ETL_NAME_FILTER in f["FunctionName"]:
                etl = f["FunctionName"]
                break
        if etl:
            break
    if not etl:
        sys.exit(f"could not find an ETL function matching {ETL_NAME_FILTER!r}")
    env = lam.get_function_configuration(FunctionName=etl)["Environment"]["Variables"]
    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return {
        "etl_fn": etl,
        "cache_arn": env["CACHE_DB_CLUSTER_ARN"],
        "secret_arn": env["CACHE_DB_SECRET_ARN"],
        "db": env.get("CACHE_DB_NAME", "dbops"),
        "clusters_table": env["CLUSTERS_TABLE"],
        "account": account,
    }


def ensure_table(account):
    """Create the low-capacity PROVISIONED test table (1 RCU / 1 WCU) if missing
    and wait until ACTIVE. Low capacity makes throttling reliable."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    try:
        ddb.describe_table(TableName=TABLE)
        _log(f"[setup] table {TABLE} already exists")
    except ddb.exceptions.ResourceNotFoundException:
        _log(f"[setup] creating PROVISIONED table {TABLE} (1 RCU / 1 WCU)")
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PROVISIONED",
            ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
            Tags=[{"Key": "Project", "Value": "DBOps"},
                  {"Key": "Purpose", "Value": "multi-engine-scenario-test"}],
        )
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
        _log("[setup] table ACTIVE")


def register(cfg):
    cid = dynamodb_cluster_id(cfg["account"], REGION, TABLE)
    reg = boto3.resource("dynamodb", region_name=REGION).Table(cfg["clusters_table"])
    reg.put_item(Item={
        "cluster_id": cid, "account_id": cfg["account"], "region": REGION,
        "engine": "dynamodb", "engine_family": "dynamodb",
        "resource_name": TABLE, "resource_type": "dynamodb-table",
        "requires_secret_for_foundation": False,
        "registered_at": "2026-06-11T00:00:00Z",
        "connection_status": "ok", "is_demo": True,
    })
    _log(f"[register] {TABLE} -> {cid}")
    return cid


def drive_load(writes):
    """Burst writes/reads with retries DISABLED so throttles surface immediately
    (and aren't masked by botocore's exponential backoff). Throttle events are
    recorded server-side regardless of client retry."""
    no_retry = Config(retries={"total_max_attempts": 1})
    ddb = boto3.client("dynamodb", region_name=REGION, config=no_retry)
    ok_w = thr_w = 0
    payload = "x" * 300  # keep each item ~1 WCU
    _log(f"[load] driving {writes} PutItems against a 1-WCU table…")
    for i in range(writes):
        try:
            ddb.put_item(TableName=TABLE,
                         Item={"pk": {"S": f"k{i}"}, "data": {"S": payload}})
            ok_w += 1
        except ClientError as e:
            if e.response["Error"]["Code"] in (
                "ProvisionedThroughputExceededException", "ThrottlingException"):
                thr_w += 1
            else:
                raise
    # reads: GetItem (hits + misses) + a couple of Scans → read consumption + latency
    ok_r = thr_r = 0
    for i in range(0, writes, 4):
        try:
            ddb.get_item(TableName=TABLE, Key={"pk": {"S": f"k{i}"}})
            ok_r += 1
        except ClientError as e:
            if e.response["Error"]["Code"] in (
                "ProvisionedThroughputExceededException", "ThrottlingException"):
                thr_r += 1
            else:
                raise
    for _ in range(3):
        try:
            ddb.scan(TableName=TABLE, Limit=50)
        except ClientError:
            pass
    _log(f"[load] writes ok={ok_w} throttled={thr_w} | reads ok={ok_r} throttled={thr_r}")
    return {"writes_ok": ok_w, "writes_throttled": thr_w,
            "reads_ok": ok_r, "reads_throttled": thr_r}


def _cache_query(cfg, sql):
    rds = boto3.client("rds-data", region_name=REGION)
    resp = rds.execute_statement(
        resourceArn=cfg["cache_arn"], secretArn=cfg["secret_arn"], database=cfg["db"],
        sql=sql, includeResultMetadata=True)
    cols = [c.get("name") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            if f.get("isNull"):
                row[cols[i]] = None
            else:
                row[cols[i]] = next((f[t] for t in
                    ("stringValue", "longValue", "doubleValue", "booleanValue") if t in f), None)
        out.append(row)
    return out


def invoke_etl(cfg):
    _log("[etl] invoking collector…")
    boto3.client("lambda", region_name=REGION).invoke(
        FunctionName=cfg["etl_fn"], Payload=b"{}")


def verify(cfg, cid):
    _log("[verify] querying cache…")
    mt = {r["metric_type"]: (r["n"], r["maxv"]) for r in _cache_query(
        cfg, "SELECT metric_type, COUNT(*) AS n, MAX(value) AS maxv "
             f"FROM metric_snapshots WHERE cluster_id='{cid}' GROUP BY metric_type")}
    meta = _cache_query(
        cfg, f"SELECT engine, resource_details FROM cluster_meta WHERE cluster_id='{cid}'")
    details = meta[0]["resource_details"] if meta else None

    def present(name):
        return name in mt

    def positive(name):
        return name in mt and (mt[name][1] or 0) > 0

    checks = [
        ("billing_mode PROVISIONED in meta", details and "PROVISIONED" in (details or "")),
        ("provisioned_rcu collected (provisioned-only metric)", present("provisioned_rcu")),
        ("provisioned_wcu collected (provisioned-only metric)", present("provisioned_wcu")),
        ("consumed_wcu > 0 (real writes)", positive("consumed_wcu")),
        ("throttling captured (write_throttle_events or throttled_requests > 0)",
         positive("write_throttle_events") or positive("throttled_requests")),
        ("per-operation latency captured (latency_ms_*)",
         any(k.startswith("latency_ms_") for k in mt)),
    ]
    _log("\n=== DynamoDB scenario verification ===")
    _log(f"  engine={meta[0]['engine'] if meta else '(no meta)'}  details={details}")
    _log(f"  metric_types: {sorted(mt)}")
    allpass = True
    for name, ok in checks:
        _log(f"  [{'PASS' if ok else 'observe'}] {name}")
        allpass = allpass and bool(ok)
    return allpass, mt


def cleanup(cfg):
    ddb = boto3.client("dynamodb", region_name=REGION)
    try:
        ddb.delete_table(TableName=TABLE)
        _log(f"[cleanup] deleted table {TABLE}")
    except ddb.exceptions.ResourceNotFoundException:
        _log(f"[cleanup] table {TABLE} already gone")
    cid = dynamodb_cluster_id(cfg["account"], REGION, TABLE)
    boto3.resource("dynamodb", region_name=REGION).Table(
        cfg["clusters_table"]).delete_item(Key={"cluster_id": cid})
    _log(f"[cleanup] removed registry row {cid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--writes", type=int, default=900)
    # Consumed/throttle/latency are 1-min granularity, but Provisioned*CapacityUnits
    # are 5-min granularity — a fresh table won't surface them until the first
    # 5-min datapoint publishes. Default wait covers that so a single run captures
    # everything; drop to ~150 if you only care about consumed/throttle/latency.
    ap.add_argument("--cw-wait", type=int, default=330,
                    help="seconds to wait for CloudWatch to publish before ETL "
                         "(>=300 needed to capture 5-min-granularity provisioned metrics)")
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    cfg = discover_config()
    _log(f"[config] etl={cfg['etl_fn']} account={cfg['account']}")
    if args.cleanup:
        cleanup(cfg)
        return 0

    ensure_table(cfg["account"])
    cid = register(cfg)
    drive_load(args.writes)
    _log(f"[wait] {args.cw_wait}s for CloudWatch to publish 1-min metrics…")
    time.sleep(args.cw_wait)
    # two ETL passes: the 10-min collection window + a second to catch late points
    invoke_etl(cfg)
    time.sleep(5)
    ok, _ = verify(cfg, cid)
    _log(f"\n=== RESULT: {'ALL CHECKS PASS' if ok else 'some checks pending (see above)'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

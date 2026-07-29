"""RDS MySQL deep-read collector (rds_instance family) — in-VPC, direct TCP.

Clone of docdb_mongo_collector's architecture: own registry scan + per-cluster
isolation; connects with pymysql over TLS (fail-closed, vendored
global-bundle.pem) using the registry row's endpoint/port + db_secret_arn
(RDS-managed master secrets carry ONLY username/password), then runs the
vendored Aurora-MySQL collectors unmodified through MySQLDataApiAdapter.
Cache writes go to the Aurora PG cache via RDS Data API, marker
/* source=dbops-rdsdirect */.

pymysql is imported lazily inside _connect (NOT at module top) so the unit
tests can patch _CONNECT_FACTORY and run without pymysql installed. The
vendored collectors + adapter are flat-module imports because the Lambda asset
root is this package dir (tests put it on sys.path while loading the module).
"""

import json
import os
from datetime import datetime, timezone

import boto3
from mssql_activity import collect_mssql_activity
from mssql_adapter import MSSQLDataApiAdapter
from mssql_perf_counters import collect_mssql_perf_counters
from mssql_query_stats import collect_mssql_query_stats
from mssql_settings import collect_mssql_settings
from mssql_waits import collect_mssql_waits
from mysql_activity import collect_mysql_activity
from mysql_adapter import MySQLDataApiAdapter
from mysql_innodb_status import collect_mysql_innodb_status
from mysql_locks import collect_mysql_locks
from mysql_query_stats import collect_mysql_query_stats
from mysql_table_stats import collect_mysql_table_stats
from schema_snapshot import collect_mysql_schema_snapshot

# TLS CA bundle for RDS, vendored into the asset during CDK bundling (Task 2).
# Resolved relative to this file so the path is valid in the deployed package.
_CA_BUNDLE_PATH = os.path.join(os.path.dirname(__file__), "global-bundle.pem")


def _connect(host, port, user, password, database):
    """Default pymysql connection factory. Imports pymysql lazily so the module
    can be loaded (and unit-tested) without pymysql installed. Tests patch this
    via the module-level _CONNECT_FACTORY hook below.

    Fail-closed TLS: refuse to connect if the CA bundle is missing rather than
    fall back to an unverified connection."""
    import pymysql  # lazy: not importable in the test env

    if not os.path.exists(_CA_BUNDLE_PATH):
        raise RuntimeError(
            f"TLS CA bundle missing at {_CA_BUNDLE_PATH} — refusing to connect "
            "without server certificate verification")
    return pymysql.connect(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        ssl_ca=_CA_BUNDLE_PATH,
        ssl_verify_cert=True,
        ssl_verify_identity=True,
        connect_timeout=8,
        read_timeout=30,
        autocommit=True,
    )


# Indirection so tests can inject a fake connection without importing pymysql.
_CONNECT_FACTORY = _connect


def _connect_mssql(host, port, user, password, database):
    """Default pytds connection factory for RDS SQL Server. Mirrors _connect:
    imports pytds lazily and fails closed if the CA bundle is missing.

    RDS SQL Server does NOT force SSL by default, so we always pass the vendored
    CA bundle with validate_host=True for verified TLS regardless of the
    instance's parameter group (matches shared/mssql_direct.connect)."""
    import pytds  # lazy: not importable in the test env

    if not os.path.exists(_CA_BUNDLE_PATH):
        raise RuntimeError(
            f"TLS CA bundle missing at {_CA_BUNDLE_PATH} — refusing to connect "
            "without server certificate verification")
    return pytds.connect(
        server=host,
        port=int(port),
        database=database or None,
        user=user,
        password=password,
        cafile=_CA_BUNDLE_PATH,
        validate_host=True,
        login_timeout=10,
        timeout=30,
        autocommit=True,
    )


_MSSQL_CONNECT_FACTORY = _connect_mssql


def _scan_all(table):
    """Paginated DynamoDB scan — never truncate at the 1MB page boundary."""
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _make_cache_execute(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name):
    """RDS-Data execute helper for the cache DB — mirrors etl_collector.handler."""

    def cache_execute(sql, params):
        sql_params = []
        for key, value in params.items():
            if value is None:
                sql_params.append({"name": key, "value": {"isNull": True}})
            elif isinstance(value, bool):
                sql_params.append({"name": key, "value": {"booleanValue": value}})
            elif isinstance(value, int):
                sql_params.append({"name": key, "value": {"longValue": value}})
            elif isinstance(value, float):
                sql_params.append({"name": key, "value": {"doubleValue": value}})
            else:
                sql_params.append({"name": key, "value": {"stringValue": str(value)}})
        # RETURNS the raw Data API response, which the schema_snapshot collector reads
        # its own previous blob back through this closure (see etl_collector).
        return rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql=f"/* source=dbops-rdsdirect */ {sql}", parameters=sql_params,
        )

    return cache_execute


def _eligible(rows):
    """Rows this collector owns: rds_instance-family MySQL or SQL Server with a
    usable secret and endpoint. Aurora (etl_collector) and other engines are
    skipped; a missing secret/endpoint means we can't connect, so skip too.
    Per-engine dispatch (mysql vs sqlserver) happens in _process_cluster."""
    out = []
    for row in rows:
        if row.get("engine_family") != "rds_instance":
            continue
        engine = row.get("engine") or ""
        if "mysql" not in engine and "sqlserver" not in engine:
            continue
        if not row.get("db_secret_arn") or not row.get("endpoint"):
            continue
        out.append(row)
    return out


def _process_cluster(row, secrets, cache_execute, run_ts):
    """Connect to one RDS MySQL instance and run the vendored collectors through
    the Data-API adapter. NEVER raises — any failure logs and returns an error
    marker so other clusters still run. Host/port come from the REGISTRY ROW
    (RDS-managed master secrets hold only username/password)."""
    cluster_id = row.get("cluster_id", "?")
    engine = row.get("engine") or ""
    conn = None
    try:
        raw = secrets.get_secret_value(SecretId=row["db_secret_arn"]).get("SecretString") or "{}"
        creds = json.loads(raw)
        user = creds.get("username")
        password = creds.get("password")

        if "sqlserver" in engine:
            conn = _MSSQL_CONNECT_FACTORY(
                host=row["endpoint"], port=row.get("port", 1433),
                user=user, password=password,
                database=row.get("db_name") or "master")
            adapter = MSSQLDataApiAdapter(conn)
            database = row.get("db_name") or "master"

            collected = {}
            # Per-collector try/except: one DMV read failing must not skip the
            # rest. target arns unused by the adapter → empty strings.
            # settings + perf_counters (E-3) read SERVER-scoped views
            # (sys.configurations, sys.dm_os_performance_counters), so they are
            # correct from this `master` session. A DATABASE-scoped DMV would
            # describe master's own system tables and must not be added here.
            for name, fn in (
                ("query_stats", collect_mssql_query_stats),
                ("activity", collect_mssql_activity),
                ("waits", collect_mssql_waits),
                ("settings", collect_mssql_settings),
                ("perf_counters", collect_mssql_perf_counters),
            ):
                try:
                    collected[name] = fn(adapter, cache_execute, "", "", cluster_id, database)
                except Exception as e:
                    collected[f"{name}_error"] = str(e)
                    print(f"[rdsdirect] {cluster_id} mssql {name} error: {e}")
            return {"cluster_id": cluster_id, "collected": collected}

        # system schema always exists — gives the session a default schema so
        # its own statements show up in the digest table.
        database = row.get("db_name") or "mysql"

        conn = _CONNECT_FACTORY(
            host=row["endpoint"], port=row.get("port", 3306),
            user=user, password=password, database=database)
        adapter = MySQLDataApiAdapter(conn)

        collected = {}
        # Order + per-collector try/except mirror etl_collector.handler's MySQL
        # branch: one collector failing must not skip the rest. target_cluster_arn
        # / target_secret_arn are unused by the adapter, so pass empty strings.
        try:
            collected["stats"] = collect_mysql_query_stats(
                adapter, cache_execute, "", "", cluster_id, database)
        except Exception as e:
            collected["stats_error"] = str(e)
            print(f"[rdsdirect] {cluster_id} mysql stats error: {e}")
        try:
            collected["table_stats"] = collect_mysql_table_stats(
                adapter, cache_execute, "", "", cluster_id, database)
        except Exception as e:
            collected["table_stats_error"] = str(e)
            print(f"[rdsdirect] {cluster_id} mysql table_stats error: {e}")
        try:
            collected["locks"] = collect_mysql_locks(
                adapter, cache_execute, "", "", cluster_id, database)
        except Exception as e:
            collected["locks_error"] = str(e)
            print(f"[rdsdirect] {cluster_id} mysql locks error: {e}")
        try:
            collected["activity"] = collect_mysql_activity(
                adapter, cache_execute, "", "", cluster_id, database)
        except Exception as e:
            collected["activity_error"] = str(e)
            print(f"[rdsdirect] {cluster_id} mysql activity error: {e}")
        try:
            collected["innodb_status"] = collect_mysql_innodb_status(
                adapter, cache_execute, "", "", cluster_id, database, snapshot_ts=run_ts)
        except Exception as e:
            collected["innodb_status_error"] = str(e)
            print(f"[rdsdirect] {cluster_id} mysql innodb status error: {e}")
        # RDS MySQL gets column-level schema history from the SAME collector file
        # as Aurora MySQL (byte-identical copy, parity-tested). Without this line
        # `sql`-gated get_schema_diff/get_schema_history would PASS an
        # rds_instance cluster (sql: True) into a table with no producer.
        try:
            collected["schema_snapshot"] = collect_mysql_schema_snapshot(
                adapter, cache_execute, "", "", cluster_id, database, snapshot_ts=run_ts)
        except Exception as e:
            collected["schema_snapshot_error"] = str(e)
            print(f"[rdsdirect] {cluster_id} mysql schema snapshot error: {e}")

        return {"cluster_id": cluster_id, "collected": collected}
    except Exception as e:
        print(f"[rdsdirect] {cluster_id} connect/collect failed: {e}")
        return {"cluster_id": cluster_id, "error": str(e)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    clusters_table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    rds_data = boto3.client("rds-data")
    secrets = boto3.client("secretsmanager")

    cache_cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    cache_secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    cache_db_name = os.environ.get("CACHE_DB_NAME", "dbops")

    cache_execute = _make_cache_execute(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name)

    # Single run_ts shared by every finding this tick so the dashboard's
    # MAX(snapshot_time) query returns them together (same contract as ETL).
    run_ts = datetime.now(timezone.utc).isoformat()

    results = []
    for row in _eligible(_scan_all(clusters_table)):
        results.append(_process_cluster(row, secrets, cache_execute, run_ts))

    return {"statusCode": 200, "body": json.dumps({"processed": len(results), "results": results}, default=str)}

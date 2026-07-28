import json
import os
from datetime import datetime, timezone

import boto3
from collectors.capacity_forecast import collect_capacity_forecast
from collectors.cost_check import collect_cost_findings
from collectors.cw_collector import collect_cw_instance_metrics, collect_cw_metrics
from collectors.docdb_cw_collector import collect_docdb_metrics
from collectors.docdb_findings import collect_docdb_findings
from collectors.dynamodb_cw_collector import collect_dynamodb_metrics
from collectors.dynamodb_findings import collect_dynamodb_findings
from collectors.elasticache_cw_collector import collect_elasticache_metrics
from collectors.elasticache_findings import collect_elasticache_findings
from collectors.engine_family import engine_family
from collectors.incident_embeddings import collect_incident_embeddings
from collectors.meta_collector import collect_cluster_meta
from collectors.mysql_activity import collect_mysql_activity
from collectors.mysql_health_checks import collect_mysql_health_checks
from collectors.mysql_innodb_status import collect_mysql_innodb_status
from collectors.mysql_locks import collect_mysql_locks
from collectors.mysql_param_fitness import collect_mysql_param_fitness
from collectors.mysql_query_stats import collect_mysql_query_stats
from collectors.mysql_table_stats import collect_mysql_table_stats
from collectors.pg_activity import collect_pg_activity
from collectors.pg_baseline_trainer import collect_pg_baselines
from collectors.pg_engine_internals import collect_pg_engine_internals
from collectors.pg_extensions import collect_pg_extensions
from collectors.pg_health_checks import collect_pg_health_checks
from collectors.pg_locks import collect_pg_locks
from collectors.pg_param_fitness import collect_param_fitness
from collectors.pg_table_stats import collect_pg_table_stats
from collectors.pi_collector import PI_METRICS_RDS_INSTANCE, collect_pi_metrics
from collectors.query_regression import collect_query_regression
from collectors.rds_instance_cw_collector import collect_rds_instance_metrics
from collectors.schema_snapshot import (
    collect_mysql_schema_snapshot,
    collect_pg_schema_snapshot,
)
from collectors.stats_collector import collect_query_stats

# schema_snapshots retention, 90 days. The `<>` MAX(...) guard is load-bearing:
# the CURRENT snapshot of a schema that has not changed in 90 days is itself older
# than the cutoff, and deleting it would destroy the only row get_schema_diff has
# to compare the next change against. Retention prunes HISTORY, never the
# baseline. Module-level so the unit test can execute it on a real engine.
SCHEMA_SNAPSHOTS_PURGE_SQL = (
    "DELETE FROM schema_snapshots s "
    "WHERE s.snapshot_time < NOW() - INTERVAL '90 days' "
    "AND s.snapshot_time <> (SELECT MAX(x.snapshot_time) FROM schema_snapshots x "
    "WHERE x.cluster_id = s.cluster_id AND x.schema_name = s.schema_name)"
)


def _session_for(region, role_arn=""):
    """A boto3 Session for a registered cluster's account+region.

    Registered clusters can live in spoke accounts; each registry row carries a
    ``spoke_role_arn`` (empty for same-account deploys). With a role we assume
    it (hub-spoke chaining) so every RDS / PI / CloudWatch / RDS-Data call for
    that cluster runs in the cluster's OWN account — mirroring api ``_session_for``.
    With no role this is a transparent local session, so single-account
    behaviour is unchanged. Creds are scoped to one collection run (900s, well
    over the 5-min ETL window); sessions are cached PER INVOCATION only (see
    ``lambda_handler``) so warm containers never reuse expired credentials."""
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName="dbops-etl",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _scan_all(table):
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _train_baselines(result, cache_rds_data, cache_cluster_arn, cache_secret_arn,
                     cache_db_name, cluster_id):
    """Seasonal baseline trainer, called from EVERY family branch.

    Engine-agnostic by construction: it reads only cache-DB `metric_snapshots`
    cluster-level rows and writes `metric_baselines`, so nothing in it is
    Aurora- or SQL-specific. Without this call a family stays permanently on the
    low-confidence flat mean/stddev anomaly fallback (`mode='flat'`) even though
    its series are already in the cache.

    Takes NO snapshot_ts: unlike the finding collectors (which must share the
    cycle's run_ts so the dashboard's MAX(snapshot_time) batch holds them all),
    the trainer stamps its own NOW() into metric_baselines.updated_at, which is
    also its once-per-hour recompute gate.
    """
    try:
        result["baselines"] = collect_pg_baselines(
            cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
        )
    except Exception as e:
        result["baselines_error"] = str(e)
        print(f"[{cluster_id}] baseline trainer error: {e}")


def _collect_one(resource, get_client, cache_rds_data, cache_execute,
                 cache_cluster_arn, cache_secret_arn, cache_db_name, run_ts):
    """Collect metrics/findings for a single registered resource.

    Dispatches on engine_family BEFORE making any RDS/PI/CloudWatch call so
    that DynamoDB and DocumentDB rows never hit Aurora-specific APIs.
    The relational branch is the verbatim existing body — no behaviour change.
    """
    cluster_id = resource["cluster_id"]
    account_id = resource.get("account_id", "")
    region = resource.get("region", os.environ.get("AWS_REGION", "ap-northeast-2"))
    engine = resource.get("engine", "aurora-postgresql")

    result = {"cluster_id": cluster_id}
    # Fix 3: prefer an explicit engine_family field set during registration so dispatch
    # is robust even if the engine string is malformed or uses a variant spelling.
    family = resource.get("engine_family") or engine_family(engine)

    # ------------------------------------------------------------------
    # DynamoDB path — no RDS/PI/SQL/findings calls
    # ------------------------------------------------------------------
    if family == "dynamodb":
        cw = get_client("cloudwatch", region)
        dynamo = get_client("dynamodb", region)
        table_name = resource.get("resource_name", cluster_id)
        try:
            result["dynamodb"] = collect_dynamodb_metrics(
                cw, dynamo, cache_execute, cluster_id, table_name, account_id, region,
            )
        except Exception as e:
            result["dynamodb_error"] = str(e)
            print(f"[{cluster_id}] dynamodb error: {e}")
        try:
            result["dynamodb_findings"] = collect_dynamodb_findings(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["dynamodb_findings_error"] = str(e)
            print(f"[{cluster_id}] dynamodb findings error: {e}")
        # BEFORE the early return: this branch never reaches the shared tail.
        _train_baselines(result, cache_rds_data, cache_cluster_arn, cache_secret_arn,
                         cache_db_name, cluster_id)
        return result

    # ------------------------------------------------------------------
    # DocumentDB path — no RDS-Aurora/PI/SQL/findings calls
    # ------------------------------------------------------------------
    if family == "documentdb":
        cw = get_client("cloudwatch", region)
        docdb = get_client("docdb", region)
        try:
            result["documentdb"] = collect_docdb_metrics(
                cw, docdb, cache_execute, cluster_id, region, account_id,
            )
        except Exception as e:
            result["documentdb_error"] = str(e)
            print(f"[{cluster_id}] documentdb error: {e}")
        try:
            result["documentdb_findings"] = collect_docdb_findings(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["documentdb_findings_error"] = str(e)
            print(f"[{cluster_id}] documentdb findings error: {e}")
        _train_baselines(result, cache_rds_data, cache_cluster_arn, cache_secret_arn,
                         cache_db_name, cluster_id)
        return result

    # ------------------------------------------------------------------
    # ElastiCache path — no RDS/PI/SQL/findings calls
    # ------------------------------------------------------------------
    if family == "elasticache":
        cw = get_client("cloudwatch", region)
        ec = get_client("elasticache", region)
        resource_name = resource.get("resource_name", cluster_id)
        engine = resource.get("engine", "redis")
        try:
            result["elasticache"] = collect_elasticache_metrics(
                cw, ec, cache_execute, cluster_id, resource_name, engine, region, account_id,
            )
        except Exception as e:
            result["elasticache_error"] = str(e)
            print(f"[{cluster_id}] elasticache error: {e}")
        try:
            result["elasticache_findings"] = collect_elasticache_findings(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
                cluster_id, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["elasticache_findings_error"] = str(e)
            print(f"[{cluster_id}] elasticache findings error: {e}")
        _train_baselines(result, cache_rds_data, cache_cluster_arn, cache_secret_arn,
                         cache_db_name, cluster_id)
        return result

    # ------------------------------------------------------------------
    # RDS instance path (non-Aurora MySQL / SQL Server) — instance-dimensioned
    # CW + meta + PI; no Aurora-cluster/Data-API calls
    # ------------------------------------------------------------------
    if family == "rds_instance":
        cw = get_client("cloudwatch", region)
        rds_client = get_client("rds", region)
        try:
            r = collect_rds_instance_metrics(
                cw, rds_client, cache_execute, cluster_id, region, account_id)
            result["rds_instance"] = r
        except Exception as e:
            result["rds_instance_error"] = str(e)
            print(f"[{cluster_id}] rds_instance error: {e}")
            return result
        if r.get("pi_enabled") and r.get("resource_id"):
            try:
                pi_client = get_client("pi", region)
                result["pi"] = collect_pi_metrics(
                    pi_client, cache_execute, r["resource_id"], cluster_id,
                    metrics=PI_METRICS_RDS_INSTANCE)
            except Exception as e:
                result["pi_error"] = str(e)
                print(f"[{cluster_id}] pi error: {e}")
        # MySQL param_fitness reads ONLY the cache DB (cluster_settings +
        # metric_snapshots), so it runs here with no VPC/target connection.
        # InnoDB-status findings need the target and live in rds_direct_collector.
        # SQL Server has no cache-only finding.
        if "mysql" in engine:
            try:
                result["param_fitness"] = collect_mysql_param_fitness(
                    cache_rds_data, cache_cluster_arn, cache_secret_arn,
                    cache_db_name, cluster_id, snapshot_ts=run_ts)
            except Exception as e:
                result["param_fitness_error"] = str(e)
                print(f"[{cluster_id}] param_fitness error: {e}")
        # Engine-agnostic cache-only advisory collectors — same set the relational
        # branch runs, all reading ONLY the hub cache DB (no target/VPC connection):
        # cost, capacity/storage forecast, query regression, seasonal baselines.
        # capacity_forecast picks up the DECREASING free_storage_bytes series that
        # rds_instance writes (Aurora has none) → disk-exhaustion ETA. All share
        # run_ts so this cycle's findings land in one MAX(snapshot_time) batch —
        # EXCEPT baselines, which writes its own NOW() into metric_baselines.
        try:
            result["cost"] = collect_cost_findings(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts)
        except Exception as e:
            result["cost_error"] = str(e)
            print(f"[{cluster_id}] cost error: {e}")
        try:
            result["capacity_forecast"] = collect_capacity_forecast(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts, engine=engine)
        except Exception as e:
            result["capacity_forecast_error"] = str(e)
            print(f"[{cluster_id}] capacity_forecast error: {e}")
        try:
            result["query_regression"] = collect_query_regression(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts)
        except Exception as e:
            result["query_regression_error"] = str(e)
            print(f"[{cluster_id}] query_regression error: {e}")
        _train_baselines(result, cache_rds_data, cache_cluster_arn, cache_secret_arn,
                         cache_db_name, cluster_id)
        return result

    # ------------------------------------------------------------------
    # Relational path (Aurora PostgreSQL / MySQL) — verbatim existing body
    # ------------------------------------------------------------------
    target_cluster_arn = resource.get("cluster_arn", "")
    target_secret_arn = resource.get("secret_arn", "")
    target_db = resource.get("db_name", "sampledb")

    rds_client = get_client("rds", region)
    pi_client = get_client("pi", region)
    cw_client = get_client("cloudwatch", region)
    # Target-DB SQL (pg_stat_*, information_schema, SHOW ...) over the RDS Data
    # API must run in the cluster's OWN account, so it uses the spoke-bound
    # client from get_client. Cache writes keep using cache_rds_data, which is
    # always the hub account where the Aurora PG cache lives.
    target_rds_data = get_client("rds-data", region)

    # 이 사이클의 모든 finding collector(health/cost/param_fitness)가
    # 공유하는 단일 snapshot_time. 대시보드 _health_findings는
    # MAX(snapshot_time) 한 배치만 반환하므로, collector마다 now()를
    # 따로 찍으면 마지막 것만 보이고 나머지 finding이 사라진다.

    try:
        result["meta"] = collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region)
    except Exception as e:
        result["meta_error"] = str(e)
        print(f"[{cluster_id}] meta error: {e}")

    try:
        instances = rds_client.describe_db_instances(
            Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}],
        )["DBInstances"]
        if instances:
            resource_id = instances[0]["DbiResourceId"]
            result["pi"] = collect_pi_metrics(pi_client, cache_execute, resource_id, cluster_id)
        else:
            result["pi"] = {"skipped": "no instances"}
    except Exception as e:
        result["pi_error"] = str(e)
        print(f"[{cluster_id}] pi error: {e}")

    try:
        result["cw"] = collect_cw_metrics(cw_client, cache_execute, cluster_id)
    except Exception as e:
        result["cw_error"] = str(e)
        print(f"[{cluster_id}] cw error: {e}")

    try:
        meta_instances = (result.get("meta") or {}).get("instances") or []
        result["cw_instance"] = collect_cw_instance_metrics(
            cw_client, cache_execute, cluster_id, meta_instances
        )
    except Exception as e:
        result["cw_instance_error"] = str(e)
        print(f"[{cluster_id}] cw_instance error: {e}")

    if target_cluster_arn and target_secret_arn and "postgresql" in engine:
        try:
            result["stats"] = collect_query_stats(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["stats_error"] = str(e)
            print(f"[{cluster_id}] stats error: {e}")

        try:
            result["table_stats"] = collect_pg_table_stats(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["table_stats_error"] = str(e)
            print(f"[{cluster_id}] table_stats error: {e}")

        try:
            result["activity"] = collect_pg_activity(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["activity_error"] = str(e)
            print(f"[{cluster_id}] activity error: {e}")

        try:
            result["locks"] = collect_pg_locks(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["locks_error"] = str(e)
            print(f"[{cluster_id}] locks error: {e}")

        try:
            result["health"] = collect_pg_health_checks(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["health_error"] = str(e)
            print(f"[{cluster_id}] health error: {e}")
        try:
            result["param_fitness"] = collect_param_fitness(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["param_fitness_error"] = str(e)
            print(f"[{cluster_id}] param fitness error: {e}")
        try:
            result["capacity_forecast"] = collect_capacity_forecast(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts, engine=engine,
            )
        except Exception as e:
            result["capacity_forecast_error"] = str(e)
            print(f"[{cluster_id}] capacity forecast error: {e}")
        try:
            result["schema_snapshot"] = collect_pg_schema_snapshot(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn,
                cluster_id, target_db, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["schema_snapshot_error"] = str(e)
            print(f"[{cluster_id}] schema snapshot error: {e}")
        try:
            result["extensions"] = collect_pg_extensions(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["extensions_error"] = str(e)
            print(f"[{cluster_id}] extensions error: {e}")
        try:
            result["engine_internals"] = collect_pg_engine_internals(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn,
                cluster_id, target_db, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["engine_internals_error"] = str(e)
            print(f"[{cluster_id}] engine internals error: {e}")
    elif target_cluster_arn and target_secret_arn and "mysql" in engine:
        # MySQL counterparts — same cache tables, MySQL-flavored source queries.
        try:
            result["stats"] = collect_mysql_query_stats(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["stats_error"] = str(e)
            print(f"[{cluster_id}] mysql stats error: {e}")
        try:
            result["table_stats"] = collect_mysql_table_stats(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["table_stats_error"] = str(e)
            print(f"[{cluster_id}] mysql table_stats error: {e}")
        try:
            result["schema_snapshot"] = collect_mysql_schema_snapshot(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn,
                cluster_id, target_db, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["schema_snapshot_error"] = str(e)
            print(f"[{cluster_id}] mysql schema snapshot error: {e}")
        try:
            result["locks"] = collect_mysql_locks(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["locks_error"] = str(e)
            print(f"[{cluster_id}] mysql locks error: {e}")
        try:
            result["activity"] = collect_mysql_activity(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
            )
        except Exception as e:
            result["activity_error"] = str(e)
            print(f"[{cluster_id}] mysql activity error: {e}")
        # param_fitness는 cluster_settings(위 mysql_locks가 갱신)에 의존하므로
        # locks 뒤에 둔다. capacity_forecast는 엔진 무관(metric_snapshots 캐시).
        try:
            result["param_fitness"] = collect_mysql_param_fitness(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["param_fitness_error"] = str(e)
            print(f"[{cluster_id}] mysql param fitness error: {e}")
        # Maintenance Health의 MySQL 대응(pg_health_checks는 postgresql 분기 전용).
        # param_fitness와 같은 이유로 locks 뒤: cluster_settings를 읽는다. table_stats도
        # 캐시에서 읽으므로 타깃 접속은 없다. run_ts 공유는 필수 —
        # 대시보드가 MAX(snapshot_time) 배치로 findings를 읽는다.
        # ponytail: 이 수집기 자체는 엔진 중립(InnoDB 사실만 쓴다)이라 rds_instance
        # 분기에도 한 줄로 붙일 수 있다. E-2 범위가 Aurora MySQL이라 지금은 걸지
        # 않았고, RDS MySQL에서 검증한 뒤 붙일 것.
        try:
            result["health"] = collect_mysql_health_checks(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["health_error"] = str(e)
            print(f"[{cluster_id}] mysql health checks error: {e}")
        try:
            result["capacity_forecast"] = collect_capacity_forecast(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts, engine=engine,
            )
        except Exception as e:
            result["capacity_forecast_error"] = str(e)
            print(f"[{cluster_id}] mysql capacity forecast error: {e}")
        try:
            result["innodb_status"] = collect_mysql_innodb_status(
                target_rds_data, cache_execute, target_cluster_arn, target_secret_arn,
                cluster_id, target_db, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["innodb_status_error"] = str(e)
            print(f"[{cluster_id}] mysql innodb status error: {e}")
    else:
        result["stats"] = {"skipped": f"engine={engine} or no secret"}

    # Query latency-regression findings — engine-agnostic, reads query_stats
    # (which both the PG + MySQL stats collectors above populate this run).
    if target_cluster_arn and target_secret_arn and ("postgresql" in engine or "mysql" in engine):
        try:
            result["query_regression"] = collect_query_regression(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
                snapshot_ts=run_ts,
            )
        except Exception as e:
            result["query_regression_error"] = str(e)
            print(f"[{cluster_id}] query regression error: {e}")

    _train_baselines(result, cache_rds_data, cache_cluster_arn, cache_secret_arn,
                     cache_db_name, cluster_id)

    try:
        result["cost"] = collect_cost_findings(
            cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
            snapshot_ts=run_ts,
        )
    except Exception as e:
        result["cost_error"] = str(e)
        print(f"[{cluster_id}] cost check error: {e}")

    return result


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    clusters_table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    # cache_rds_data always targets the hub-account Aurora PG cache where every
    # collector WRITES its results. Per-cluster TARGET reads use a spoke-bound
    # client built below (get_client), so cross-account clusters are collected
    # in their own account.
    cache_rds_data = boto3.client("rds-data")

    cache_cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    cache_secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    cache_db_name = os.environ.get("CACHE_DB_NAME", "dbops")

    def cache_execute(sql, params):
        sql_params = []
        for key, value in params.items():
            if value is None:
                # str(None) == "None" used to flow into stringValue and blow up
                # numeric casts ("invalid input syntax for type double
                # precision") — which silently dropped cluster_meta for every
                # PROVISIONED cluster (sv2_min/max_acu are None there).
                sql_params.append({"name": key, "value": {"isNull": True}})
            elif isinstance(value, bool):
                sql_params.append({"name": key, "value": {"booleanValue": value}})
            elif isinstance(value, int):
                sql_params.append({"name": key, "value": {"longValue": value}})
            elif isinstance(value, float):
                sql_params.append({"name": key, "value": {"doubleValue": value}})
            else:
                sql_params.append({"name": key, "value": {"stringValue": str(value)}})
        # RETURNS the raw Data API response. Almost every caller is an INSERT and
        # ignores it, but the schema_snapshot collector has to READ its own
        # previous blob back out of the cache to decide whether anything changed,
        # and this closure is the only cache handle a collector is given.
        return cache_rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql=f"/* source=dbops-etl */ {sql}", parameters=sql_params,
        )

    # Per-INVOCATION caches: a spoke role is assumed at most once per (role, region)
    # and reused across that cluster's service clients, but never carried across
    # invocations (assumed-role creds expire; warm containers must re-assume).
    session_cache = {}
    client_cache = {}

    def make_get_client(role_arn):
        """get_client(service, region) bound to one cluster's account. With no
        spoke role this is a plain local client — identical to the previous
        single-account behaviour."""
        def get_client(service, region):
            sk = (role_arn, region)
            if sk not in session_cache:
                session_cache[sk] = _session_for(region, role_arn)
            ck = (role_arn, region, service)
            if ck not in client_cache:
                client_cache[ck] = session_cache[sk].client(service)
            return client_cache[ck]
        return get_client

    clusters = _scan_all(clusters_table)
    results = []
    for resource in clusters:
        get_client = make_get_client(resource.get("spoke_role_arn", ""))
        results.append(_collect_one(
            resource, get_client, cache_rds_data, cache_execute,
            cache_cluster_arn, cache_secret_arn, cache_db_name,
            datetime.now(timezone.utc).isoformat(),
        ))

    # Retention: metric_snapshots has no purge and grows unbounded (PARTITION BY
    # RANGE (ts) but only the DEFAULT partition exists). Keep ~90 days. The BRIN
    # index on ts (schema_v20) makes this an instant block-range check, so running
    # it every invocation is cheap — it matches 0 rows until data ages past the
    # window. Best-effort: a purge failure must never break collection.
    # ponytail: DELETE over partition rotation; revisit at large-fleet scale.
    try:
        cache_rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql="/* source=dbops-etl */ DELETE FROM metric_snapshots "
                "WHERE ts < NOW() - INTERVAL '90 days'",
        )
    except Exception as e:
        print(f"[etl] metric_snapshots purge failed: {type(e).__name__}: {e}")

    # query_stats grows unbounded the same way (per-query aggregates every
    # collection cycle). Keep ~90 days: the SLO latency SLI reads up to 90d of
    # query_stats, so do NOT shorten this window. Own try/except so a purge
    # failure never breaks collection nor the metric_snapshots purge above.
    # ponytail: DELETE over partition rotation; revisit at large-fleet scale.
    try:
        cache_rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql="/* source=dbops-etl */ DELETE FROM query_stats "
                "WHERE snapshot_time < NOW() - INTERVAL '90 days'",
        )
    except Exception as e:
        print(f"[etl] query_stats purge failed: {type(e).__name__}: {e}")

    # schema_snapshots: 90 days, same best-effort shape and BRIN-backed cheapness
    # as the two purges above (brin_schema_snapshots_time, schema_v26). Store-on-
    # change means this normally matches 0 rows.
    #
    # The NOT-the-latest guard is load-bearing, not tidiness: the CURRENT snapshot
    # of a schema that has not changed in 90 days is older than the cutoff, and
    # deleting it would destroy the only thing get_schema_diff has to compare the
    # next change against. Retention prunes HISTORY, never the baseline.
    # ponytail: correlated subquery over a window function; the table holds a
    # handful of rows per cluster, so this never needs to be clever.
    #
    # Hoisted to SCHEMA_SNAPSHOTS_PURGE_SQL (module level) so the unit test can
    # EXECUTE it against a real PostgreSQL server and assert the surviving rows,
    # instead of grepping the handler for a substring.
    try:
        cache_rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql=f"/* source=dbops-etl */ {SCHEMA_SNAPSHOTS_PURGE_SQL}",
        )
    except Exception as e:
        print(f"[etl] schema_snapshots purge failed: {type(e).__name__}: {e}")

    # Incident-similarity embeddings: backfill a bounded batch of un-embedded
    # event_log / runbook rows (Titan → pgvector) so find_similar_incidents can do
    # semantic cosine search. Best-effort; the tool keyword-falls-back meanwhile.
    try:
        collect_incident_embeddings(cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name)
    except Exception as e:
        print(f"[etl] incident embeddings failed: {type(e).__name__}: {e}")

    return {"statusCode": 200, "body": json.dumps({"collected": len(results), "results": results}, default=str)}

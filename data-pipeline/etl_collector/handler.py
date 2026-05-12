import os
import json
import boto3
from collectors.meta_collector import collect_cluster_meta
from collectors.pi_collector import collect_pi_metrics
from collectors.stats_collector import collect_query_stats
from collectors.cw_collector import collect_cw_metrics
from collectors.pg_table_stats import collect_pg_table_stats
from collectors.pg_activity import collect_pg_activity
from collectors.pg_locks import collect_pg_locks
from collectors.pg_health_checks import collect_pg_health_checks
from collectors.pg_extensions import collect_pg_extensions
from collectors.pg_baseline_trainer import collect_pg_baselines
from collectors.cost_check import collect_cost_findings
from collectors.mysql_query_stats import collect_mysql_query_stats
from collectors.mysql_table_stats import collect_mysql_table_stats
from collectors.mysql_locks import collect_mysql_locks
from collectors.mysql_activity import collect_mysql_activity


def _scan_all(table):
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    clusters_table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    rds_data = boto3.client("rds-data")

    cache_cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    cache_secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    cache_db_name = os.environ.get("CACHE_DB_NAME", "dbops")

    def cache_execute(sql, params):
        sql_params = []
        for key, value in params.items():
            if isinstance(value, bool):
                sql_params.append({"name": key, "value": {"booleanValue": value}})
            elif isinstance(value, int):
                sql_params.append({"name": key, "value": {"longValue": value}})
            elif isinstance(value, float):
                sql_params.append({"name": key, "value": {"doubleValue": value}})
            else:
                sql_params.append({"name": key, "value": {"stringValue": str(value)}})
        rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql=f"/* source=dbops-etl */ {sql}", parameters=sql_params,
        )

    client_cache = {}

    def get_client(service, region):
        key = (service, region)
        if key not in client_cache:
            client_cache[key] = boto3.client(service, region_name=region)
        return client_cache[key]

    clusters = _scan_all(clusters_table)
    results = []

    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        account_id = cluster.get("account_id", "")
        region = cluster.get("region", os.environ.get("AWS_REGION", "ap-northeast-2"))
        engine = cluster.get("engine", "aurora-postgresql")
        target_cluster_arn = cluster.get("cluster_arn", "")
        target_secret_arn = cluster.get("secret_arn", "")
        target_db = cluster.get("db_name", "sampledb")

        rds_client = get_client("rds", region)
        pi_client = get_client("pi", region)
        cw_client = get_client("cloudwatch", region)
        result = {"cluster_id": cluster_id}

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

        if target_cluster_arn and target_secret_arn and "postgresql" in engine:
            try:
                result["stats"] = collect_query_stats(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["stats_error"] = str(e)
                print(f"[{cluster_id}] stats error: {e}")

            try:
                result["table_stats"] = collect_pg_table_stats(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["table_stats_error"] = str(e)
                print(f"[{cluster_id}] table_stats error: {e}")

            try:
                result["activity"] = collect_pg_activity(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["activity_error"] = str(e)
                print(f"[{cluster_id}] activity error: {e}")

            try:
                result["locks"] = collect_pg_locks(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["locks_error"] = str(e)
                print(f"[{cluster_id}] locks error: {e}")

            try:
                result["health"] = collect_pg_health_checks(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["health_error"] = str(e)
                print(f"[{cluster_id}] health error: {e}")
            try:
                result["extensions"] = collect_pg_extensions(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["extensions_error"] = str(e)
                print(f"[{cluster_id}] extensions error: {e}")
        elif target_cluster_arn and target_secret_arn and "mysql" in engine:
            # MySQL counterparts — same cache tables, MySQL-flavored source queries.
            try:
                result["stats"] = collect_mysql_query_stats(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["stats_error"] = str(e)
                print(f"[{cluster_id}] mysql stats error: {e}")
            try:
                result["table_stats"] = collect_mysql_table_stats(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["table_stats_error"] = str(e)
                print(f"[{cluster_id}] mysql table_stats error: {e}")
            try:
                result["locks"] = collect_mysql_locks(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["locks_error"] = str(e)
                print(f"[{cluster_id}] mysql locks error: {e}")
            try:
                result["activity"] = collect_mysql_activity(
                    rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db,
                )
            except Exception as e:
                result["activity_error"] = str(e)
                print(f"[{cluster_id}] mysql activity error: {e}")
        else:
            result["stats"] = {"skipped": f"engine={engine} or no secret"}

        # Seasonal baseline trainer — engine-agnostic, only reads cache DB
        # metric_snapshots. Time-gated to once per hour per cluster.
        try:
            result["baselines"] = collect_pg_baselines(
                rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
            )
        except Exception as e:
            result["baselines_error"] = str(e)
            print(f"[{cluster_id}] baseline trainer error: {e}")

        try:
            result["cost"] = collect_cost_findings(
                rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
            )
        except Exception as e:
            result["cost_error"] = str(e)
            print(f"[{cluster_id}] cost check error: {e}")

        results.append(result)

    return {"statusCode": 200, "body": json.dumps({"collected": len(results), "results": results}, default=str)}

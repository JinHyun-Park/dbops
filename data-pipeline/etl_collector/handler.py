import os
import json
import boto3
from collectors.meta_collector import collect_cluster_meta
from collectors.pi_collector import collect_pi_metrics
from collectors.stats_collector import collect_query_stats


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
            if isinstance(value, int):
                sql_params.append({"name": key, "value": {"longValue": value}})
            elif isinstance(value, float):
                sql_params.append({"name": key, "value": {"doubleValue": value}})
            else:
                sql_params.append({"name": key, "value": {"stringValue": str(value)}})
        rds_data.execute_statement(
            resourceArn=cache_cluster_arn, secretArn=cache_secret_arn, database=cache_db_name,
            sql=f"/* source=dbops-etl */ {sql}", parameters=sql_params,
        )

    response = clusters_table.scan()
    clusters = response.get("Items", [])
    results = []

    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        account_id = cluster.get("account_id", "")
        region = cluster.get("region", os.environ.get("AWS_REGION", "ap-northeast-2"))
        engine = cluster.get("engine", "aurora-postgresql")
        target_cluster_arn = cluster.get("cluster_arn", "")
        target_secret_arn = cluster.get("secret_arn", "")
        target_db = cluster.get("db_name", "sampledb")

        rds_client = boto3.client("rds", region_name=region)
        pi_client = boto3.client("pi", region_name=region)
        result = {"cluster_id": cluster_id}

        try:
            meta = collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region)
            result["meta"] = meta
        except Exception as e:
            result["meta_error"] = str(e)
            print(f"[{cluster_id}] meta error: {e}")

        try:
            instances = rds_client.describe_db_instances(
                Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}],
            )["DBInstances"]
            if instances:
                resource_id = instances[0]["DbiResourceId"]
                pi = collect_pi_metrics(pi_client, cache_execute, resource_id, cluster_id)
                result["pi"] = pi
            else:
                result["pi"] = {"skipped": "no instances"}
        except Exception as e:
            result["pi_error"] = str(e)
            print(f"[{cluster_id}] pi error: {e}")

        if target_cluster_arn and target_secret_arn and "postgresql" in engine:
            try:
                stats = collect_query_stats(rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, target_db)
                result["stats"] = stats
            except Exception as e:
                result["stats_error"] = str(e)
                print(f"[{cluster_id}] stats error: {e}")
        else:
            result["stats"] = {"skipped": f"engine={engine} or no secret"}

        results.append(result)

    return {"statusCode": 200, "body": json.dumps({"collected": len(results), "results": results}, default=str)}

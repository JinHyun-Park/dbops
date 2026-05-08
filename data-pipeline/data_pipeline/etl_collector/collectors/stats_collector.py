def collect_query_stats(rds_data_client, cache_execute, cluster_arn, secret_arn, cluster_id, database="dbops"):
    sql = """SELECT queryid::text as query_hash, query as query_text,
               calls, total_exec_time as total_time_ms,
               mean_exec_time as mean_time_ms, rows as rows_returned,
               shared_blks_hit, shared_blks_read
        FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 100"""

    response = rds_data_client.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=database,
        sql=f"/* source=dbops-agent */ {sql}", includeResultMetadata=True,
    )

    inserted = 0
    for record in response.get("records", []):
        insert_sql = """INSERT INTO query_stats (cluster_id, snapshot_time, query_hash, query_text,
            calls, total_time_ms, mean_time_ms, rows_returned, shared_blks_hit, shared_blks_read)
            VALUES (:cluster_id, NOW(), :query_hash, :query_text,
            :calls, :total_time_ms, :mean_time_ms, :rows_returned, :shared_blks_hit, :shared_blks_read)"""
        fields = record
        params = {
            "cluster_id": cluster_id,
            "query_hash": fields[0].get("stringValue", ""),
            "query_text": fields[1].get("stringValue", ""),
            "calls": fields[2].get("longValue", 0),
            "total_time_ms": fields[3].get("doubleValue", 0.0),
            "mean_time_ms": fields[4].get("doubleValue", 0.0),
            "rows_returned": fields[5].get("longValue", 0),
            "shared_blks_hit": fields[6].get("longValue", 0),
            "shared_blks_read": fields[7].get("longValue", 0),
        }
        cache_execute(insert_sql, params)
        inserted += 1
    return {"cluster_id": cluster_id, "queries_collected": inserted}

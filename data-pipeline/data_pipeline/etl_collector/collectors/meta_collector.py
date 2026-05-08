def collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region):
    response = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    cluster = response["DBClusters"][0]

    sql = """
        INSERT INTO cluster_meta (cluster_id, account_id, region, engine, engine_version,
            status, endpoint, reader_endpoint, storage_size_gb, updated_at)
        VALUES (:cluster_id, :account_id, :region, :engine, :engine_version,
            :status, :endpoint, :reader_endpoint, :storage_size_gb, NOW())
        ON CONFLICT (cluster_id) DO UPDATE SET
            engine_version = EXCLUDED.engine_version,
            status = EXCLUDED.status,
            storage_size_gb = EXCLUDED.storage_size_gb,
            updated_at = NOW()
    """
    params = {
        "cluster_id": cluster_id,
        "account_id": account_id,
        "region": region,
        "engine": cluster["Engine"],
        "engine_version": cluster["EngineVersion"],
        "status": cluster["Status"],
        "endpoint": cluster.get("Endpoint", ""),
        "reader_endpoint": cluster.get("ReaderEndpoint", ""),
        "storage_size_gb": cluster.get("AllocatedStorage", 0),
    }
    cache_execute(sql, params)
    return {"cluster_id": cluster_id, "status": cluster["Status"]}

def collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region):
    response = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    cluster = response["DBClusters"][0]

    # Serverless v2 carries ScalingConfiguration with MinCapacity/MaxCapacity
    # in ACUs (Aurora Capacity Units). Provisioned clusters return no such
    # field, so we leave the columns NULL — cost_check uses NULL as the
    # signal to skip the ACU advice.
    sv2 = cluster.get("ServerlessV2ScalingConfiguration") or {}
    engine_mode = cluster.get("EngineMode") or ("serverless" if sv2 else "provisioned")

    sql = """
        INSERT INTO cluster_meta (
            cluster_id, account_id, region, engine, engine_version,
            status, endpoint, reader_endpoint, storage_size_gb,
            backup_retention_days, earliest_restorable_time, latest_restorable_time,
            preferred_backup_window, preferred_maintenance_window,
            multi_az, deletion_protection,
            engine_mode, serverlessv2_min_acu, serverlessv2_max_acu,
            updated_at
        )
        VALUES (
            :cluster_id, :account_id, :region, :engine, :engine_version,
            :status, :endpoint, :reader_endpoint, :storage_size_gb,
            :backup_retention_days,
            CASE WHEN :earliest_restorable_str='' THEN NULL ELSE :earliest_restorable_str::timestamptz END,
            CASE WHEN :latest_restorable_str='' THEN NULL ELSE :latest_restorable_str::timestamptz END,
            :preferred_backup_window, :preferred_maintenance_window,
            :multi_az, :deletion_protection,
            :engine_mode, :sv2_min_acu, :sv2_max_acu,
            NOW()
        )
        ON CONFLICT (cluster_id) DO UPDATE SET
            engine_version = EXCLUDED.engine_version,
            status = EXCLUDED.status,
            storage_size_gb = EXCLUDED.storage_size_gb,
            backup_retention_days = EXCLUDED.backup_retention_days,
            earliest_restorable_time = EXCLUDED.earliest_restorable_time,
            latest_restorable_time = EXCLUDED.latest_restorable_time,
            preferred_backup_window = EXCLUDED.preferred_backup_window,
            preferred_maintenance_window = EXCLUDED.preferred_maintenance_window,
            multi_az = EXCLUDED.multi_az,
            deletion_protection = EXCLUDED.deletion_protection,
            engine_mode = EXCLUDED.engine_mode,
            serverlessv2_min_acu = EXCLUDED.serverlessv2_min_acu,
            serverlessv2_max_acu = EXCLUDED.serverlessv2_max_acu,
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
        "backup_retention_days": cluster.get("BackupRetentionPeriod", 0),
        "earliest_restorable_str": cluster["EarliestRestorableTime"].isoformat() if cluster.get("EarliestRestorableTime") else "",
        "latest_restorable_str": cluster["LatestRestorableTime"].isoformat() if cluster.get("LatestRestorableTime") else "",
        "preferred_backup_window": cluster.get("PreferredBackupWindow", ""),
        "preferred_maintenance_window": cluster.get("PreferredMaintenanceWindow", ""),
        "multi_az": bool(cluster.get("MultiAZ", False)),
        "deletion_protection": bool(cluster.get("DeletionProtection", False)),
        "engine_mode": engine_mode,
        "sv2_min_acu": float(sv2["MinCapacity"]) if sv2.get("MinCapacity") is not None else None,
        "sv2_max_acu": float(sv2["MaxCapacity"]) if sv2.get("MaxCapacity") is not None else None,
    }
    cache_execute(sql, params)
    return {"cluster_id": cluster_id, "status": cluster["Status"]}

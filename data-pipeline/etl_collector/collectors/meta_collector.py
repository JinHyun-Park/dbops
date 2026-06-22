import json


def _build_instance_list(rds_client, cluster_id: str, members: list) -> list:
    """[{"id","role","class"}] for every cluster member. role from
    DBClusterMembers.IsClusterWriter; class from a single filtered
    describe_db_instances. On describe failure, returns role-only entries
    (class "") so the picker still works."""
    roles = {
        m.get("DBInstanceIdentifier"): ("writer" if m.get("IsClusterWriter") else "reader")
        for m in members
        if m.get("DBInstanceIdentifier")
    }
    classes = {}
    try:
        resp = rds_client.describe_db_instances(
            Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}]
        )
        classes = {
            i["DBInstanceIdentifier"]: i.get("DBInstanceClass", "")
            for i in resp.get("DBInstances", [])
        }
    except Exception as e:
        print(f"[meta] instance list class lookup failed for {cluster_id}: {e}")
    return [
        {"id": iid, "role": role, "class": classes.get(iid, "")}
        for iid, role in roles.items()
    ]


def _writer_instance_class(rds_client, cluster_id: str, members: list) -> str:
    """Writer 인스턴스의 DBInstanceClass. Sv2는 "db.serverless", 프로비저닝은
    db.r6g.large 같은 실제 클래스 — 한 필드로 두 모드를 다 표현한다.
    describe 권한 문제 등으로 실패하면 빈 문자열(컬럼 유지)."""
    writer_id = next(
        (m.get("DBInstanceIdentifier") for m in members if m.get("IsClusterWriter")),
        None,
    ) or (members[0].get("DBInstanceIdentifier") if members else None)
    if not writer_id:
        return ""
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=writer_id)
        return resp["DBInstances"][0].get("DBInstanceClass", "")
    except Exception as e:
        print(f"[meta] instance class lookup failed for {cluster_id}: {e}")
        return ""


def collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region):
    response = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    cluster = response["DBClusters"][0]

    # Serverless v2 carries ScalingConfiguration with MinCapacity/MaxCapacity
    # in ACUs (Aurora Capacity Units). Provisioned clusters return no such
    # field, so we leave the columns NULL — cost_check uses NULL as the
    # signal to skip the ACU advice.
    sv2 = cluster.get("ServerlessV2ScalingConfiguration") or {}
    engine_mode = cluster.get("EngineMode") or ("serverless" if sv2 else "provisioned")
    instance_class = _writer_instance_class(
        rds_client, cluster_id, cluster.get("DBClusterMembers") or []
    )
    instances = _build_instance_list(
        rds_client, cluster_id, cluster.get("DBClusterMembers") or []
    )

    sql = """
        INSERT INTO cluster_meta (
            cluster_id, account_id, region, engine, engine_version,
            status, endpoint, reader_endpoint, storage_size_gb,
            backup_retention_days, earliest_restorable_time, latest_restorable_time,
            preferred_backup_window, preferred_maintenance_window,
            multi_az, deletion_protection,
            engine_mode, serverlessv2_min_acu, serverlessv2_max_acu,
            instance_class, http_endpoint_enabled,
            instances,
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
            :instance_class, :http_endpoint_enabled,
            :instances::jsonb,
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
            instance_class = COALESCE(NULLIF(EXCLUDED.instance_class, ''), cluster_meta.instance_class),
            http_endpoint_enabled = EXCLUDED.http_endpoint_enabled,
            instances = EXCLUDED.instances,
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
        "instance_class": instance_class,
        "http_endpoint_enabled": bool(cluster.get("HttpEndpointEnabled", False)),
        "instances": json.dumps(instances),
    }
    cache_execute(sql, params)
    return {"cluster_id": cluster_id, "status": cluster["Status"], "instances": instances}

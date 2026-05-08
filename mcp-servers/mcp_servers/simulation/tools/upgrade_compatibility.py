import boto3
from mcp_servers.shared.cache_client import CacheClient


def check_upgrade_compatibility_impl(cache: CacheClient, cluster_id: str, target_version: str) -> dict:
    meta_sql = "SELECT engine, engine_version FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    current = meta.rows[0] if meta.rows else {}

    rds = boto3.client("rds")
    resp = rds.describe_db_engine_versions(
        Engine=current.get("engine", "aurora-postgresql"),
        EngineVersion=target_version,
    )
    target_info = resp["DBEngineVersions"][0] if resp.get("DBEngineVersions") else {}

    valid_targets = rds.describe_db_engine_versions(
        Engine=current.get("engine", "aurora-postgresql"),
        EngineVersion=current.get("engine_version", ""),
    )
    upgradable = []
    for v in valid_targets.get("DBEngineVersions", []):
        for t in v.get("ValidUpgradeTarget", []):
            upgradable.append(t["EngineVersion"])

    is_compatible = target_version in upgradable

    return {
        "cluster_id": cluster_id,
        "current_version": current.get("engine_version", "unknown"),
        "target_version": target_version,
        "is_compatible": is_compatible,
        "valid_upgrade_targets": upgradable[:10],
        "target_info": {
            "version": target_info.get("EngineVersion", ""),
            "description": target_info.get("DBEngineVersionDescription", ""),
        },
    }

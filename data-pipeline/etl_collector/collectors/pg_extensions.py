"""Sync installed pg_extension rows into cache `cluster_extensions`.

We UPSERT installed extensions and DELETE rows for any extension that's no
longer present (operator dropped it). plpgsql is excluded because it ships
with every PG cluster and adds noise.
"""

LIST_SQL = """
SELECT extname, extversion
FROM pg_extension
WHERE extname <> 'plpgsql'
ORDER BY extname
"""


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def collect_pg_extensions(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl-ext */ {LIST_SQL}",
        includeResultMetadata=True,
    )
    installed: list[tuple[str, str]] = []
    for rec in resp.get("records", []):
        installed.append((_str(rec[0]), _str(rec[1])))

    # Upsert installed rows.
    for extname, extversion in installed:
        cache_execute(
            "INSERT INTO cluster_extensions (cluster_id, extname, extversion, updated_at) "
            "VALUES (:cluster_id, :extname, :extversion, NOW()) "
            "ON CONFLICT (cluster_id, extname) DO UPDATE SET "
            "  extversion = EXCLUDED.extversion, updated_at = NOW()",
            {"cluster_id": cluster_id, "extname": extname, "extversion": extversion},
        )

    # Remove rows for extensions no longer present.
    if installed:
        # Build a NOT IN clause via bind params.
        param_names = []
        params = {"cluster_id": cluster_id}
        for i, (name, _) in enumerate(installed):
            pn = f"keep{i}"
            param_names.append(f":{pn}")
            params[pn] = name
        cache_execute(
            f"DELETE FROM cluster_extensions "
            f"WHERE cluster_id = :cluster_id AND extname NOT IN ({','.join(param_names)})",
            params,
        )
    else:
        cache_execute(
            "DELETE FROM cluster_extensions WHERE cluster_id = :cluster_id",
            {"cluster_id": cluster_id},
        )

    return {"cluster_id": cluster_id, "extensions_synced": len(installed)}

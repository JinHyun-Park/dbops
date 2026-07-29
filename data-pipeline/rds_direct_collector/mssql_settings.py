"""SQL Server server-level options -> cluster_settings (E-3).

The rds_instance family had a config READ surface for nobody: mysql_locks fills
cluster_settings for RDS MySQL, SQL Server had no producer at all (MEASURED 0
rows for dbops-demo-mssql), so /api/dashboard/{id}/settings returned an empty
list for half the family.

sys.configurations is SERVER-scoped, which is why this collector is correct from
the `master` session handler.py already opens: a database-scoped DMV would have
described master's own system tables instead of the user data. MEASURED live on
dbops-demo-mssql: 84 rows total, of which this collector keeps the curated
_TRACKED subset (same shape as mysql_locks.SETTINGS_SQL keeping 17 of MySQL's
several hundred global variables).

PENDING vs RUNNING, and why the two are NOT the same question:
  `value` is the CONFIGURED value, `value_in_use` is what the engine is actually
  running. They diverge for two different reasons and the wording must not
  conflate them:
    - is_dynamic = 0 -> the change needs a restart to take effect.
    - is_dynamic = 1 -> the engine itself adjusted/clamped the value; no restart
      will change it.
  MEASURED on the fixture: `min server memory (MB)` is configured 0 but in use
  16, with is_dynamic = 1. Calling that "restart required" would have been a
  confident wrong answer, which is exactly the failure class this tier removes.

`value` in cluster_settings is therefore always the RUNNING value (what a DBA
sees when they query the server), and the divergence is reported in `unit`,
which the frontend SettingsPanel renders as a suffix after the value.
"""

# Curated set. Every name below was MEASURED PRESENT in sys.configurations on
# SQL Server 2019 Express (sqlserver-ex 15.00.4470.1.v1). A name that is absent
# on another edition/version simply yields no row: this collector never invents
# a setting it did not read.
#
# These are sys.configurations names, which are NOT spelled the way the RDS API
# spells the same options. MEASURED on dbops-demo-mssql: of the 23 names below,
# 7 differ from describe_db_parameters by case only ('Agent XPs' vs 'agent xps',
# 'max server memory (MB)' vs 'max server memory (mb)'), 6 are IsModifiable=false
# in the parameter group, and 2 ('backup checksum default',
# 'blocked process threshold (s)') have no RDS parameter at all. The write tool
# (operations/tools/modify_rds_instance_params.py) therefore matches parameter
# names case-INSENSITIVELY and answers in the API's spelling, so every name shown
# here is a name a DBA can act on. If a name is added here, that is the contract
# to keep: displayed implies acceptable.
_TRACKED = (
    # memory
    "max server memory (MB)",
    "min server memory (MB)",
    "min memory per query (KB)",
    "index create memory (KB)",
    # parallelism: the two options that actually decide plan shape
    "max degree of parallelism",
    "cost threshold for parallelism",
    # workload / connections
    "max worker threads",
    "user connections",
    "optimize for ad hoc workloads",
    "network packet size (B)",
    "remote query timeout (s)",
    "query governor cost limit",
    # diagnosability
    "blocked process threshold (s)",
    "default trace enabled",
    # durability / storage
    "backup checksum default",
    "recovery interval (min)",
    "fill factor (%)",
    # surface area
    "clr enabled",
    "xp_cmdshell",
    "Ad Hoc Distributed Queries",
    "lightweight pooling",
    "priority boost",
    "Agent XPs",
)

# CAST to VARCHAR because sys.configurations.value is sql_variant, which the
# pytds -> Data-API-shape adapter would otherwise hand back in a type the row
# reader has no field for.
SETTINGS_SQL = """
SELECT
  RTRIM(name)                     AS name,
  CAST(value AS VARCHAR(64))        AS configured,
  CAST(value_in_use AS VARCHAR(64)) AS running,
  CAST(is_dynamic AS INT)           AS is_dynamic
FROM sys.configurations
WHERE name IN ({placeholders})
ORDER BY name
""".strip()


UPSERT_SETTING_SQL = (
    "INSERT INTO cluster_settings (cluster_id, name, value, unit, updated_at) "
    "VALUES (:cluster_id, :name, :value, :unit, NOW()) "
    "ON CONFLICT (cluster_id, name) DO UPDATE SET "
    "  value = EXCLUDED.value, unit = EXCLUDED.unit, updated_at = NOW()"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def _sql_literal(name):
    """Single-quoted T-SQL literal for a known-constant option name.

    The values come from the module-level _TRACKED tuple, never from a caller,
    so this is not a user-input boundary; the quote doubling is here so a name
    containing an apostrophe could never build a broken statement.
    """
    return "'" + str(name).replace("'", "''") + "'"


def collect_mssql_settings(rds_data_client, cache_execute, target_cluster_arn,
                           target_secret_arn, cluster_id, database):
    sql = SETTINGS_SQL.format(
        placeholders=", ".join(_sql_literal(n) for n in _TRACKED))
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {sql}",
    )

    upserted, pending = 0, 0
    for rec in resp.get("records", []):
        name = _str(rec[0])
        configured = _str(rec[1])
        running = _str(rec[2])
        is_dynamic = _long(rec[3])
        if configured == running:
            unit = ""
        elif is_dynamic:
            # The engine adjusted it. A restart will NOT apply `configured`.
            unit = f"(설정값 {configured}, 엔진이 조정한 값)"
            pending += 1
        else:
            unit = f"(설정값 {configured}, 재시작 후 적용)"
            pending += 1
        cache_execute(UPSERT_SETTING_SQL, {
            "cluster_id": cluster_id,
            "name": name,
            # Always the RUNNING value: "what is this server doing right now".
            "value": running,
            "unit": unit,
        })
        upserted += 1

    return {"cluster_id": cluster_id, "settings_upserted": upserted,
            "diverging_from_configured": pending}

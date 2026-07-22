"""Engine-family classification + capability map (canonical pure module).

No shared Lambda layer spans api/ · data-pipeline/ · mcp-servers/, so this file
is duplicated VERBATIM in each package that needs it:
  - api/clusters/engine_family.py
  - api/dashboard/engine_family.py
  - data-pipeline/etl_collector/collectors/engine_family.py
  - mcp-servers/mcp_servers/shared/engine_family.py
Keep all copies in sync. The frontend mirror lives in frontend/src/lib/engine.ts.
"""

import hashlib

RELATIONAL = "relational"
DOCUMENTDB = "documentdb"
DYNAMODB = "dynamodb"
ELASTICACHE = "elasticache"
RDS_INSTANCE = "rds_instance"


def engine_family(engine):
    """Map an `engine` string to a family. Unknown → relational (legacy: every
    existing registry row is Aurora; the SQL path is the safe historical default
    and DynamoDB/DocDB are matched explicitly before the fallback)."""
    e = (engine or "").lower()
    if "docdb" in e or "documentdb" in e:
        return DOCUMENTDB
    if "dynamodb" in e:
        return DYNAMODB
    if "redis" in e or "valkey" in e or "memcached" in e or "elasticache" in e:
        return ELASTICACHE
    # RDS instance engines (non-Aurora). Order matters: 'aurora-mysql' contains
    # 'mysql', so the aurora guard keeps Aurora MySQL relational.
    if "sqlserver" in e:
        return RDS_INSTANCE
    if "mysql" in e and "aurora" not in e:
        return RDS_INSTANCE
    return RELATIONAL


# Per-family capabilities. Foundation runs findings collectors for relational
# only; documentdb/dynamodb collect metrics + meta but emit no findings yet
# (specs #2/#3 add them). `rds_meta`/`perf_insights`/`sql` gate the ETL
# pre-branch RDS calls and the dashboard backend endpoints.
CAPABILITIES = {
    RELATIONAL: {
        "sql": True, "sql_via": "data_api", "rds_meta": True, "perf_insights": True, "simulation": True,
        # custom_endpoint: Aurora custom cluster endpoints (P2-⑤) are relational-
        # only; the operations handler positive-gates the create/delete/modify
        # tools on this key so non-relational engines get unsupported_engine.
        "custom_endpoint": True,
        # prewarm: Aurora reader buffer-cache prewarm (P2-④) is relational-only
        # (pg_prewarm is PG-specific; the tool additionally gates PG vs MySQL).
        # Positive gate like custom_endpoint.
        "prewarm": True,
        # scale_instance: Aurora reader scale-out/scale-in (N-③) is instance-level
        # (both PG and MySQL). Positive gate — non-relational engines can't add/
        # remove an RDS DB instance this way.
        "scale_instance": True,
        "cw_namespace": "AWS/RDS",
        "findings": {"health", "cost", "param_fitness", "capacity_forecast"},
    },
    DOCUMENTDB: {
        "sql": False, "rds_meta": True, "perf_insights": False, "simulation": False,
        "docdb_write": True,
        "cw_namespace": "AWS/DocDB",
        "findings": {"docdb"},
    },
    DYNAMODB: {
        "sql": False, "rds_meta": False, "perf_insights": False, "simulation": False,
        "ddb_cost_simulation": True,
        "ddb_write": True,
        "cw_namespace": "AWS/DynamoDB",
        "findings": {"ddb"},
    },
    ELASTICACHE: {
        "sql": False, "rds_meta": False, "perf_insights": False,
        "simulation": False,
        "elasticache_cost_simulation": True,
        "elasticache_write": True,
        "live_read": True,
        "cw_namespace": "AWS/ElastiCache",
        "findings": {"elasticache"},
    },
    RDS_INSTANCE: {
        # SQL-capable but NOT via RDS Data API (Aurora-only) — R-3 wires the
        # direct-TCP path; until then execute_sql's Data API call must not be
        # reached for this family (sql_via is the dispatch key).
        "sql": True, "sql_via": "direct",
        "rds_meta": True, "perf_insights": True, "simulation": False,
        # Cluster/reader-topology concepts — never applicable to a standalone
        # DB instance.
        "custom_endpoint": False, "prewarm": False, "scale_instance": False,
        # Shared namespace with Aurora but instance-dimensioned
        # (DBInstanceIdentifier; the DBClusterIdentifier dimension does not
        # exist for these engines).
        "cw_namespace": "AWS/RDS",
        # R-2: cache-only MySQL param_fitness runs in the ETL collector (reads
        # the cache DB only, no VPC). InnoDB-status findings come from the
        # VPC direct-TCP collector (rds_direct_collector), not tracked here.
        "findings": {"param_fitness"},
    },
}


def dynamodb_cluster_id(account_id, region, table_name):
    """Regex-safe registry PK for a DynamoDB table. Table names allow `_`/`.`
    and up to 255 chars, which the API validators (`^[a-zA-Z0-9-]{1,63}$`)
    reject — so use a deterministic slug and keep the real name in resource_name."""
    h = hashlib.sha256(f"{account_id}:{region}:{table_name}".encode()).hexdigest()[:12]
    return f"ddb-{h}"

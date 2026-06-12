"""Engine-family classification + capability map (canonical pure module).

No shared Lambda layer spans api/ · data-pipeline/ · mcp-servers/, so this file
is duplicated VERBATIM in each package that needs it:
  - api/clusters/engine_family.py
  - mcp-servers/mcp_servers/shared/engine_family.py
Keep all copies in sync. The frontend mirror lives in frontend/src/lib/engine.ts.
"""

import hashlib

RELATIONAL = "relational"
DOCUMENTDB = "documentdb"
DYNAMODB = "dynamodb"


def engine_family(engine):
    """Map an `engine` string to a family. Unknown → relational (legacy: every
    existing registry row is Aurora; the SQL path is the safe historical default
    and DynamoDB/DocDB are matched explicitly before the fallback)."""
    e = (engine or "").lower()
    if "docdb" in e or "documentdb" in e:
        return DOCUMENTDB
    if "dynamodb" in e:
        return DYNAMODB
    return RELATIONAL


# Per-family capabilities. Foundation runs findings collectors for relational
# only; documentdb/dynamodb collect metrics + meta but emit no findings yet
# (specs #2/#3 add them). `rds_meta`/`perf_insights`/`sql` gate the ETL
# pre-branch RDS calls and the dashboard backend endpoints.
CAPABILITIES = {
    RELATIONAL: {
        "sql": True, "rds_meta": True, "perf_insights": True, "simulation": True,
        "cw_namespace": "AWS/RDS",
        "findings": {"health", "cost", "param_fitness", "capacity_forecast"},
    },
    DOCUMENTDB: {
        "sql": False, "rds_meta": True, "perf_insights": False, "simulation": False,
        "cw_namespace": "AWS/DocDB",
        "findings": {"docdb"},
    },
    DYNAMODB: {
        "sql": False, "rds_meta": False, "perf_insights": False, "simulation": False,
        "ddb_cost_simulation": True,
        "cw_namespace": "AWS/DynamoDB",
        "findings": {"ddb"},
    },
}


def dynamodb_cluster_id(account_id, region, table_name):
    """Regex-safe registry PK for a DynamoDB table. Table names allow `_`/`.`
    and up to 255 chars, which the API validators (`^[a-zA-Z0-9-]{1,63}$`)
    reject — so use a deterministic slug and keep the real name in resource_name."""
    h = hashlib.sha256(f"{account_id}:{region}:{table_name}".encode()).hexdigest()[:12]
    return f"ddb-{h}"

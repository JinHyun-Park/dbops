import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.operations.tools.audit_permissions import audit_permissions_impl
from mcp_servers.operations.tools.create_docdb_index import create_docdb_index_impl
from mcp_servers.operations.tools.create_snapshot import create_snapshot_impl
from mcp_servers.operations.tools.elasticache_live_read import elasticache_live_read_impl
from mcp_servers.operations.tools.enable_dynamodb_pitr import enable_dynamodb_pitr_impl
from mcp_servers.operations.tools.execute_sql import execute_sql_impl
from mcp_servers.operations.tools.get_runbook import get_runbook_impl
from mcp_servers.operations.tools.manage_maintenance import manage_maintenance_impl
from mcp_servers.operations.tools.modify_dynamodb_capacity import modify_dynamodb_capacity_impl
from mcp_servers.operations.tools.modify_dynamodb_ttl import modify_dynamodb_ttl_impl
from mcp_servers.operations.tools.modify_parameter import modify_parameter_impl
from mcp_servers.operations.tools.modify_scaling import modify_scaling_impl
from mcp_servers.operations.tools.query_activity_audit import query_activity_audit_impl
from mcp_servers.operations.tools.request_approval import request_approval_impl
from mcp_servers.operations.tools.restore_cluster import restore_cluster_impl
from mcp_servers.operations.tools.review_sql import review_sql_impl
from mcp_servers.operations.tools.schema_diff import get_schema_diff_impl
from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.operations.tools.set_docdb_profiler import set_docdb_profiler_impl
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import CAPABILITIES
from mcp_servers.shared.engine_family import engine_family as _engine_family

cache = CacheClient()

# NoSQL write tools → the per-family CAPABILITIES key they REQUIRE. Only these
# tools are engine-gated; the Aurora tools (execute_sql etc.) stay ungated.
# FAIL-CLOSED for writes: a None family (missing row / lookup error / empty
# cluster_id) resolves to .get(None,{}).get(key,False) == False → refused, so an
# unknown/unregistered/lookup-failed cluster can NEVER slip a write through even
# with a valid-looking approval (review fix #3 — opposite of simulation's read-side
# DEFAULT-PERMIT).
_ENGINE_GATED_TOOLS = {
    "modify_dynamodb_capacity": "ddb_write",
    "modify_dynamodb_ttl": "ddb_write",
    "enable_dynamodb_pitr": "ddb_write",
    # DocumentDB Mongo-protocol write tools (stage 2). FAIL-CLOSED on None family
    # too: a documentdb cluster missing the docdb_write capability — or any
    # unresolvable cluster — refuses before reaching the impl (review fix #3).
    "set_docdb_profiler": "docdb_write",
    "create_docdb_index": "docdb_write",
    "elasticache_live_read": "live_read",
}

TOOLS = {
    "get_schema_diff": {
        "impl": get_schema_diff_impl,
        "description": "Compare schemas between two time points or show latest diff",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "snapshot_a": {"type": "string", "description": "ISO 8601 timestamp of first snapshot"},
                "snapshot_b": {"type": "string", "description": "ISO 8601 timestamp of second snapshot"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_schema_history": {
        "impl": get_schema_history_impl,
        "description": "Track schema change history over a period",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "days": {"type": "integer", "default": 30, "description": "Look-back window in days"},
            },
            "required": ["cluster_id"],
        },
    },
    "execute_sql": {
        "impl": execute_sql_impl,
        "description": (
            "Execute SQL against a cluster. SELECT/EXPLAIN/SHOW/DESCRIBE run "
            "directly. Write operations require approval — set approved=true "
            "AND approval_id=<uuid from request_approval>. Dangerous SQL "
            "(DROP/TRUNCATE/DELETE) requires force=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sql": {"type": "string", "description": "SQL statement to execute"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval — server verifies this against DDB before executing"},
                "force": {"type": "boolean", "default": False, "description": "Force execution of dangerous SQL"},
            },
            "required": ["cluster_id", "sql"],
        },
    },
    "modify_parameter": {
        "impl": modify_parameter_impl,
        "description": (
            "Modify a DB cluster parameter. Requires approved=true AND "
            "approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "parameter_name": {"type": "string", "description": "Parameter name to modify"},
                "value": {"type": "string", "description": "New parameter value"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "parameter_name", "value"],
        },
    },
    "modify_scaling": {
        "impl": modify_scaling_impl,
        "description": (
            "Modify Serverless v2 scaling configuration. Requires approved=true "
            "AND approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "min_capacity": {"type": "number", "description": "Minimum ACU capacity"},
                "max_capacity": {"type": "number", "description": "Maximum ACU capacity"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "manage_maintenance": {
        "impl": manage_maintenance_impl,
        "description": (
            "Describe or modify maintenance windows. Modify action requires "
            "approved=true AND approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "action": {"type": "string", "enum": ["describe", "modify"], "default": "describe", "description": "Action to perform"},
                "window": {"type": "string", "description": "New maintenance window (for modify action)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "create_snapshot": {
        "impl": create_snapshot_impl,
        "description": (
            "Create a manual cluster snapshot (backup). Non-destructive but "
            "requires approved=true AND approval_id=<uuid from request_approval>. "
            "snapshot_id is optional — auto-generated if omitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "snapshot_id": {"type": "string", "description": "Optional snapshot identifier (auto-generated if omitted)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "restore_cluster": {
        "impl": restore_cluster_impl,
        "description": (
            "Restore a cluster into a BRAND-NEW cluster from a snapshot or a "
            "point in time. HIGH RISK — it stands up a new, billable Aurora "
            "cluster. The source cluster is NEVER modified. Requires "
            "approved=true AND approval_id=<uuid from request_approval>. "
            "mode='snapshot' needs snapshot_id; mode='pitr' needs "
            "restore_to_time (ISO 8601 within the PITR window) or "
            "use_latest=true. new_cluster_id MUST differ from cluster_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Source Aurora cluster ID (read-only — never modified)"},
                "new_cluster_id": {"type": "string", "description": "Identifier for the NEW restored cluster (must differ from source)"},
                "mode": {"type": "string", "enum": ["snapshot", "pitr"], "default": "snapshot", "description": "Restore source type"},
                "snapshot_id": {"type": "string", "description": "Snapshot to restore from (mode=snapshot)"},
                "restore_to_time": {"type": "string", "description": "ISO 8601 timestamp within the PITR window (mode=pitr)"},
                "use_latest": {"type": "boolean", "default": False, "description": "Restore to the latest restorable time (mode=pitr)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "new_cluster_id"],
        },
    },
    "modify_dynamodb_capacity": {
        "impl": modify_dynamodb_capacity_impl,
        "description": (
            "DynamoDB only: change provisioned RCU/WCU and/or switch billing "
            "mode (Provisioned<->On-Demand) via update_table. Requires "
            "approved=true AND approval_id=<uuid from request_approval>. Blocks "
            "tables that have any GSI (per-GSI capacity unsupported in v1). "
            "Reject RCU/WCU < 1."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target DynamoDB table ID (ddb-* registry slug)"},
                "billing_mode": {"type": "string", "description": "PROVISIONED or On-Demand — omit to keep current mode"},
                "rcu": {"type": "integer", "description": "Provisioned read capacity units (>=1; required for Provisioned)"},
                "wcu": {"type": "integer", "description": "Provisioned write capacity units (>=1; required for Provisioned)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "modify_dynamodb_ttl": {
        "impl": modify_dynamodb_ttl_impl,
        "description": (
            "DynamoDB only: enable or disable an attribute TTL via "
            "update_time_to_live. Requires approved=true AND "
            "approval_id=<uuid from request_approval>. Idempotent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target DynamoDB table ID (ddb-* registry slug)"},
                "attribute": {"type": "string", "description": "TTL attribute name (expiry epoch-seconds attribute)"},
                "enabled": {"type": "boolean", "default": True, "description": "True to enable TTL, false to disable"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "attribute"],
        },
    },
    "enable_dynamodb_pitr": {
        "impl": enable_dynamodb_pitr_impl,
        "description": (
            "DynamoDB only: turn Point-in-Time Recovery (PITR) on or off via "
            "update_continuous_backups. Requires approved=true AND "
            "approval_id=<uuid from request_approval>. DISABLING PITR is a "
            "data-protection degradation and additionally requires force=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target DynamoDB table ID (ddb-* registry slug)"},
                "enabled": {"type": "boolean", "default": True, "description": "True to enable PITR, false to disable (force required)"},
                "force": {"type": "boolean", "default": False, "description": "Required to DISABLE PITR (enabled=false)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "set_docdb_profiler": {
        "impl": set_docdb_profiler_impl,
        "description": (
            "DocumentDB only: set the database profiler level via the Mongo "
            "protocol (db.command profile). level 0=off, 1=slow ops (slowms "
            "threshold), 2=all ops. Requires approved=true AND "
            "approval_id=<uuid from request_approval>. Idempotent. Needs a "
            "configured write credential (mongo_write_secret_arn)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target DocumentDB cluster ID"},
                "db": {"type": "string", "default": "admin", "description": "Target database name (defaults to admin)"},
                "level": {"type": "integer", "default": 1, "description": "Profiler level: 0=off, 1=slow ops, 2=all ops"},
                "slowms": {"type": "integer", "default": 100, "description": "slowms threshold in milliseconds (>=0)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "create_docdb_index": {
        "impl": create_docdb_index_impl,
        "description": (
            "DocumentDB only: create an index on a collection via the Mongo "
            "protocol (create_index, background=true). keys is an ORDERED list "
            "of [field, direction] pairs (direction 1=asc, -1=desc) — compound "
            "order is significant. name is required. Requires approved=true AND "
            "approval_id=<uuid from request_approval>. Idempotent (skips if the "
            "named index exists). Needs a configured write credential "
            "(mongo_write_secret_arn)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target DocumentDB cluster ID"},
                "db": {"type": "string", "description": "Target database name"},
                "collection": {"type": "string", "description": "Target collection name"},
                "keys": {"type": "array", "description": "Ordered list of [field, direction] pairs, e.g. [[\"user_id\", 1], [\"created_at\", -1]]"},
                "name": {"type": "string", "description": "Index name (required)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "db", "collection", "keys", "name"],
        },
    },
    "elasticache_live_read": {
        "impl": elasticache_live_read_impl,
        "description": "ElastiCache only: live Redis/Valkey/Memcached deep-read — "
                       "INFO, SLOWLOG, CLIENT LIST, MEMORY STATS (Redis) or stats "
                       "(Memcached). Read-only; no mutation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Registered ElastiCache cluster id"},
                "sections": {"type": "array", "items": {"type": "string"},
                             "description": "Optional subset of Redis INFO sections "
                                            "(server/clients/memory/stats/replication/keyspace)"},
            },
            "required": ["cluster_id"],
        },
    },
    "review_sql": {
        "impl": review_sql_impl,
        "description": "Pre-execution SQL review with risk classification, issue detection, and rollback suggestion",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sql": {"type": "string", "description": "SQL statement to review"},
            },
            "required": ["cluster_id", "sql"],
        },
    },
    "audit_permissions": {
        "impl": audit_permissions_impl,
        "description": "Audit database user permissions and detect superuser accounts",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "engine": {"type": "string", "enum": ["postgresql", "mysql"], "default": "postgresql", "description": "Database engine type"},
            },
            "required": ["cluster_id"],
        },
    },
    "query_activity_audit": {
        "impl": query_activity_audit_impl,
        "description": (
            "Search executed write operations + approval history across "
            "the audit_log PG table and the DDB approvals table. Use this "
            "to answer compliance / retro questions like 'who changed "
            "max_connections in prod-pg-1 last week?' or 'show me every "
            "parameter modification approved by Alice this month'. "
            "Returns merged + chronological list. Read-only — does NOT "
            "execute anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {
                    "type": "string",
                    "description": (
                        "Filter to one cluster (empty = all clusters caller "
                        "can see)"
                    ),
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "Match requested_by OR approved_by (Cognito "
                        "username/email)"
                    ),
                },
                "action_type": {
                    "type": "string",
                    "description": (
                        "Filter by action type: execute_sql, "
                        "modify_parameter, modify_scaling, "
                        "manage_maintenance, other"
                    ),
                },
                "days": {
                    "type": "integer",
                    "default": 7,
                    "description": "Look-back window in days (1..90)",
                },
            },
        },
    },
    "get_runbook": {
        "impl": get_runbook_impl,
        "description": (
            "Fetch a saved runbook (markdown playbook from /runbooks) by id "
            "OR a fuzzy title/tag query, and get back its content plus the "
            "fenced ```sql blocks extracted as ordered `steps`. Use this to "
            "EXECUTE a runbook: present the steps to the DBA, then run each "
            "step's SQL via the execute_sql tool. execute_sql is "
            "approval-gated — writes require request_approval then "
            "execute_sql with approved=true AND approval_id. NEVER bypass "
            "approval. This tool is read-only and executes nothing itself. "
            "When the query is ambiguous it returns a `candidates` list — "
            "confirm the intended runbook with the DBA before proceeding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "runbook_id": {
                    "type": "string",
                    "description": "Exact runbook id (preferred when known)",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Fuzzy title/tag search when the id is unknown "
                        "(matches title ILIKE or any tag ILIKE)"
                    ),
                },
            },
        },
    },
    "request_approval": {
        "impl": request_approval_impl,
        "description": (
            "Register a DBA approval request for a write action. Call this "
            "immediately after a write tool returns status=approval_required. "
            "Returns approval_id + review URL. After the DBA approves on the "
            "/approvals page, re-issue the original write tool with BOTH "
            "approved=true AND approval_id=<returned uuid> — the server "
            "verifies the id against DDB and refuses replays."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "action_type": {
                    "type": "string",
                    "enum": ["execute_sql", "modify_parameter", "modify_scaling", "manage_maintenance", "create_snapshot", "restore_cluster", "modify_dynamodb_capacity", "modify_dynamodb_ttl", "enable_dynamodb_pitr", "set_docdb_profiler", "create_docdb_index", "other"],
                    "description": "Which write tool needs approval",
                },
                "action_details": {
                    "type": "object",
                    "description": "The exact arguments the write tool would have been called with — DBA reviews this verbatim",
                },
                "requested_by": {"type": "string", "default": "agent"},
            },
            "required": ["cluster_id", "action_type", "action_details"],
        },
    },
}


def _extract_tool_name(context):
    cc = getattr(context, "client_context", None)
    if not cc:
        return None
    custom = getattr(cc, "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName") or custom.get("tool_name")
    if not raw:
        return None
    return raw.split("___", 1)[1] if "___" in raw else raw


def _resolve_family(cluster_id):
    """Resolve the engine family from cluster_meta via the cache. Returns None
    when cluster_id is empty, the row is missing, or the lookup errors. For the
    engine-gated WRITE tools a None family is FAIL-CLOSED (refused) — opposite of
    simulation's read-side default-permit (review fix #3)."""
    if not cluster_id:
        return None
    try:
        rows = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception as e:
        print(f"[operations] family lookup failed for {cluster_id}: {e}")
        return None
    rows = getattr(rows, "rows", rows)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return _engine_family(rows[0].get("engine"))
    return None


def lambda_handler(event, context):
    tool_name = _extract_tool_name(context)
    method = event.get("method") if isinstance(event, dict) else None

    if method == "tools/list":
        return {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
            for n, t in TOOLS.items()
        ]}

    if tool_name and tool_name in TOOLS:
        # POSITIVE engine-capability gate, FAIL-CLOSED for the NoSQL write tools
        # only (the Aurora tools stay ungated). A NoSQL tool called on an Aurora
        # cluster — or on an unresolvable/unregistered cluster — refuses, so a
        # valid-looking approval can never drive a write at the wrong engine.
        cap_key = _ENGINE_GATED_TOOLS.get(tool_name)
        if cap_key:
            cluster_id = (event or {}).get("cluster_id") if isinstance(event, dict) else None
            fam = _resolve_family(cluster_id)
            if not CAPABILITIES.get(fam, {}).get(cap_key, False):
                engine_label = "DocumentDB 클러스터" if cap_key == "docdb_write" else "DynamoDB 테이블"
                return {"content": [{"type": "text", "text": json.dumps({
                    "status": "unsupported_engine",
                    "engine_family": fam,
                    "cluster_id": cluster_id,
                    "reason": (
                        "cluster engine could not be resolved"
                        if fam is None
                        else f"{tool_name}는 {engine_label} 전용입니다 (현재 엔진: {fam})."
                    ),
                })}]}
        try:
            result = TOOLS[tool_name]["impl"](cache, **(event or {}))
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]}

    return {"error": f"Unknown tool: {tool_name}"}

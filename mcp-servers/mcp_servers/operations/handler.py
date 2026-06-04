import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.operations.tools.audit_permissions import audit_permissions_impl
from mcp_servers.operations.tools.create_snapshot import create_snapshot_impl
from mcp_servers.operations.tools.execute_sql import execute_sql_impl
from mcp_servers.operations.tools.manage_maintenance import manage_maintenance_impl
from mcp_servers.operations.tools.modify_parameter import modify_parameter_impl
from mcp_servers.operations.tools.modify_scaling import modify_scaling_impl
from mcp_servers.operations.tools.query_activity_audit import query_activity_audit_impl
from mcp_servers.operations.tools.request_approval import request_approval_impl
from mcp_servers.operations.tools.review_sql import review_sql_impl
from mcp_servers.operations.tools.schema_diff import get_schema_diff_impl
from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.shared.cache_client import CacheClient

cache = CacheClient()

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
                    "enum": ["execute_sql", "modify_parameter", "modify_scaling", "manage_maintenance", "create_snapshot", "other"],
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


def lambda_handler(event, context):
    tool_name = _extract_tool_name(context)
    method = event.get("method") if isinstance(event, dict) else None

    if method == "tools/list":
        return {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
            for n, t in TOOLS.items()
        ]}

    if tool_name and tool_name in TOOLS:
        try:
            result = TOOLS[tool_name]["impl"](cache, **(event or {}))
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]}

    return {"error": f"Unknown tool: {tool_name}"}

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.operations.tools.schema_diff import get_schema_diff_impl
from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.operations.tools.execute_sql import execute_sql_impl
from mcp_servers.operations.tools.modify_parameter import modify_parameter_impl
from mcp_servers.operations.tools.modify_scaling import modify_scaling_impl
from mcp_servers.operations.tools.manage_maintenance import manage_maintenance_impl
from mcp_servers.operations.tools.review_sql import review_sql_impl
from mcp_servers.operations.tools.audit_permissions import audit_permissions_impl

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
        "description": "Execute SQL against a cluster. Write operations require approval (approved=true). Dangerous SQL (DROP/TRUNCATE/DELETE) requires force=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sql": {"type": "string", "description": "SQL statement to execute"},
                "approved": {"type": "boolean", "default": False, "description": "DBA approval for write operations"},
                "force": {"type": "boolean", "default": False, "description": "Force execution of dangerous SQL"},
            },
            "required": ["cluster_id", "sql"],
        },
    },
    "modify_parameter": {
        "impl": modify_parameter_impl,
        "description": "Modify a DB cluster parameter. Requires approval (approved=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "parameter_name": {"type": "string", "description": "Parameter name to modify"},
                "value": {"type": "string", "description": "New parameter value"},
                "approved": {"type": "boolean", "default": False, "description": "DBA approval required"},
            },
            "required": ["cluster_id", "parameter_name", "value"],
        },
    },
    "modify_scaling": {
        "impl": modify_scaling_impl,
        "description": "Modify Serverless v2 scaling configuration. Requires approval (approved=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "min_capacity": {"type": "number", "description": "Minimum ACU capacity"},
                "max_capacity": {"type": "number", "description": "Maximum ACU capacity"},
                "approved": {"type": "boolean", "default": False, "description": "DBA approval required"},
            },
            "required": ["cluster_id"],
        },
    },
    "manage_maintenance": {
        "impl": manage_maintenance_impl,
        "description": "Describe or modify maintenance windows. Modify action requires approval (approved=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "action": {"type": "string", "enum": ["describe", "modify"], "default": "describe", "description": "Action to perform"},
                "window": {"type": "string", "description": "New maintenance window (for modify action)"},
                "approved": {"type": "boolean", "default": False, "description": "DBA approval required for modify"},
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
}


def lambda_handler(event, context):
    method = event.get("method")

    if method == "tools/list":
        tools_list = []
        for name, tool in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["input_schema"],
            })
        return {"tools": tools_list}

    if method == "tools/call":
        tool_name = event.get("params", {}).get("name")
        arguments = event.get("params", {}).get("arguments", {})

        if tool_name not in TOOLS:
            return {"error": f"Unknown tool: {tool_name}"}

        impl = TOOLS[tool_name]["impl"]
        result = impl(cache, **arguments)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    return {"error": f"Unknown method: {method}"}

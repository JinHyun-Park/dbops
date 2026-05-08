import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.simulation.tools.upgrade_compatibility import check_upgrade_compatibility_impl
from mcp_servers.simulation.tools.upgrade_impact import estimate_upgrade_impact_impl
from mcp_servers.simulation.tools.upgrade_plan import generate_upgrade_plan_impl
from mcp_servers.simulation.tools.parameter_simulation import simulate_parameter_change_impl
from mcp_servers.simulation.tools.scaling_simulation import simulate_scaling_impl
from mcp_servers.simulation.tools.ddl_impact import simulate_ddl_impact_impl

cache = CacheClient()

TOOLS = {
    "check_upgrade_compatibility": {
        "impl": check_upgrade_compatibility_impl,
        "description": "Check if a target engine version is a valid upgrade path for the cluster",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "target_version": {"type": "string", "description": "Target engine version to upgrade to"},
            },
            "required": ["cluster_id", "target_version"],
        },
    },
    "estimate_upgrade_impact": {
        "impl": estimate_upgrade_impact_impl,
        "description": "Estimate time, downtime, and risk for each upgrade method (in-place, blue/green, clone)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "target_version": {"type": "string", "description": "Target engine version"},
            },
            "required": ["cluster_id", "target_version"],
        },
    },
    "generate_upgrade_plan": {
        "impl": generate_upgrade_plan_impl,
        "description": "Generate a step-by-step upgrade plan with rollback strategy",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "target_version": {"type": "string", "description": "Target engine version"},
                "method": {"type": "string", "enum": ["blue_green", "in_place", "clone"], "default": "blue_green"},
            },
            "required": ["cluster_id", "target_version"],
        },
    },
    "simulate_parameter_change": {
        "impl": simulate_parameter_change_impl,
        "description": "Simulate the impact of changing a database parameter (restart required, dynamic/static)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "parameter_name": {"type": "string", "description": "Parameter name to change"},
                "new_value": {"type": "string", "description": "New parameter value"},
            },
            "required": ["cluster_id", "parameter_name", "new_value"],
        },
    },
    "simulate_scaling": {
        "impl": simulate_scaling_impl,
        "description": "Simulate ACU scaling and estimate cost impact",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "new_min_acu": {"type": "number", "description": "New minimum ACU"},
                "new_max_acu": {"type": "number", "description": "New maximum ACU"},
            },
            "required": ["cluster_id"],
        },
    },
    "simulate_ddl_impact": {
        "impl": simulate_ddl_impact_impl,
        "description": "Simulate the impact of a DDL statement (lock type, estimated time, online DDL possibility)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "ddl_sql": {"type": "string", "description": "DDL SQL statement to simulate"},
            },
            "required": ["cluster_id", "ddl_sql"],
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

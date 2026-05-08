import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.incident.tools.health_status import get_health_status_impl
from mcp_servers.incident.tools.recent_events import get_recent_events_impl
from mcp_servers.incident.tools.search_logs import search_logs_impl
from mcp_servers.incident.tools.correlate_signals import correlate_signals_impl
from mcp_servers.incident.tools.incident_summary import get_incident_summary_impl
from mcp_servers.incident.tools.similar_incidents import find_similar_incidents_impl

cache = CacheClient()

TOOLS = {
    "get_health_status": {
        "impl": get_health_status_impl,
        "description": "Get cluster health overview from cached metadata and recent metrics",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_recent_events": {
        "impl": get_recent_events_impl,
        "description": "Get recent events from event_log for a cluster",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "hours": {"type": "integer", "default": 24, "description": "Look-back window in hours"},
                "event_type": {"type": "string", "description": "Filter by event type"},
            },
            "required": ["cluster_id"],
        },
    },
    "search_logs": {
        "impl": search_logs_impl,
        "description": "Search CloudWatch Logs Insights for cluster error logs",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "query": {"type": "string", "description": "CloudWatch Logs Insights query"},
                "hours": {"type": "integer", "default": 6, "description": "Look-back window in hours"},
                "log_group": {"type": "string", "description": "Override log group name"},
            },
            "required": ["cluster_id"],
        },
    },
    "correlate_signals": {
        "impl": correlate_signals_impl,
        "description": "Correlate metrics and events on a unified timeline for a time window",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "start_time": {"type": "string", "description": "ISO 8601 start time"},
                "end_time": {"type": "string", "description": "ISO 8601 end time"},
            },
            "required": ["cluster_id", "start_time", "end_time"],
        },
    },
    "get_incident_summary": {
        "impl": get_incident_summary_impl,
        "description": "Aggregate incident events by type and severity over a period",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "days": {"type": "integer", "default": 30, "description": "Look-back window in days"},
            },
            "required": ["cluster_id"],
        },
    },
    "find_similar_incidents": {
        "impl": find_similar_incidents_impl,
        "description": "Search Bedrock Knowledge Base for similar past incidents",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "symptoms": {"type": "string", "description": "Description of current symptoms"},
            },
            "required": ["cluster_id", "symptoms"],
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

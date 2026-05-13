import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.incident.tools.correlate_signals import correlate_signals_impl
from mcp_servers.incident.tools.health_status import get_health_status_impl
from mcp_servers.incident.tools.incident_summary import get_incident_summary_impl
from mcp_servers.incident.tools.recent_events import get_recent_events_impl
from mcp_servers.incident.tools.search_logs import search_logs_impl
from mcp_servers.incident.tools.similar_incidents import find_similar_incidents_impl
from mcp_servers.shared.cache_client import CacheClient

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

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.performance.tools.top_queries import get_top_queries_impl
from mcp_servers.performance.tools.pi_metrics import get_pi_metrics_impl
from mcp_servers.performance.tools.slow_queries import get_slow_queries_impl
from mcp_servers.performance.tools.compare_periods import compare_periods_impl

cache = CacheClient()

TOOLS = {
    "get_top_queries": {
        "impl": get_top_queries_impl,
        "description": "Get top-N queries from Aurora PG Cache sorted by total time, calls, or mean time",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sort_by": {"type": "string", "enum": ["total_time", "calls", "mean_time", "rows"], "default": "total_time"},
                "limit": {"type": "integer", "default": 10},
                "start_time": {"type": "string", "description": "ISO 8601 start time"},
                "end_time": {"type": "string", "description": "ISO 8601 end time"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_pi_metrics": {
        "impl": get_pi_metrics_impl,
        "description": "Get Performance Insights metrics (AAS, wait events, counter metrics) from cache",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "metric_type": {"type": "string", "enum": ["aas", "cpu", "connections", "iops", "wait_events"], "default": "aas"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_slow_queries": {
        "impl": get_slow_queries_impl,
        "description": "Get slow queries exceeding threshold from cache",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "threshold_ms": {"type": "number", "default": 1000.0},
                "limit": {"type": "integer", "default": 20},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "compare_periods": {
        "impl": compare_periods_impl,
        "description": "Compare metrics between two time periods for trend analysis",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "period_a_start": {"type": "string"},
                "period_a_end": {"type": "string"},
                "period_b_start": {"type": "string"},
                "period_b_end": {"type": "string"},
                "metric_type": {"type": "string", "default": "aas"},
            },
            "required": ["cluster_id", "period_a_start", "period_a_end", "period_b_start", "period_b_end"],
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

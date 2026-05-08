import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.performance.tools.top_queries import get_top_queries_impl
from mcp_servers.performance.tools.pi_metrics import get_pi_metrics_impl
from mcp_servers.performance.tools.slow_queries import get_slow_queries_impl
from mcp_servers.performance.tools.compare_periods import compare_periods_impl
from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
from mcp_servers.performance.tools.detect_regressions import detect_regressions_impl
from mcp_servers.performance.tools.forecast_capacity import forecast_capacity_impl
from mcp_servers.performance.tools.performance_summary import get_performance_summary_impl
from mcp_servers.performance.tools.recommend_index import recommend_index_impl
from mcp_servers.performance.tools.vacuum_stats import get_vacuum_stats_impl

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
    "detect_anomalies": {
        "impl": detect_anomalies_impl,
        "description": "Detect anomalous metrics using z-score analysis against a 7-day baseline",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "hours": {"type": "integer", "default": 4, "description": "Recent window in hours to compare against baseline"},
                "threshold": {"type": "number", "default": 2.0, "description": "Z-score threshold for anomaly detection"},
            },
            "required": ["cluster_id"],
        },
    },
    "detect_regressions": {
        "impl": detect_regressions_impl,
        "description": "Detect query performance regressions around a change point (deploy, config change)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "change_point": {"type": "string", "description": "ISO 8601 timestamp of the change event"},
                "hours_before": {"type": "integer", "default": 24, "description": "Hours before change point for baseline"},
                "hours_after": {"type": "integer", "default": 24, "description": "Hours after change point to analyze"},
                "min_change_pct": {"type": "number", "default": 50.0, "description": "Minimum % increase to flag as regression"},
            },
            "required": ["cluster_id", "change_point"],
        },
    },
    "forecast_capacity": {
        "impl": forecast_capacity_impl,
        "description": "Forecast when a metric (storage, connections, AAS) will reach its limit using linear regression",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "metric": {"type": "string", "enum": ["storage_gb", "connections", "aas"], "default": "storage_gb"},
                "days_lookback": {"type": "integer", "default": 30, "description": "Days of historical data for trend calculation"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_performance_summary": {
        "impl": get_performance_summary_impl,
        "description": "Get a high-level performance summary with key KPIs (AAS, slow queries, connections)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "hours": {"type": "integer", "default": 24, "description": "Time window in hours"},
            },
            "required": ["cluster_id"],
        },
    },
    "recommend_index": {
        "impl": recommend_index_impl,
        "description": "Recommend indexes based on sequential scan ratio and query patterns",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "min_seq_scan_ratio": {"type": "number", "default": 0.5},
            },
            "required": ["cluster_id"],
        },
    },
    "get_vacuum_stats": {
        "impl": get_vacuum_stats_impl,
        "description": "(PostgreSQL) Get autovacuum stats, dead tuples, and bloat ratio per table",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
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

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import CAPABILITIES
from mcp_servers.shared.engine_family import engine_family as _engine_family
from mcp_servers.simulation.tools.capacity_cost import simulate_dynamodb_capacity_cost_impl
from mcp_servers.simulation.tools.ddl_impact import simulate_ddl_impact_impl
from mcp_servers.simulation.tools.parameter_simulation import simulate_parameter_change_impl
from mcp_servers.simulation.tools.scaling_simulation import simulate_scaling_impl
from mcp_servers.simulation.tools.upgrade_compatibility import check_upgrade_compatibility_impl
from mcp_servers.simulation.tools.upgrade_impact import estimate_upgrade_impact_impl
from mcp_servers.simulation.tools.upgrade_plan import generate_upgrade_plan_impl

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
        "description": "Simulate scaling cost with real AWS pricing — Serverless v2 ACU range or provisioned instance resize",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "new_min_acu": {"type": "number", "description": "New minimum ACU (Serverless v2 only)"},
                "new_max_acu": {"type": "number", "description": "New maximum ACU (Serverless v2 only)"},
                "new_instance_class": {"type": "string", "description": "New instance class for provisioned clusters, e.g. db.r6g.xlarge"},
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
    "simulate_dynamodb_capacity_cost": {
        "impl": simulate_dynamodb_capacity_cost_impl,
        "description": "DynamoDB only: compare Provisioned vs On-Demand monthly cost from the table's actual consumed capacity, priced with the real AWS Price List API",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target DynamoDB table ID (ddb-* registry slug)"},
                "headroom": {"type": "number", "description": "Target utilization for provisioned sizing (0-1, default 0.70)"},
                "window_hours": {"type": "number", "description": "Consumed-capacity lookback window in hours (default/cap 168)"},
            },
            "required": ["cluster_id"],
        },
    },
}


def _resolve_family(cluster_id):
    """Resolve the engine family from cluster_meta via the cache. Returns None
    (→ DEFAULT-PERMIT) when cluster_id is empty, the row is missing, or the
    lookup errors — mirroring the execute_sql guard so legacy/mock/transient
    paths never false-positive."""
    if not cluster_id:
        return None
    try:
        rows = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception as e:
        print(f"[simulation] family lookup failed for {cluster_id}: {e}")
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return _engine_family(rows[0].get("engine"))
    return None


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
        cluster_id = (event or {}).get("cluster_id") if isinstance(event, dict) else None
        fam = _resolve_family(cluster_id)
        if tool_name == "simulate_dynamodb_capacity_cost":
            # POSITIVE gate: this is the one simulation tool that is DynamoDB-only.
            # It uses its own `ddb_cost_simulation` capability (Aurora/DocDB lack it)
            # rather than the generic `simulation` key (which dynamodb leaves False
            # so the Aurora tools still cleanly refuse it). A None family (missing/
            # error) → .get(None,{}) → False → refused, which is correct: only a
            # resolved dynamodb table should pass.
            if not CAPABILITIES.get(fam, {}).get("ddb_cost_simulation", False):
                return {"content": [{"type": "text", "text": json.dumps({
                    "status": "unsupported_engine",
                    "engine_family": fam,
                    "cluster_id": cluster_id,
                    "message": (
                        "용량/비용 비교(Provisioned↔On-Demand)는 DynamoDB 테이블 전용입니다."
                    ),
                })}]}
        else:
            # Engine-family guard: the OTHER simulation tools (upgrade/parameter/
            # DDL/scaling) are Aurora-specific — version-upgrade, SQL DDL, DB
            # parameter groups, and ACU/instance resize have no NoSQL equivalent.
            # Refuse cleanly for documentdb/dynamodb. DEFAULT-PERMIT on unknown/
            # missing/error (mirror execute_sql).
            if isinstance(fam, str) and not CAPABILITIES.get(fam, {}).get("simulation", True):
                return {"content": [{"type": "text", "text": json.dumps({
                    "status": "unsupported_engine",
                    "engine_family": fam,
                    "cluster_id": cluster_id,
                    "message": (
                        "시뮬레이션(업그레이드·파라미터·DDL·스케일링)은 Aurora(PostgreSQL/MySQL) "
                        "전용입니다. DynamoDB 용량/비용이나 DocumentDB 스케일링은 "
                        "get_maintenance_findings로 진단하고, 변경은 AWS Console/CDK로 적용하세요."
                    ),
                })}]}
        try:
            result = TOOLS[tool_name]["impl"](cache, **(event or {}))
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]}

    return {"error": f"Unknown tool: {tool_name}"}

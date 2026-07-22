import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.operations.tools.add_reader_instance import add_reader_instance_impl
from mcp_servers.operations.tools.audit_permissions import audit_permissions_impl
from mcp_servers.operations.tools.create_custom_endpoint import create_custom_endpoint_impl
from mcp_servers.operations.tools.create_docdb_index import create_docdb_index_impl
from mcp_servers.operations.tools.create_elasticache_snapshot import create_elasticache_snapshot_impl
from mcp_servers.operations.tools.create_rds_snapshot import create_rds_snapshot_impl
from mcp_servers.operations.tools.create_snapshot import create_snapshot_impl
from mcp_servers.operations.tools.delete_custom_endpoint import delete_custom_endpoint_impl
from mcp_servers.operations.tools.elasticache_live_read import elasticache_live_read_impl
from mcp_servers.operations.tools.enable_dynamodb_pitr import enable_dynamodb_pitr_impl
from mcp_servers.operations.tools.execute_sql import execute_sql_impl
from mcp_servers.operations.tools.get_runbook import get_runbook_impl
from mcp_servers.operations.tools.manage_maintenance import manage_maintenance_impl
from mcp_servers.operations.tools.modify_custom_endpoint import modify_custom_endpoint_impl
from mcp_servers.operations.tools.modify_dynamodb_capacity import modify_dynamodb_capacity_impl
from mcp_servers.operations.tools.modify_dynamodb_ttl import modify_dynamodb_ttl_impl
from mcp_servers.operations.tools.modify_elasticache_node_type import modify_elasticache_node_type_impl
from mcp_servers.operations.tools.modify_parameter import modify_parameter_impl
from mcp_servers.operations.tools.modify_rds_instance_class import modify_rds_instance_class_impl
from mcp_servers.operations.tools.modify_scaling import modify_scaling_impl
from mcp_servers.operations.tools.plan_az_scaleout import plan_az_scaleout_impl
from mcp_servers.operations.tools.prewarm_reader import prewarm_reader_impl
from mcp_servers.operations.tools.query_activity_audit import query_activity_audit_impl
from mcp_servers.operations.tools.reboot_elasticache import reboot_elasticache_impl
from mcp_servers.operations.tools.reboot_rds_instance import reboot_rds_instance_impl
from mcp_servers.operations.tools.remove_reader_instance import remove_reader_instance_impl
from mcp_servers.operations.tools.request_approval import request_approval_impl
from mcp_servers.operations.tools.restore_cluster import restore_cluster_impl
from mcp_servers.operations.tools.review_sql import review_sql_impl
from mcp_servers.operations.tools.scale_out_with_warmup import scale_out_with_warmup_impl
from mcp_servers.operations.tools.schema_diff import get_schema_diff_impl
from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.operations.tools.set_docdb_profiler import set_docdb_profiler_impl
from mcp_servers.operations.tools.test_elasticache_failover import test_elasticache_failover_impl
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
    # Aurora custom cluster endpoints (P2-⑤) are a relational-only feature. The
    # gate is POSITIVE and FAIL-CLOSED just like the NoSQL writes: only the
    # relational family has the custom_endpoint capability, so DynamoDB/DocDB/
    # ElastiCache (or any unresolvable cluster) get unsupported_engine before the
    # impl runs — no ugly RDS fault on a ddb-* slug.
    "create_custom_endpoint": "custom_endpoint",
    "delete_custom_endpoint": "custom_endpoint",
    "modify_custom_endpoint": "custom_endpoint",
    # Reader buffer-cache prewarm (P2-④) is relational-only (pg_prewarm is
    # PG-specific; the impl additionally gates PG-vs-MySQL). Same positive,
    # FAIL-CLOSED gate as custom_endpoint.
    "prewarm_reader": "prewarm",
    # Reader scale-out/scale-in (N-③) is a relational-only, instance-level write
    # (both PG and MySQL). Same positive, FAIL-CLOSED gate.
    "add_reader_instance": "scale_instance",
    "remove_reader_instance": "scale_instance",
    # AZ scale-out runbook planner (P2-⑥) is READ-ONLY but Aurora-relational
    # only (it describes clusters/instances), so it shares the same positive,
    # FAIL-CLOSED scale_instance gate.
    "plan_az_scaleout": "scale_instance",
    # Reader scale-out + auto-warmup (N-④): same relational-only, instance-level
    # write gate as add_reader_instance (it creates a reader).
    "scale_out_with_warmup": "scale_instance",
    "modify_dynamodb_capacity": "ddb_write",
    "modify_dynamodb_ttl": "ddb_write",
    "enable_dynamodb_pitr": "ddb_write",
    # DocumentDB Mongo-protocol write tools (stage 2). FAIL-CLOSED on None family
    # too: a documentdb cluster missing the docdb_write capability — or any
    # unresolvable cluster — refuses before reaching the impl (review fix #3).
    "set_docdb_profiler": "docdb_write",
    "create_docdb_index": "docdb_write",
    "elasticache_live_read": "live_read",
    "modify_elasticache_node_type": "elasticache_write",
    "create_elasticache_snapshot": "elasticache_write",
    "reboot_elasticache": "elasticache_write",
    "test_elasticache_failover": "elasticache_write",
    # Standalone RDS instance write tools (R-3): reboot / snapshot / modify-class.
    # POSITIVE, FAIL-CLOSED gate on the rds_instance-only instance_write cap —
    # Aurora (relational) and every non-relational family lack it, so those
    # clusters (and any unresolvable one) get unsupported_engine before the impl.
    "reboot_rds_instance": "instance_write",
    "create_rds_snapshot": "instance_write",
    "modify_rds_instance_class": "instance_write",
}

_CAP_LABEL = {
    "custom_endpoint": "Aurora 클러스터",
    "prewarm": "Aurora PostgreSQL 클러스터",
    "scale_instance": "Aurora 클러스터",
    "ddb_write": "DynamoDB 테이블",
    "docdb_write": "DocumentDB 클러스터",
    "live_read": "ElastiCache 클러스터",
    "elasticache_write": "ElastiCache 클러스터",
    "instance_write": "RDS 인스턴스(비-Aurora)",
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
    "create_custom_endpoint": {
        "impl": create_custom_endpoint_impl,
        "description": (
            "Aurora only: create a custom DB cluster endpoint (a stable DNS name "
            "routing a chosen subset of readers). endpoint_type is READER or ANY "
            "(never WRITER). static_members and excluded_members are mutually "
            "exclusive. Requires approved=true AND approval_id=<uuid from "
            "request_approval>. Returns cli_preview — the exact aws rds "
            "create-db-cluster-endpoint command this will execute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "endpoint_identifier": {"type": "string", "description": "New custom endpoint identifier (1-63 chars, letter-start, alphanumeric + hyphens)"},
                "endpoint_type": {"type": "string", "enum": ["READER", "ANY"], "default": "READER", "description": "Custom endpoint type (WRITER is not allowed)"},
                "static_members": {"type": "array", "items": {"type": "string"}, "description": "Instance IDs to INCLUDE (mutually exclusive with excluded_members)"},
                "excluded_members": {"type": "array", "items": {"type": "string"}, "description": "Instance IDs to EXCLUDE (mutually exclusive with static_members)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "endpoint_identifier"],
        },
    },
    "delete_custom_endpoint": {
        "impl": delete_custom_endpoint_impl,
        "description": (
            "Aurora only: delete a CUSTOM DB cluster endpoint. Verifies the "
            "endpoint exists and is CUSTOM first — the built-in writer/reader "
            "endpoints can NEVER be deleted through this tool. Requires "
            "approved=true AND approval_id=<uuid from request_approval>. Returns "
            "cli_preview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "endpoint_identifier": {"type": "string", "description": "Custom endpoint identifier to delete"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "endpoint_identifier"],
        },
    },
    "modify_custom_endpoint": {
        "impl": modify_custom_endpoint_impl,
        "description": (
            "Aurora only: change the StaticMembers or ExcludedMembers of an "
            "existing CUSTOM DB cluster endpoint (mutually exclusive; at least "
            "one required). Built-in writer/reader endpoints are protected. "
            "Requires approved=true AND approval_id=<uuid from request_approval>. "
            "Returns cli_preview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "endpoint_identifier": {"type": "string", "description": "Custom endpoint identifier to modify"},
                "static_members": {"type": "array", "items": {"type": "string"}, "description": "New INCLUDE list (mutually exclusive with excluded_members)"},
                "excluded_members": {"type": "array", "items": {"type": "string"}, "description": "New EXCLUDE list (mutually exclusive with static_members)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "endpoint_identifier"],
        },
    },
    "prewarm_reader": {
        "impl": prewarm_reader_impl,
        "description": (
            "Aurora PostgreSQL only: prewarm a COLD reader instance's buffer "
            "pool before it takes traffic. Optionally excludes the reader from a "
            "custom endpoint while warming, runs CREATE EXTENSION pg_prewarm/"
            "pg_buffercache on the writer, connects directly to the reader "
            "instance endpoint, pg_prewarms the top-N largest relations, and "
            "re-includes the reader. Requires approved=true AND "
            "approval_id=<uuid from request_approval>. Returns a step plan as "
            "cli_preview at the approval stage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora PostgreSQL cluster ID"},
                "reader_instance_id": {"type": "string", "description": "The COLD reader instance to warm (must be a reader, not the writer)"},
                "endpoint_identifier": {"type": "string", "description": "Optional custom endpoint to exclude the reader from while warming (auto re-included)"},
                "top_n": {"type": "integer", "default": 20, "description": "Number of largest relations to prewarm (capped)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "reader_instance_id"],
        },
    },
    "add_reader_instance": {
        "impl": add_reader_instance_impl,
        "description": (
            "Aurora only (scale-out): add a new READER instance to a cluster to "
            "expand read capacity. new_instance_id is required (names the new "
            "reader). instance_class defaults to the WRITER's current class "
            "(Serverless v2 → db.serverless) so it adds a same-shape reader; "
            "availability_zone is optional. Requires approved=true AND "
            "approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "new_instance_id": {"type": "string", "description": "Identifier for the NEW reader instance (required)"},
                "instance_class": {"type": "string", "description": "DB instance class (defaults to the writer's current class if omitted)"},
                "availability_zone": {"type": "string", "description": "Optional AZ to place the new reader in"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "new_instance_id"],
        },
    },
    "remove_reader_instance": {
        "impl": remove_reader_instance_impl,
        "description": (
            "Aurora only (scale-in): remove a READER instance from a cluster. "
            "The target must be a reader member of THIS cluster; the writer and "
            "the cluster's last remaining instance are protected and can NEVER "
            "be deleted through this tool. Deletion is irreversible. Requires "
            "approved=true AND approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "instance_id": {"type": "string", "description": "Reader instance to remove (writer/last instance are protected)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "instance_id"],
        },
    },
    "scale_out_with_warmup": {
        "impl": scale_out_with_warmup_impl,
        "description": (
            "Aurora only (scale-out + auto-warmup): add a new READER instance AND "
            "auto-queue a buffer-pool prewarm for it (semi-automatic, TWO human "
            "approvals). This tool is approval #1 (creates the reader). Once the "
            "reader reaches 'available', a prewarm_reader approval auto-appears in "
            "the Approval Center as approval #2 — after the DBA approves it, the "
            "reader is warmed automatically before it takes traffic. new_instance_id "
            "is required; instance_class defaults to the writer's class (Serverless "
            "v2 → db.serverless). Requires approved=true AND approval_id=<uuid from "
            "request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "new_instance_id": {"type": "string", "description": "Identifier for the NEW reader instance (required)"},
                "instance_class": {"type": "string", "description": "DB instance class (defaults to the writer's current class if omitted)"},
                "endpoint_identifier": {"type": "string", "description": "Optional custom endpoint to exclude the reader from while the auto-warm runs (auto re-included)"},
                "top_n": {"type": "integer", "default": 20, "description": "Number of largest relations to prewarm once the reader is available (capped)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "new_instance_id"],
        },
    },
    "plan_az_scaleout": {
        "impl": plan_az_scaleout_impl,
        "description": (
            "Aurora only (READ-ONLY): plan a preemptive AZ scale-out — N reader "
            "instances spread round-robin over the cluster's healthy AZs, "
            "EXCLUDING one chosen AZ. Resolves a concrete instance_class + AZ + "
            "unique id for each planned reader. Creates nothing; the /scaleout-az "
            "runbook turns each planned reader into an add_reader_instance approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "exclude_az": {"type": "string", "description": "AZ to exclude from the spread (empty = spread over all cluster AZs)"},
                "count": {"type": "integer", "default": 1, "description": "Number of readers to plan (1-10, clamped)"},
                "instance_class": {"type": "string", "description": "DB instance class (defaults to the writer's current class if omitted)"},
            },
            "required": ["cluster_id"],
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
    "modify_elasticache_node_type": {
        "impl": modify_elasticache_node_type_impl,
        "description": "ElastiCache only: scale the node type (modify_replication_group). Approval-gated write.",
        "input_schema": {"type": "object", "properties": {
            "cluster_id": {"type": "string"}, "node_type": {"type": "string"},
            "approved": {"type": "boolean"}, "approval_id": {"type": "string"}},
            "required": ["cluster_id", "node_type"]},
    },
    "create_elasticache_snapshot": {
        "impl": create_elasticache_snapshot_impl,
        "description": "ElastiCache (Redis/Valkey) only: create a backup snapshot. Approval-gated write.",
        "input_schema": {"type": "object", "properties": {
            "cluster_id": {"type": "string"}, "snapshot_name": {"type": "string"},
            "approved": {"type": "boolean"}, "approval_id": {"type": "string"}},
            "required": ["cluster_id", "snapshot_name"]},
    },
    "reboot_elasticache": {
        "impl": reboot_elasticache_impl,
        "description": "ElastiCache only: reboot the primary cache cluster node (reboot_cache_cluster). Approval-gated write.",
        "input_schema": {"type": "object", "properties": {
            "cluster_id": {"type": "string"},
            "approved": {"type": "boolean"}, "approval_id": {"type": "string"}},
            "required": ["cluster_id"]},
    },
    "test_elasticache_failover": {
        "impl": test_elasticache_failover_impl,
        "description": "ElastiCache only: test failover for a replication group node group (requires a replica). Approval-gated write.",
        "input_schema": {"type": "object", "properties": {
            "cluster_id": {"type": "string"},
            "node_group_id": {"type": "string"},
            "approved": {"type": "boolean"}, "approval_id": {"type": "string"}},
            "required": ["cluster_id"]},
    },
    "reboot_rds_instance": {
        "impl": reboot_rds_instance_impl,
        "description": (
            "Standalone RDS instance only (non-Aurora MySQL/SQL Server): reboot "
            "the DB instance. Aurora cluster members are refused. Requires "
            "approved=true AND approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target RDS DB instance id"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "create_rds_snapshot": {
        "impl": create_rds_snapshot_impl,
        "description": (
            "Standalone RDS instance only (non-Aurora): create a manual DB "
            "instance snapshot. snapshot_id is optional — a dbops-<id>-<ts> "
            "default is resolved at approval time and bound to it. Requires "
            "approved=true AND approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target RDS DB instance id"},
                "snapshot_id": {"type": "string", "description": "Optional snapshot identifier (default resolved+bound at approval time)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id"],
        },
    },
    "modify_rds_instance_class": {
        "impl": modify_rds_instance_class_impl,
        "description": (
            "Standalone RDS instance only (non-Aurora): change the DB instance "
            "compute class (modify_db_instance, ApplyImmediately). target_class "
            "is required; the current class is bound at approval time and the "
            "change is refused if it drifted since. Requires approved=true AND "
            "approval_id=<uuid from request_approval>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target RDS DB instance id"},
                "target_class": {"type": "string", "description": "New DB instance class (e.g. db.r6g.large)"},
                "current_class": {"type": "string", "description": "Current class bound at approval time (re-issue with the value from approval_required)"},
                "approved": {"type": "boolean", "default": False, "description": "Set to true only when DBA has approved on /approvals"},
                "approval_id": {"type": "string", "description": "UUID returned by request_approval"},
            },
            "required": ["cluster_id", "target_class"],
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
                    "enum": ["execute_sql", "modify_parameter", "modify_scaling", "manage_maintenance", "create_snapshot", "restore_cluster", "create_custom_endpoint", "delete_custom_endpoint", "modify_custom_endpoint", "prewarm_reader", "add_reader_instance", "remove_reader_instance", "scale_out_with_warmup", "modify_dynamodb_capacity", "modify_dynamodb_ttl", "enable_dynamodb_pitr", "set_docdb_profiler", "create_docdb_index", "modify_elasticache_node_type", "create_elasticache_snapshot", "reboot_elasticache", "test_elasticache_failover", "reboot_rds_instance", "create_rds_snapshot", "modify_rds_instance_class", "other"],
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
                engine_label = _CAP_LABEL.get(cap_key, cap_key)
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

"""query_activity_audit — agent-facing audit log for compliance + retro.

DBAs (and compliance auditors) regularly ask:
  - "Who changed max_connections on prod-pg-1 in the last 7 days?"
  - "Did anyone DROP a table in any cluster this week?"
  - "Show me every parameter change Alice approved this month."

Until now the agent had to compose multiple smaller tools (events,
sessions, manual scan) and stitch the answer. This tool gives it a
single primitive: query both the DDB approvals table (where every
write proposal is tracked) and the audit_log PG table (where executed
writes get stamped — currently sparse, but designed to fill in over
time as the agent migrates more execution paths to log there).

Output shape mirrors what /api/activity returns to the UI so the agent
can describe results consistently with what the DBA sees on /activity.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from mcp_servers.shared.cache_client import CacheClient


def query_activity_audit_impl(
    cache: CacheClient,
    cluster_id: str = "",
    actor: str = "",
    action_type: str = "",
    days: int = 7,
) -> dict:
    """Search the audit + approvals tables for a flexible time window.

    Args:
        cluster_id: filter to one cluster (empty = all clusters caller
            can see)
        actor: requested_by OR approved_by match (single field — the
            DDB scan checks both)
        action_type: e.g. modify_parameter, execute_sql,
            modify_scaling, manage_maintenance
        days: how far back to look (1..90; clamped)

    Returns:
        {ok, window_days, sources: [...], items: [...]} where each item
        is {ts, source, status, cluster_id, action_type, actor,
            details_excerpt}
    """
    days = max(1, min(int(days or 7), 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    items: list[dict] = []
    sources_used: list[str] = []

    # --- DDB approvals ---------------------------------------------------
    table_name = os.environ.get("APPROVALS_TABLE", "")
    if table_name:
        try:
            ddb = boto3.resource("dynamodb").Table(table_name)
            scan_kwargs: dict = {}
            filter_clauses = []
            attr_values: dict = {}
            if cluster_id:
                filter_clauses.append("cluster_id = :cid")
                attr_values[":cid"] = cluster_id
            if actor:
                filter_clauses.append(
                    "(requested_by = :a OR approved_by = :a)"
                )
                attr_values[":a"] = actor
            if action_type:
                filter_clauses.append(
                    "(action_type = :at OR tool_name = :at)"
                )
                attr_values[":at"] = action_type
            if filter_clauses:
                scan_kwargs["FilterExpression"] = " AND ".join(filter_clauses)
                scan_kwargs["ExpressionAttributeValues"] = attr_values

            resp = ddb.scan(**scan_kwargs)
            for r in resp.get("Items", []):
                # Time-window filter post-scan because created_at format
                # varies across writers (ms epoch vs ISO).
                ts = _parse_ts(r.get("created_at"))
                if ts and ts < cutoff:
                    continue
                details = r.get("action_details") or r.get("parameters") or {}
                details_str = (
                    details if isinstance(details, str)
                    else json.dumps(details, default=str)
                )
                items.append({
                    "ts": str(r.get("created_at") or ""),
                    "source": "approvals",
                    "status": r.get("approval_status"),
                    "cluster_id": r.get("cluster_id"),
                    "action_type": (
                        r.get("action_type") or r.get("tool_name")
                    ),
                    "actor": (
                        f"{r.get('requested_by') or 'agent'} → "
                        f"{r.get('approved_by') or '—'}"
                    ),
                    "details_excerpt": details_str[:500],
                })
            sources_used.append("approvals")
        except ClientError as e:
            print(f"[query_activity_audit] approvals scan failed: {e}")

    # --- PG audit_log ----------------------------------------------------
    try:
        conds = ["created_at > NOW() - MAKE_INTERVAL(days => :days)"]
        params: dict = {"days": days}
        if cluster_id:
            conds.append("cluster_id = :cid")
            params["cid"] = cluster_id
        if actor:
            conds.append("(requested_by = :a OR approved_by = :a)")
            params["a"] = actor
        if action_type:
            conds.append("(action_type = :at OR tool_name = :at)")
            params["at"] = action_type
        sql = (
            "SELECT id, cluster_id, action_type, tool_name, requested_by, "
            "       approved_by, LEFT(sql_text, 500) AS sql_text, status, "
            "       created_at "
            f"FROM audit_log WHERE {' AND '.join(conds)} "
            "ORDER BY created_at DESC LIMIT 200"
        )
        result = cache.execute(sql, params)
        for r in result.rows:
            items.append({
                "ts": str(r.get("created_at") or ""),
                "source": "audit_log",
                "status": r.get("status"),
                "cluster_id": r.get("cluster_id"),
                "action_type": r.get("action_type") or r.get("tool_name"),
                "actor": (
                    f"{r.get('requested_by') or 'agent'} → "
                    f"{r.get('approved_by') or '—'}"
                ),
                "details_excerpt": (r.get("sql_text") or "")[:500],
            })
        sources_used.append("audit_log")
    except Exception as e:
        # audit_log table may exist with no rows, or the cache_client
        # may not have permission for some reason. Don't block the
        # whole tool — surface what we got from DDB.
        print(f"[query_activity_audit] audit_log skipped: {e}")

    # Sort newest first across sources.
    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)

    return {
        "ok": True,
        "window_days": days,
        "cluster_id": cluster_id,
        "actor": actor,
        "action_type": action_type,
        "sources": sources_used,
        "count": len(items),
        "items": items[:200],
    }


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    s = str(value)
    # ms epoch (from request_approval write path)
    if s.isdigit() and len(s) >= 10:
        try:
            return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    # ISO format
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None

"""approval_guard — server-side verification that a write tool has a
valid DBA approval before it executes.

Before this module existed, write tools (`execute_sql`, `modify_parameter`,
`modify_scaling`, `manage_maintenance`) treated `approved=true` as a soft
parameter — the agent could set it without any external check. The system
prompt asked the agent to wait for DBA confirmation in chat, but that's
suggestion-grade enforcement: a prompt-injected user message or a buggy
agent could bypass it.

The guard moves enforcement into the tool itself:
  1. `request_approval` creates a DDB row with a UUID.
  2. DBA reviews on /approvals and flips `approval_status` to "approved".
  3. Agent re-issues the write tool with `approved=true` AND `approval_id`.
  4. The tool calls `verify_approval(approval_id, cluster_id, action_type)`
     which round-trips to DDB and confirms:
       - row exists
       - status == "approved"
       - cluster_id matches (no swapping clusters)
       - action_type matches (no swapping tool intent)
       - resolved_at is within the replay window (default 30 min)
       - the row has not been consumed before (mark and CAS on consume)
  5. On success the guard CONSUMES the row by writing
     `approval_status = "consumed"` so the same approval can't be replayed.

This makes the agent's `approved=true` claim verifiable against an
external authority (the DDB table that the DBA writes to), while keeping
the tool surface unchanged for read-only callers.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

# Approvals older than this (measured from `resolved_at`) cannot be replayed.
# Window is intentionally short — if the operator approved 45 minutes ago
# the world has moved on, and the agent must request fresh approval.
REPLAY_WINDOW_SECONDS = 30 * 60


def _parse_resolved_at(value: str) -> Optional[float]:
    """Resolved_at is written as an ISO-8601 timestamp by the API handler
    when the DBA clicks approve. We return epoch seconds, or None on
    parse failure (which the caller treats as "stale, reject")."""
    if not value:
        return None
    try:
        # Handle both "2026-05-28T12:34:56" and "2026-05-28T12:34:56.123456".
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _find_approval(table, approval_id: str) -> Optional[dict]:
    """Approval rows have a composite key (approval_id, created_at). Two
    code paths write rows with different created_at formats (ms-epoch from
    `request_approval`, ISO from manual POSTs), so we have to scan by
    approval_id. The table is small (one row per write request) so a scan
    is acceptable here — the alternative is a GSI which costs more than
    it saves."""
    try:
        resp = table.scan(
            FilterExpression="approval_id = :aid",
            ExpressionAttributeValues={":aid": approval_id},
            Limit=1,
        )
    except ClientError as e:
        print(f"[approval_guard] scan failed: {e}")
        return None
    items = resp.get("Items") or []
    return items[0] if items else None


def verify_approval(
    approval_id: str,
    cluster_id: str,
    action_type: str,
) -> dict:
    """Verify that `approval_id` is a fresh, matching, un-consumed approval
    for this exact (cluster_id, action_type). On success, atomically
    consume the row so it cannot be replayed.

    Returns `{"ok": True}` or `{"ok": False, "reason": "..."}`.
    """
    # Local-dev escape hatch — gated by a server-side env var so the agent
    # cannot trigger it from tool arguments. Checked BEFORE approval_id
    # validation so unit tests don't have to thread an id through.
    if os.environ.get("APPROVAL_GUARD_BYPASS") == "1":
        return {"ok": True, "bypass": True}

    if not approval_id:
        return {"ok": False, "reason": "approval_id missing — call request_approval first"}

    table_name = os.environ.get("APPROVALS_TABLE", "")
    if not table_name:
        # Fail-closed: if the deployment forgot to wire the approvals
        # table we refuse to execute writes rather than silently allowing
        # them.
        return {"ok": False, "reason": "APPROVALS_TABLE not configured — server cannot verify"}

    ddb = boto3.resource("dynamodb").Table(table_name)
    row = _find_approval(ddb, approval_id)
    if not row:
        return {"ok": False, "reason": f"approval_id {approval_id!r} not found"}

    status = row.get("approval_status", "")
    if status == "consumed":
        return {"ok": False, "reason": "approval already consumed — request a new one"}
    if status != "approved":
        return {
            "ok": False,
            "reason": f"approval status is {status!r} (need 'approved')",
        }

    row_cluster = row.get("cluster_id", "")
    if row_cluster != cluster_id:
        return {
            "ok": False,
            "reason": (
                f"approval is for cluster {row_cluster!r} but tool was "
                f"called with {cluster_id!r}"
            ),
        }

    row_action = row.get("action_type", "")
    # "other" is a permissive bucket the agent uses when the action doesn't
    # cleanly map. Tools that pass action_type="other" accept any approval
    # whose recorded action_type is also "other".
    if row_action and row_action != action_type:
        return {
            "ok": False,
            "reason": (
                f"approval is for action_type={row_action!r} but tool is "
                f"{action_type!r}"
            ),
        }

    resolved_at = _parse_resolved_at(row.get("resolved_at", ""))
    if resolved_at is None:
        return {"ok": False, "reason": "approval has no resolved_at — was it actually approved?"}
    age_seconds = time.time() - resolved_at
    if age_seconds > REPLAY_WINDOW_SECONDS:
        return {
            "ok": False,
            "reason": (
                f"approval is {int(age_seconds // 60)} minutes old "
                f"(replay window is {REPLAY_WINDOW_SECONDS // 60} min) — "
                "request a new approval"
            ),
        }

    # Atomic consume: only succeeds if status is still "approved" — prevents
    # two concurrent re-issues from both passing the check above.
    try:
        ddb.update_item(
            Key={
                "approval_id": row["approval_id"],
                "created_at": row["created_at"],
            },
            UpdateExpression="SET approval_status = :consumed, consumed_at = :now",
            ConditionExpression="approval_status = :approved",
            ExpressionAttributeValues={
                ":consumed": "consumed",
                ":approved": "approved",
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return {"ok": False, "reason": "approval was just consumed by a concurrent call"}
        print(f"[approval_guard] consume failed: {e}")
        return {"ok": False, "reason": f"consume failed: {code or str(e)[:200]}"}

    return {"ok": True}

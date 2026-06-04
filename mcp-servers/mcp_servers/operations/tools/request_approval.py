"""request_approval — agent-facing primitive for creating DBA approval rows.

When a write tool (`execute_sql` DDL/DML, `modify_parameter`, `modify_scaling`,
`manage_maintenance`) returns `{"status": "approval_required", ...}`, the
agent should call this tool with the same payload to register the request
in the Approval Center. The DBA then reviews it on `/approvals`, and on
approval the agent re-issues the write with `approved=true`.

The handoff is intentionally explicit (two tool calls) so:
  - The agent's transcript shows exactly what it proposed.
  - The DBA sees the same JSON the agent saw.
  - Replay (re-issuing after approval) is auditable.
"""

import os
import time
import uuid

import boto3


def request_approval_impl(
    cache,
    cluster_id: str,
    action_type: str,
    action_details: dict,
    requested_by: str = "agent",
) -> dict:
    """Create an approval row in the DDB approvals table and return its id
    plus a deep link the agent can mention in chat."""
    table_name = os.environ.get("APPROVALS_TABLE", "")
    if not table_name:
        return {
            "status": "error",
            "message": "APPROVALS_TABLE env var not configured on this deployment",
        }

    if action_type not in (
        "execute_sql",
        "modify_parameter",
        "modify_scaling",
        "manage_maintenance",
        "create_snapshot",
        "restore_cluster",
        "other",
    ):
        return {
            "status": "error",
            "message": f"unknown action_type {action_type!r}",
        }

    approval_id = str(uuid.uuid4())
    created_at = str(int(time.time() * 1000))  # ms epoch as string for sort key

    try:
        ddb = boto3.resource("dynamodb").Table(table_name)
        ddb.put_item(
            Item={
                "approval_id": approval_id,
                "created_at": created_at,
                "approval_status": "pending",
                "cluster_id": cluster_id,
                "action_type": action_type,
                "action_details": action_details,
                "requested_by": requested_by,
            }
        )
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}

    frontend = os.environ.get("FRONTEND_URL", "").rstrip("/")
    deep_link = f"{frontend}/approvals" if frontend else "/approvals"

    return {
        "status": "pending",
        "approval_id": approval_id,
        "cluster_id": cluster_id,
        "action_type": action_type,
        "action_details": action_details,
        "review_url": deep_link,
        "message": (
            f"DBA 승인이 등록되었습니다 (approval_id={approval_id}). "
            "검토 후 승인이 떨어지면 같은 호출을 approved=true 와 "
            f"approval_id={approval_id!r} 를 모두 넣어서 다시 실행해주세요. "
            "approval_id 가 없거나 30분이 지나면 서버가 거부합니다."
        ),
    }

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

import hashlib
import json
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


def _bypass_enabled() -> bool:
    """Whether the local-dev approval bypass is active.

    APPROVAL_GUARD_BYPASS lets local dev and unit tests skip the DDB round-trip.
    It is REFUSED inside any AWS Lambda runtime — even if the env var is somehow
    set on a deployed function (misconfig, tampering, a copy-pasted template),
    the guard will not honor it in production. `AWS_LAMBDA_FUNCTION_NAME` /
    `AWS_EXECUTION_ENV` are always present in the Lambda runtime and absent
    locally, so the bypass cannot disable approvals on a real deployment."""
    if os.environ.get("APPROVAL_GUARD_BYPASS") != "1":
        return False
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_EXECUTION_ENV"):
        print(
            "[approval_guard] APPROVAL_GUARD_BYPASS is set but IGNORED — refusing "
            "to bypass approval inside a Lambda runtime"
        )
        return False
    return True


def _norm_val(v):
    """Normalise a value for stable hashing so trivially-equal payloads hash
    the same across the request side (agent's action_details) and the execute
    side (tool args): numbers and numeric strings collapse to float (2, 2.0,
    "2" all match), bools stay bools, everything else stringifies."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    try:
        return float(s)
    except (ValueError, TypeError):
        return s


def _project(action_type: str, details: dict) -> dict:
    """Reduce an action payload to the fields that DEFINE the operation, so
    the approval is bound to *what* gets executed, not just (cluster, action).
    Both request_approval (storing the hash) and verify_approval (checking it)
    run the same projection — that's the whole point of keeping it here.

    Per-tool projections mirror each write tool's args. Aliases are tolerated
    (the approval_required response sometimes uses a different key than the
    tool arg) so the agent can pass either shape without a false mismatch."""
    d = details or {}
    if action_type == "execute_sql":
        return {"sql": str(d.get("sql") or "").strip()}
    if action_type == "modify_parameter":
        return {
            "parameter_name": d.get("parameter_name") or d.get("parameter"),
            "value": _norm_val(d.get("value")),
        }
    if action_type == "modify_scaling":
        return {
            "min_capacity": _norm_val(
                d["min_capacity"] if "min_capacity" in d else d.get("min_acu")
            ),
            "max_capacity": _norm_val(
                d["max_capacity"] if "max_capacity" in d else d.get("max_acu")
            ),
        }
    if action_type == "manage_maintenance":
        return {"window": str(d.get("window") or "").strip()}
    if action_type == "create_snapshot":
        sid = str(d.get("snapshot_id") or "").strip()
        # The approval_required response advertises "(auto-generated)" when no
        # id is given; treat that placeholder as empty so the request side
        # (which may echo the placeholder) and the execute side (empty arg →
        # auto-gen) hash to the same value instead of falsely mismatching.
        if sid == "(auto-generated)":
            sid = ""
        return {"snapshot_id": sid}
    if action_type == "restore_cluster":
        # Bind the full restore spec — an approval for "restore snapshot A into
        # cluster X" must not be reusable for snapshot B or a PITR target.
        return {
            "new_cluster_id": str(d.get("new_cluster_id") or "").strip(),
            "mode": d.get("mode") or "snapshot",
            "snapshot_id": str(d.get("snapshot_id") or "").strip(),
            "restore_to_time": str(d.get("restore_to_time") or "").strip(),
            "use_latest": bool(d.get("use_latest")),
        }
    # ===== NoSQL write tools (multi-engine #P3.6 Group C) =====
    if action_type == "modify_dynamodb_capacity":
        # Bind the table target explicitly (fix #1 — no user-controllable target
        # outside the hash) + the EFFECTIVE (validated >=1) rcu/wcu so the hashed
        # value equals the executed value (fix #4). billing_mode is the requested
        # switch ("" when not switching). force is bound for parity (v1 unused).
        return {
            "target": str(d.get("target") or "").strip(),
            "billing_mode": str(d.get("billing_mode") or "").strip(),
            "rcu": _norm_val(d.get("rcu")),
            "wcu": _norm_val(d.get("wcu")),
            "force": bool(d.get("force")),
        }
    if action_type == "modify_dynamodb_ttl":
        return {
            "attribute": str(d.get("attribute") or "").strip(),
            "enabled": bool(d.get("enabled")),
        }
    if action_type == "enable_dynamodb_pitr":
        # force is required to DISABLE (fix #7) and is hashed so the DBA approves
        # the forceful variant specifically — a disable approval can't be reused
        # for a different (enable) shape and vice-versa.
        return {
            "enabled": bool(d.get("enabled")),
            "force": bool(d.get("force")),
        }
    if action_type == "set_docdb_profiler":
        # stage 2: handler impl + pymongo bundling land later; the projection is
        # added now so the guard/Cedar stay coherent across the 5 NoSQL writes.
        return {
            "db": str(d.get("db") or "").strip(),
            "level": _norm_val(d.get("level")),
            "slowms": _norm_val(d.get("slowms")),
        }
    if action_type == "create_docdb_index":
        # stage 2: handler impl lands later. keys is an ORDERED list of
        # [field, direction] pairs (fix #2) — compound-index field order is
        # semantically significant, so we must NOT sort it: [["a",1],["b",1]] and
        # [["b",1],["a",1]] are different indexes and MUST hash differently.
        raw_keys = d.get("keys") or []
        keys = []
        for pair in raw_keys:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                keys.append([str(pair[0]), _norm_val(pair[1])])
        return {
            "db": str(d.get("db") or "").strip(),
            "collection": str(d.get("collection") or "").strip(),
            "keys": keys,
            "name": str(d.get("name") or "").strip(),
        }
    # other / unknown: bind the FULL detail set VERBATIM (no numeric coercion).
    # This closes the loose "other" bucket — an "other" approval now matches
    # only if the entire registered payload matches. We deliberately do NOT
    # run _norm_val here: collapsing distinct strings like "001" and 1 would
    # let two different "other" operations share a hash.
    return {k: d[k] for k in sorted(d.keys())}


def canonical_action_hash(action_type: str, details: dict) -> str:
    """SHA-256 of the canonical projection of an action payload. Stable across
    key ordering and trivial numeric formatting differences."""
    proj = _project(action_type, details)
    blob = json.dumps(proj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    it saves.

    NO Limit here: DynamoDB applies Limit BEFORE FilterExpression, so
    `Limit=1` means "scan one row, then filter" — as soon as the table held
    a second row the matching approval stopped being found and every write
    was refused with "not found". Paginate to the end instead."""
    kwargs = {
        "FilterExpression": "approval_id = :aid",
        "ExpressionAttributeValues": {":aid": approval_id},
    }
    try:
        while True:
            resp = table.scan(**kwargs)
            items = resp.get("Items") or []
            if items:
                return items[0]
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return None
            kwargs["ExclusiveStartKey"] = lek
    except ClientError as e:
        print(f"[approval_guard] scan failed: {e}")
        return None


def verify_approval(
    approval_id: str,
    cluster_id: str,
    action_type: str,
    payload: Optional[dict] = None,
) -> dict:
    """Verify that `approval_id` is a fresh, matching, un-consumed approval
    for this exact (cluster_id, action_type, payload). On success, atomically
    consume the row so it cannot be replayed.

    `payload` is the operation the tool is ABOUT to execute (its real args).
    If the approval row was minted with a `payload_hash` (every row created by
    `request_approval` after payload-binding shipped), the projected hash of
    `payload` must match — otherwise a harmless-looking approval cannot be
    redirected to a different SQL/parameter/window on the same cluster.

    Returns `{"ok": True}` or `{"ok": False, "reason": "..."}`.
    """
    # Local-dev escape hatch — gated by a server-side env var so the agent
    # cannot trigger it from tool arguments, AND refused inside the Lambda
    # runtime so a misconfigured deploy can't turn approvals into a no-op.
    # Checked BEFORE approval_id validation so unit tests don't have to thread
    # an id through.
    if _bypass_enabled():
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
    # 빈 action_type은 거부한다 — 비어 있으면 아래 매칭이 통째로 스킵되어
    # 임의 write tool의 승인으로 재사용될 수 있다(Codex 감사 적발). 행이
    # tool_name만 있고 action_type이 없는 레거시/수동 POST가 이 구멍을
    # 만들었다. fail-closed: action_type이 없는 승인은 어떤 쓰기도 승인하지
    # 않는다.
    if not row_action:
        return {
            "ok": False,
            "reason": "approval row has no action_type — cannot verify intent (fail-closed)",
        }
    # "other" is a permissive bucket the agent uses when the action doesn't
    # cleanly map. Tools that pass action_type="other" accept any approval
    # whose recorded action_type is also "other".
    if row_action != action_type:
        return {
            "ok": False,
            "reason": (
                f"approval is for action_type={row_action!r} but tool is "
                f"{action_type!r}"
            ),
        }

    # Payload binding: the approval is for a SPECIFIC operation payload, not
    # just an (action_type, cluster) pair. Rows minted by request_approval
    # carry a payload_hash; when present, the tool must pass the exact payload
    # it is about to run and it must hash-match. Rows without a payload_hash
    # are legacy (pre-binding) — skip the check rather than break in-flight
    # approvals across the deploy boundary.
    expected_hash = row.get("payload_hash")
    if expected_hash:
        if payload is None:
            return {
                "ok": False,
                "reason": (
                    "approval is payload-bound but the tool passed no payload "
                    "to verify — server refuses to execute"
                ),
            }
        if canonical_action_hash(action_type, payload) != expected_hash:
            return {
                "ok": False,
                "reason": (
                    "approved payload does not match the operation being "
                    "executed — request a new approval for this exact change"
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

"""Enqueue an agent task (here: auto-RCA) when an alert fires.

Writes a ``pending`` row into the agent-tasks table; the table's stream then
drives the task_worker (agent stack) which runs the RCA and stores the result.
alert_evaluator can't invoke the worker directly (data stack must not depend on
agent stack), so the table is the decoupling point.

No-op when AGENT_TASKS_TABLE isn't configured, so the caller can enqueue
unconditionally. Never raises into the caller — a failed enqueue must not break
alerting itself.

See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.
"""

import os
import time
import uuid

import boto3

# Skip a fresh auto-RCA if one for the same cluster was enqueued within this
# window — repeated/flapping alerts shouldn't spawn a pile of duplicate RCAs.
DEDUPE_MINUTES = 15
TTL_DAYS = 30


def enqueue_auto_rca(cluster_id: str, rule_id, title: str = "") -> str | None:
    """Create a pending auto_rca task for `cluster_id` unless a recent one
    exists. Returns the new task_id, or None when skipped / not configured."""
    table_name = os.environ.get("AGENT_TASKS_TABLE")
    if not table_name or not cluster_id:
        return None

    try:
        table = boto3.resource("dynamodb").Table(table_name)
        now_ms = int(time.time() * 1000)
        since = str(now_ms - DEDUPE_MINUTES * 60 * 1000)

        # Dedupe via the per-cluster GSI (created_at is fixed-width ms-epoch, so
        # string ordering == numeric ordering). FilterExpression on kind — note
        # we do NOT pass a Limit (a Limit applies BEFORE the filter and could
        # hide a matching row), we just read the small recent slice.
        resp = table.query(
            IndexName="cluster-created-index",
            KeyConditionExpression="cluster_id = :cid AND created_at > :since",
            FilterExpression="kind = :k",
            ExpressionAttributeValues={
                ":cid": cluster_id,
                ":since": since,
                ":k": "auto_rca",
            },
        )
        if resp.get("Items"):
            return None  # a recent auto-RCA is already pending/done

        task_id = str(uuid.uuid4())
        table.put_item(
            Item={
                "task_id": task_id,
                "record_type": "task",  # constant PK for the recency GSI
                "cluster_id": cluster_id,
                "kind": "auto_rca",
                "trigger": f"alert:{rule_id}",
                "status": "pending",
                "created_at": str(now_ms),
                "title": title or f"경보 자동 RCA · {cluster_id}",
                "ttl": int(time.time()) + TTL_DAYS * 24 * 60 * 60,
            }
        )
        return task_id
    except Exception as e:
        print(f"[alert-evaluator] auto-RCA enqueue failed for {cluster_id}: {type(e).__name__}: {e}")
        return None

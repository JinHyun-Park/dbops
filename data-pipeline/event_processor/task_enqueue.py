"""Enqueue an agent task (here: auto-RCA) when an alert fires.

Writes a ``pending`` row into the agent-tasks table; the table's stream then
drives the task_worker (agent stack) which runs the RCA and stores the result.
alert_evaluator can't invoke the worker directly (data stack must not depend on
agent stack), so the table is the decoupling point.

No-op when AGENT_TASKS_TABLE isn't configured, so the caller can enqueue
unconditionally. Never raises into the caller — a failed enqueue must not break
alerting itself.

VERBATIM COPY. This project shares cross-package code by duplicating the file plus a
parity test, because data-pipeline Lambdas are separate assets with no shared layer.
Copies live in alert_evaluator/, proactive_monitor/ and event_processor/, and
tests/unit/data_pipeline/test_task_enqueue_parity.py asserts they stay byte-identical.
Edit all three together.

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


def enqueue_auto_rca(
    cluster_id: str,
    rule_id,
    title: str = "",
    *,
    trigger: str = "",
    observed_at: str = "",
) -> str | None:
    """Create a pending auto_rca task for `cluster_id` unless a recent one exists.

    Returns the new task_id, or None when skipped / not configured.

    `trigger` names what raised this, defaulting to the alert form for callers that
    do not pass one. It goes on the row so /tasks can say whether a human, an alert,
    an anomaly or an engine event opened the investigation.

    `observed_at` is WHEN THE INCIDENT WAS OBSERVED, not when this was enqueued, and
    it is the reason this parameter exists. The worker used to diagnose around task
    EXECUTION time, which is a different moment: alert_evaluator reads MAX(value) over
    a 10-minute lookback on a 5-minute poll, so the spike can be 10-15 minutes older
    than the anchor. Measured on the 2026-08-30 auto-RCA: the CPU breach was at
    19:54:00 and 19:56:00, the anchor landed at 19:59:30, and by 19:59:00 CPU was
    already back to 49.6. The RCA investigated the recovery, not the incident.
    Pass an ISO 8601 UTC string; empty means "anchor on now" as before.
    """
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
                "trigger": trigger or f"alert:{rule_id}",
                "status": "pending",
                "created_at": str(now_ms),
                "title": title or f"경보 자동 RCA · {cluster_id}",
                # Read back by task_worker._run_rca and passed to
                # diagnose_root_cause as around_time. Absent means anchor on now.
                **({"observed_at": observed_at} if observed_at else {}),
                "ttl": int(time.time()) + TTL_DAYS * 24 * 60 * 60,
            }
        )
        return task_id
    except Exception as e:
        print(f"[task-enqueue] auto-RCA enqueue failed for {cluster_id}: {type(e).__name__}: {e}")
        return None

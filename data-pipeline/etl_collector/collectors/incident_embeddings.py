"""incident_embeddings — backfill pgvector embeddings for incident similarity.

Embeds event_log messages + runbooks with amazon.titan-embed-text-v2 (1024-dim)
into the `embedding vector(1024)` columns (schema_v21), so find_similar_incidents
can do semantic cosine search instead of keyword ILIKE matching.

Cache-global (not per-cluster): runs once per ETL invocation, bounded to a small
batch so a large backlog drains gradually without ballooning Bedrock cost/latency.
Best-effort — any failure is logged and never breaks the ETL run. New rows written
between runs simply get embedded on a later pass; the tool keyword-falls-back for
rows not yet embedded.

ponytail: poll-and-embed in the 5-min ETL rather than embed-on-write — event_log is
written from many places (event_processor, finding collectors), so one centralized
backfill is far less code than hooking every writer.
"""

import json

import boto3

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
EMBED_DIM = 1024
EVENT_BATCH = 25       # event_log rows embedded per run
RUNBOOK_BATCH = 10     # runbooks embedded per run
MAX_CHARS = 8000       # Titan input cap guard (truncate long bodies)


def _embed(bedrock, text):
    """Return a 1024-float embedding for text, or None on any failure."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        resp = bedrock.invoke_model(
            modelId=EMBED_MODEL,
            body=json.dumps({"inputText": text[:MAX_CHARS], "dimensions": EMBED_DIM}),
        )
        vec = json.loads(resp["body"].read()).get("embedding")
        if isinstance(vec, list) and len(vec) == EMBED_DIM:
            return vec
    except Exception as e:
        print(f"[incident_embeddings] embed failed: {type(e).__name__}: {e}")
    return None


def _rows(rds, arn, sec, db, sql):
    resp = rds.execute_statement(
        resourceArn=arn, secretArn=sec, database=db,
        sql=f"/* source=dbops-etl */ {sql}", includeResultMetadata=True,
    )
    cols = [c.get("name", "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"c{i}"
            row[col] = None if f.get("isNull") else next(
                (f[t] for t in ("longValue", "stringValue", "doubleValue") if t in f), None)
        out.append(row)
    return out


def _store(rds, arn, sec, db, table, row_id, vec):
    rds.execute_statement(
        resourceArn=arn, secretArn=sec, database=db,
        sql=f"/* source=dbops-etl */ UPDATE {table} SET embedding = :emb::vector WHERE id = :id",
        parameters=[
            {"name": "emb", "value": {"stringValue": "[" + ",".join(f"{x:.6f}" for x in vec) + "]"}},
            {"name": "id", "value": {"longValue": int(row_id)}},
        ],
    )


def collect_incident_embeddings(rds_data, cache_arn, cache_secret, cache_db):
    """Embed a bounded batch of un-embedded event_log + runbook rows. Best-effort."""
    bedrock = boto3.client("bedrock-runtime")
    embedded = {"events": 0, "runbooks": 0}
    try:
        events = _rows(
            rds_data, cache_arn, cache_secret, cache_db,
            "SELECT id, message FROM event_log "
            "WHERE embedding IS NULL AND message IS NOT NULL "
            "AND severity IN ('warning','critical','error') "
            f"ORDER BY event_time DESC LIMIT {EVENT_BATCH}",
        )
        for r in events:
            vec = _embed(bedrock, r.get("message"))
            if vec:
                _store(rds_data, cache_arn, cache_secret, cache_db, "event_log", r["id"], vec)
                embedded["events"] += 1
    except Exception as e:
        print(f"[incident_embeddings] event backfill failed: {type(e).__name__}: {e}")

    try:
        runbooks = _rows(
            rds_data, cache_arn, cache_secret, cache_db,
            "SELECT id, title, summary_md, body_md FROM runbooks "
            f"WHERE embedding IS NULL ORDER BY created_at DESC LIMIT {RUNBOOK_BATCH}",
        )
        for r in runbooks:
            text = " ".join(str(r.get(f) or "") for f in ("title", "summary_md", "body_md"))
            vec = _embed(bedrock, text)
            if vec:
                _store(rds_data, cache_arn, cache_secret, cache_db, "runbooks", r["id"], vec)
                embedded["runbooks"] += 1
    except Exception as e:
        print(f"[incident_embeddings] runbook backfill failed: {type(e).__name__}: {e}")

    return embedded

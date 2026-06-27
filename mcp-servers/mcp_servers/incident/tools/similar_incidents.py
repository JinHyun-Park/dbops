import json
import re

import boto3

from mcp_servers.shared.cache_client import CacheClient

# Semantic search: amazon.titan-embed-text-v2 (1024-dim) into the pgvector
# `embedding` columns (schema_v21). Embeddings are backfilled by the
# incident_embeddings ETL collector; this tool embeds the query symptoms and
# does a cosine search, FALLING BACK to keyword ILIKE when embedding fails or
# nothing is embedded/similar yet.
_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
_EMBED_DIM = 1024
# Cosine-distance ceiling: rows farther than this (similarity < ~0.25) aren't
# "similar" enough to surface — if none clear it, fall back to keyword search.
_MAX_COS_DISTANCE = 0.75
_bedrock = None


def _embed(text: str):
    """Embed text with Titan; return the pgvector string literal '[...]' (ready
    for ``::vector``) or None on any failure (caller keyword-falls-back)."""
    text = (text or "").strip()
    if not text:
        return None
    global _bedrock
    try:
        if _bedrock is None:
            _bedrock = boto3.client("bedrock-runtime")
        resp = _bedrock.invoke_model(
            modelId=_EMBED_MODEL,
            body=json.dumps({"inputText": text[:8000], "dimensions": _EMBED_DIM}),
        )
        vec = json.loads(resp["body"].read()).get("embedding")
        if isinstance(vec, list) and len(vec) == _EMBED_DIM:
            return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    except Exception:
        return None
    return None


def _vsearch_events(cache: CacheClient, qvec: str, cluster_id: str) -> list[dict]:
    """Cosine-nearest warning/critical/error events for ONE cluster (cluster-scoped,
    same tenancy rule as the keyword path)."""
    sql = (
        "SELECT cluster_id, event_time, event_type, severity, source, message, "
        "1 - (embedding <=> :qvec::vector) AS similarity "
        "FROM event_log "
        "WHERE cluster_id = :cluster_id AND severity IN ('warning','critical','error') "
        "AND embedding IS NOT NULL AND (embedding <=> :qvec::vector) < :maxd "
        "ORDER BY embedding <=> :qvec::vector LIMIT 10"
    )
    return cache.execute(sql, {"qvec": qvec, "cluster_id": cluster_id, "maxd": _MAX_COS_DISTANCE}).rows


def _vsearch_runbooks(cache: CacheClient, qvec: str, cluster_id: str) -> list[dict]:
    """Cosine-nearest runbooks (cluster-specific or cluster-agnostic)."""
    sql = (
        "SELECT id, cluster_id, title, summary_md, tags, source, created_at, "
        "1 - (embedding <=> :qvec::vector) AS similarity "
        "FROM runbooks "
        "WHERE (cluster_id = :cluster_id OR cluster_id IS NULL) "
        "AND embedding IS NOT NULL AND (embedding <=> :qvec::vector) < :maxd "
        "ORDER BY embedding <=> :qvec::vector LIMIT 5"
    )
    return cache.execute(sql, {"qvec": qvec, "cluster_id": cluster_id, "maxd": _MAX_COS_DISTANCE}).rows


# Tokens shorter than this are dropped (e.g. "to", "is", "db").
_MIN_KEYWORD_LEN = 3
# Cap on how many keywords we OR together to keep the query bounded.
_MAX_KEYWORDS = 8
# Common filler words that carry no diagnostic signal.
_STOPWORDS = {
    "the", "and", "for", "with", "was", "are", "but", "not", "has", "had",
    "this", "that", "from", "have", "high", "low", "when", "into", "after",
    "before", "during", "very", "some", "any", "all",
}


def _tokenize(symptoms: str) -> list[str]:
    """Break free-text symptoms into distinct lowercase keywords.

    Strips punctuation, drops very short tokens and stopwords, and de-dupes
    while preserving order so the most-specific terms stay first.
    """
    if not symptoms:
        return []
    raw = re.split(r"[^a-zA-Z0-9]+", symptoms.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for token in raw:
        token = token.strip()
        if len(token) < _MIN_KEYWORD_LEN:
            continue
        if token in _STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= _MAX_KEYWORDS:
            break
    return keywords


def _search_events(cache: CacheClient, keywords: list[str], cluster_id: str) -> list[dict]:
    """Return matching warning/critical event_log rows for ONE cluster.

    ALWAYS cluster-scoped — there is no fleet-wide mode (a fleet search would
    surface other teams' incident events to a caller who can't see those
    clusters; this MCP tool has no caller identity to scope safely). cluster_id
    is required; each row is scored by keyword matches (match_count) and ordered
    by score then recency.
    """
    params: dict = {}
    like_clauses = []
    for i, kw in enumerate(keywords):
        key = f"kw{i}"
        params[key] = f"%{kw}%"
        like_clauses.append(f"message ILIKE :{key}")
    or_block = " OR ".join(like_clauses)
    # COUNT how many keywords matched for ranking + "why matched".
    score_expr = " + ".join(
        f"(CASE WHEN message ILIKE :kw{i} THEN 1 ELSE 0 END)" for i in range(len(keywords))
    )

    conditions = [
        "severity IN ('warning', 'critical', 'error')",
        f"({or_block})",
        "cluster_id = :cluster_id",
    ]
    params["cluster_id"] = cluster_id

    sql = (
        f"SELECT cluster_id, event_time, event_type, severity, source, message, "
        f"({score_expr}) AS match_count "
        f"FROM event_log WHERE {' AND '.join(conditions)} "
        f"ORDER BY match_count DESC, event_time DESC LIMIT 10"
    )
    result = cache.execute(sql, params)
    return result.rows


def _search_runbooks(cache: CacheClient, keywords: list[str], cluster_id: str) -> list[dict]:
    """Return saved runbooks whose title/summary/body/tags match the symptoms.

    Runbooks may be cluster-agnostic (cluster_id IS NULL), so those are always
    eligible alongside ones authored for this specific cluster.
    """
    params: dict = {"cluster_id": cluster_id}
    field_clauses = []
    for i, kw in enumerate(keywords):
        key = f"rkw{i}"
        params[key] = f"%{kw}%"
        field_clauses.append(
            f"(title ILIKE :{key} OR summary_md ILIKE :{key} "
            f"OR body_md ILIKE :{key} OR array_to_string(tags, ' ') ILIKE :{key})"
        )
    or_block = " OR ".join(field_clauses)
    score_expr = " + ".join(
        f"(CASE WHEN (title ILIKE :rkw{i} OR summary_md ILIKE :rkw{i} "
        f"OR body_md ILIKE :rkw{i} OR array_to_string(tags, ' ') ILIKE :rkw{i}) THEN 1 ELSE 0 END)"
        for i in range(len(keywords))
    )
    sql = (
        f"SELECT id, cluster_id, title, summary_md, tags, source, created_at, "
        f"({score_expr}) AS match_count "
        f"FROM runbooks WHERE (cluster_id = :cluster_id OR cluster_id IS NULL) AND ({or_block}) "
        f"ORDER BY match_count DESC, created_at DESC LIMIT 5"
    )
    result = cache.execute(sql, params)
    return result.rows


def _why_matched(keywords: list[str], text: str) -> str:
    """Short explanation listing which keywords appear in the matched text."""
    if not text:
        return ""
    lowered = text.lower()
    hits = [kw for kw in keywords if kw in lowered]
    if not hits:
        return ""
    return "matched keywords: " + ", ".join(hits)


def find_similar_incidents_impl(
    cache: CacheClient,
    cluster_id: str,
    symptoms: str,
) -> dict:
    """Find past incidents/events and saved runbooks similar to free-text symptoms.

    Read-only, cluster-scoped. Embeds the symptoms (Titan) and does a pgvector
    cosine search over event_log + runbooks; falls back to keyword ILIKE search
    when nothing is embedded yet, nothing is similar enough, or Bedrock is
    unavailable. Always cluster-scoped for tenancy (no fleet-wide fallback — event
    messages carry hostnames/query fragments the caller may not be allowed to see,
    and this MCP tool has no caller identity to scope a fleet search safely).
    """
    if not symptoms or not symptoms.strip():
        return {
            "cluster_id": cluster_id,
            "symptoms": symptoms,
            "similar_incidents": [],
            "count": 0,
            "note": "No symptoms text provided.",
        }

    keywords = _tokenize(symptoms)  # used by the keyword fallback + why_matched
    method = "semantic"
    event_rows: list[dict] = []
    runbook_rows: list[dict] = []

    qvec = _embed(symptoms)
    if qvec is not None:
        try:
            event_rows = _vsearch_events(cache, qvec, cluster_id)
            runbook_rows = _vsearch_runbooks(cache, qvec, cluster_id)
        except Exception:
            event_rows, runbook_rows = [], []

    # Fall back to keyword ILIKE when semantic found nothing (no embeddings yet,
    # nothing similar enough, or Bedrock unavailable).
    if not event_rows and not runbook_rows:
        method = "keyword"
        if not keywords:
            return {
                "cluster_id": cluster_id,
                "symptoms": symptoms,
                "similar_incidents": [],
                "count": 0,
                "method": method,
                "note": "No searchable keywords could be extracted from the symptoms.",
            }
        event_rows = _search_events(cache, keywords, cluster_id=cluster_id)
        runbook_rows = _search_runbooks(cache, keywords, cluster_id)

    similar: list[dict] = []
    for row in event_rows:
        message = row.get("message") or ""
        entry = {
            "kind": "event",
            "scope": "cluster",
            "cluster_id": row.get("cluster_id"),
            "event_time": row.get("event_time"),
            "event_type": row.get("event_type"),
            "severity": row.get("severity"),
            "source": row.get("source"),
            "message": message,
        }
        if method == "semantic":
            entry["similarity"] = round(float(row.get("similarity") or 0), 3)
        else:
            entry["match_count"] = int(row.get("match_count", 0) or 0)
            entry["why_matched"] = _why_matched(keywords, message)
        similar.append(entry)

    for row in runbook_rows:
        entry = {
            "kind": "runbook",
            "runbook_id": row.get("id"),
            "cluster_id": row.get("cluster_id"),
            "title": row.get("title"),
            "summary": row.get("summary_md"),
            "tags": row.get("tags"),
            "source": row.get("source"),
            "created_at": row.get("created_at"),
        }
        if method == "semantic":
            entry["similarity"] = round(float(row.get("similarity") or 0), 3)
        else:
            haystack = " ".join(str(row.get(f) or "") for f in ("title", "summary_md"))
            entry["match_count"] = int(row.get("match_count", 0) or 0)
            entry["why_matched"] = _why_matched(keywords, haystack)
        similar.append(entry)

    if not similar:
        note = (
            f"No similar past incidents or runbooks found ({method} search). "
            "Try broader symptom terms."
        )
    else:
        note = f"Found {len(similar)} match(es) via {method} search (cluster-scoped)."

    return {
        "cluster_id": cluster_id,
        "symptoms": symptoms,
        "method": method,
        "keywords": keywords if method == "keyword" else None,
        "similar_incidents": similar,
        "count": len(similar),
        "note": note,
    }

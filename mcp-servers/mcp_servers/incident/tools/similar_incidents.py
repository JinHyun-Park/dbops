import re

from mcp_servers.shared.cache_client import CacheClient

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

    Read-only. Tokenizes the symptoms into keywords and searches the Aurora PG
    cache: warning/critical/error rows in event_log (this cluster first, then
    fleet-wide if the cluster has none) plus any matching saved runbooks.
    """
    keywords = _tokenize(symptoms)
    if not keywords:
        return {
            "cluster_id": cluster_id,
            "symptoms": symptoms,
            "similar_incidents": [],
            "count": 0,
            "note": "No searchable keywords could be extracted from the symptoms.",
        }

    similar: list[dict] = []

    # Events for THIS cluster only. A fleet-wide fallback (cluster_id=None) was
    # removed for tenancy: it surfaced other teams' incident events (messages
    # often carry hostnames / error text / query fragments) to a caller who may
    # not be allowed to see those clusters, and this MCP tool has no caller
    # identity to scope a fleet search safely. No local history => empty events.
    event_rows = _search_events(cache, keywords, cluster_id=cluster_id)

    for row in event_rows:
        message = row.get("message") or ""
        similar.append(
            {
                "kind": "event",
                "scope": "cluster",  # always cluster-scoped (no fleet fallback)
                "cluster_id": row.get("cluster_id"),
                "event_time": row.get("event_time"),
                "event_type": row.get("event_type"),
                "severity": row.get("severity"),
                "source": row.get("source"),
                "message": message,
                "match_count": int(row.get("match_count", 0) or 0),
                "why_matched": _why_matched(keywords, message),
            }
        )

    # 3) Saved runbooks (cluster-specific or cluster-agnostic).
    for row in _search_runbooks(cache, keywords, cluster_id):
        haystack = " ".join(
            str(row.get(f) or "")
            for f in ("title", "summary_md")
        )
        similar.append(
            {
                "kind": "runbook",
                "runbook_id": row.get("id"),
                "cluster_id": row.get("cluster_id"),
                "title": row.get("title"),
                "summary": row.get("summary_md"),
                "tags": row.get("tags"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "match_count": int(row.get("match_count", 0) or 0),
                "why_matched": _why_matched(keywords, haystack),
            }
        )

    if not similar:
        note = (
            f"No similar past incidents or runbooks found for keywords "
            f"{keywords}. Try broader symptom terms or widen the time range."
        )
    else:
        note = (
            f"Found {len(similar)} match(es) for keywords {keywords} "
            f"(event scope: cluster)."
        )

    return {
        "cluster_id": cluster_id,
        "symptoms": symptoms,
        "keywords": keywords,
        "similar_incidents": similar,
        "count": len(similar),
        "note": note,
    }

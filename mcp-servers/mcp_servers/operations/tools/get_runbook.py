"""get_runbook — fetch a stored runbook and extract its SQL steps.

Runbooks are markdown playbooks the DBA (or the agent) saved on /runbooks.
Until now they were a library only — view/list/create. This tool lets the
agent RETRIEVE a saved runbook, by id or by a fuzzy title/tag match, and
get back the markdown plus the fenced ```sql blocks pulled out as ordered
`steps`.

The agent's job after calling this is to PRESENT the plan to the DBA and
then run each step's SQL through `execute_sql` — which is approval-gated.
This tool NEVER executes anything; it is strictly read-only over the
`runbooks` cache table (same table api/runbooks/handler.py writes).
"""

from __future__ import annotations

import re

from mcp_servers.shared.cache_client import CacheClient

# Pull fenced ```sql ... ``` blocks out of the markdown body. The language
# tag is optional-but-checked: we only treat a block as a step when it is
# explicitly tagged `sql` (case-insensitive) so prose code samples in other
# languages aren't mistaken for executable steps.
_SQL_FENCE_RE = re.compile(
    r"```[ \t]*sql[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)

# Columns shared by both the by-id fetch and the fuzzy search so the
# returned runbook object is shaped consistently.
_RUNBOOK_COLS = (
    "id, cluster_id, title, summary_md, body_md, tags, source, "
    "source_ref, created_by, created_at::text AS created_at"
)


def get_runbook_impl(
    cache: CacheClient,
    runbook_id: str = "",
    query: str = "",
) -> dict:
    """Retrieve a saved runbook and extract its SQL steps.

    Args:
        runbook_id: exact runbook id (preferred when known). Accepts the
            numeric id as a string or int-like value.
        query: fuzzy title/tag search used when no id is given. Matches
            title ILIKE OR any tag ILIKE. Returns the best (most recent)
            match plus a short `candidates` list when ambiguous.

    Returns:
        On hit:
            {runbook: {...}, steps: [{n, sql}], content: <body_md>, note}
        On ambiguous fuzzy match:
            {runbook: {best match}, candidates: [...], steps, content, note}
        On miss:
            {error, runbook: None, steps: [], candidates: [...]}
    """
    rid = str(runbook_id or "").strip()
    q = str(query or "").strip()

    if not rid and not q:
        return {
            "error": "provide either runbook_id or query",
            "runbook": None,
            "steps": [],
        }

    candidates: list[dict] = []

    if rid:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            return {
                "error": f"invalid runbook_id: {rid!r} (expected a number)",
                "runbook": None,
                "steps": [],
            }
        rows = cache.execute(
            f"SELECT {_RUNBOOK_COLS} FROM runbooks WHERE id = :id",
            {"id": rid_int},
        ).rows
        if not rows:
            return {
                "error": f"no runbook found with id={rid_int}",
                "runbook": None,
                "steps": [],
            }
        row = rows[0]
    else:
        # Fuzzy match: title ILIKE OR any tag ILIKE. Newest first so the
        # "best" pick is the most recently saved match.
        like = f"%{q}%"
        candidates = cache.execute(
            f"SELECT {_RUNBOOK_COLS} FROM runbooks "
            "WHERE title ILIKE :like "
            "   OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE :like) "
            "ORDER BY created_at DESC LIMIT 10",
            {"like": like},
        ).rows
        if not candidates:
            return {
                "error": f"no runbook matched query={q!r}",
                "runbook": None,
                "steps": [],
                "candidates": [],
            }
        row = candidates[0]

    steps = _extract_sql_steps(row.get("body_md") or "")

    note = (
        "Present this runbook to the DBA, then run each step's SQL via the "
        "execute_sql tool. Writes (DDL/DML) require approval: call "
        "request_approval, then re-issue execute_sql with approved=true AND "
        "approval_id. NEVER bypass approval — this tool does not execute "
        "anything itself."
    )
    if not steps:
        note = (
            "No fenced ```sql blocks were found in this runbook. Read the "
            "markdown content and decide with the DBA which actions to take; "
            "any write must still go through execute_sql + approval."
        )

    result: dict = {
        "runbook": {
            "id": row.get("id"),
            "cluster_id": row.get("cluster_id"),
            "title": row.get("title"),
            "summary_md": row.get("summary_md"),
            "tags": row.get("tags") or [],
            "source": row.get("source"),
            "created_by": row.get("created_by"),
            "created_at": row.get("created_at"),
        },
        "steps": steps,
        "content": row.get("body_md") or "",
        "note": note,
    }

    # Surface a short candidate list when the fuzzy match was ambiguous so
    # the agent can confirm the right runbook with the DBA.
    if not rid and len(candidates) > 1:
        result["candidates"] = [
            {"id": c.get("id"), "title": c.get("title"), "tags": c.get("tags") or []}
            for c in candidates
        ]

    return result


def _extract_sql_steps(body_md: str) -> list[dict]:
    """Pull fenced ```sql blocks out of the markdown in document order."""
    steps: list[dict] = []
    for i, match in enumerate(_SQL_FENCE_RE.finditer(body_md), start=1):
        sql = match.group(1).strip()
        if not sql:
            continue
        steps.append({"n": i, "sql": sql})
    # Renumber after skipping empties so `n` is contiguous 1..N.
    for n, step in enumerate(steps, start=1):
        step["n"] = n
    return steps

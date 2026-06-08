"""recommend_index — derive concrete CREATE INDEX DDL from the query workload.

The previous version LEFT JOINed query_stats to index_usage on cluster_id alone
(a degenerate, cartesian-ish join), set a constant "reason" string, and emitted no
DDL — so it told a DBA *that* a query was heavy but never *what index to build*.

This version reads the heavy queries out of the `query_stats` cache and PARSES
their query_text to recover the table and the columns a composite index should
cover. The output is actual `CREATE INDEX CONCURRENTLY ...` DDL the DBA can review.

The parsing is deliberately HEURISTIC and PostgreSQL-flavored — it is regex over
SQL text, not a real parser. It handles the common shapes (single driving table,
flat WHERE predicates, simple equi-JOINs, ORDER BY) and intentionally *skips*
queries it cannot parse confidently rather than emit garbage DDL. The emitted DDL
is advice only: it is never executed here. Creating an index goes through the
normal execute_sql human-approval flow, and the column order should still be
validated with EXPLAIN against a replica before building.

The guiding principle throughout is ERR TOWARD SKIPPING: a missed recommendation
is harmless, an invalid or misleading one is not. So anything we cannot attribute
to a single concrete table+column with confidence — CTEs, subqueries, derived
tables, expression/positional ORDER BY, quoted/reserved/case-folded identifiers —
is dropped rather than guessed.
"""

import re

from mcp_servers.shared.cache_client import CacheClient

# How many heavy queries to pull from the cache. Generous enough to find a few
# parseable candidates even when some queries are too complex to parse.
_CANDIDATE_QUERY_LIMIT = 15
# Postgres identifiers max out at 63 bytes; keep generated index names under it.
_MAX_INDEX_NAME_LEN = 63
# Keep the source query in the output readable, not a multi-KB dump.
_SOURCE_QUERY_TRUNCATE = 240
# A table with seq_scan well above idx_scan is genuinely getting scanned; this is
# the multiplier used to "boost" (flag) query-derived recommendations.
_SEQ_SCAN_DOMINANCE = 1.0

# Quoted, schema-qualified identifier: optional "schema". then table, each part an
# unquoted ident or a "double-quoted" ident.
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED = rf'(?:{_IDENT}\.)?{_IDENT}'

# Driving table: FROM <table> [AS] [alias]. We only take the first table after FROM
# (the driving table); JOINed tables are handled separately via their ON clauses.
_FROM_RE = re.compile(
    rf'\bFROM\s+(?P<table>{_QUALIFIED})\s*(?:AS\s+)?(?P<alias>{_IDENT})?',
    re.IGNORECASE,
)
# JOIN <table> [AS] [alias] ON <a>.<x> = <b>.<y>
_JOIN_RE = re.compile(
    rf'\bJOIN\s+(?P<table>{_QUALIFIED})\s*(?:AS\s+)?(?P<alias>{_IDENT})?\s+ON\s+'
    rf'(?P<left>{_QUALIFIED})\s*=\s*(?P<right>{_QUALIFIED})',
    re.IGNORECASE,
)
# WHERE ... up to GROUP BY / ORDER BY / LIMIT / HAVING / end-of-string.
_WHERE_RE = re.compile(
    r'\bWHERE\b(?P<body>.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|;|$)',
    re.IGNORECASE | re.DOTALL,
)
# ORDER BY a, b.c DESC, ...  (captures the whole list; we split it ourselves).
_ORDER_BY_RE = re.compile(
    r'\bORDER\s+BY\b(?P<body>.*?)(?:\bLIMIT\b|;|$)',
    re.IGNORECASE | re.DOTALL,
)
# A WHERE predicate column: <col> <op> ... where op is one of =, >, <, >=, <=,
# IN, BETWEEN, LIKE. We capture the column reference (possibly alias.col).
_PREDICATE_RE = re.compile(
    rf'(?P<col>{_QUALIFIED})\s*(?:=|>=|<=|<>|!=|<|>|\bIN\b|\bBETWEEN\b|\bLIKE\b)',
    re.IGNORECASE,
)

# Words we must never mistake for an alias or column when the regex over-matches,
# AND reserved words that — if they appear unquoted as a table/column — mean the
# source MUST have quoted them (e.g. "order", "user"). Since we refuse to emit
# quoted DDL (the quote-folding bug), an unquoted reserved word is a skip signal.
_SQL_KEYWORDS = {
    # clause/structure keywords
    "where", "join", "inner", "left", "right", "outer", "full", "cross",
    "on", "group", "order", "by", "having", "limit", "offset", "and", "or",
    "select", "from", "as", "using", "union", "for", "with", "lateral",
    "distinct", "all", "into", "values", "set", "returning", "fetch", "window",
    "intersect", "except", "natural", "not", "null", "is", "in", "between",
    "like", "ilike", "exists", "case", "when", "then", "else", "end",
    # common reserved words that collide with table/column names
    "user", "table", "column", "index", "primary", "key", "foreign",
    "references", "constraint", "default", "check", "unique", "create",
    "drop", "alter", "insert", "update", "delete", "grant", "revoke",
    "desc", "asc", "nulls", "first", "last", "current_date", "current_time",
    "current_timestamp", "current_user", "session_user", "localtime",
    "localtimestamp", "true", "false", "to", "do", "any", "some", "array",
}

# A simple, safe, unquoted PostgreSQL identifier — the ONLY shape we trust to emit
# verbatim into DDL. Anything quoted, dotted-beyond-qualifier, or non-matching is
# rejected rather than re-quoted (quoting would case-fold incorrectly).
_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _is_simple_ident(ident: str) -> bool:
    """True only for an unquoted, non-reserved, simple identifier.

    A double-quoted source token (`"User"`, `"order"`) is intentionally rejected:
    its case/reservedness is significant and we must not strip the quotes and emit
    a case-folded, possibly-invalid identifier. Reserved words are rejected for the
    same reason (their unquoted use in real SQL would be a syntax error, so seeing
    one as a parsed name means we mis-parsed or it was quoted in the source)."""
    if not ident or not _SIMPLE_IDENT_RE.match(ident):
        return False
    return ident.lower() not in _SQL_KEYWORDS


def _split_ref(ref: str) -> tuple[str, str]:
    """Split `alias.col` or `schema.table` into (qualifier, name).

    Returns ("", name) when there is no qualifier. Only the LAST dotted segment is
    the column/table name; everything before it is treated as the qualifier. The
    raw segments are returned WITHOUT quote stripping — callers gate on
    `_is_simple_ident`, which rejects any quoted/reserved token, so a quoted source
    identifier never survives into emitted DDL.
    """
    parts = ref.split(".")
    if len(parts) == 1:
        return "", parts[0].strip()
    return parts[-2].strip(), parts[-1].strip()


def _strip_literals(query_text: str) -> str:
    """Blank out string/quoted-identifier literals so structural regexes (subquery
    detection, FROM/WHERE) don't trip over parens or keywords *inside* literals.

    Replaces the CONTENT of '...' single-quoted strings and "..." quoted
    identifiers with spaces (preserving the delimiters and overall length so
    offsets stay sane). This is intentionally simple: it does not handle dollar-
    quoting or escaped quotes perfectly, but err-toward-skipping means a parse that
    looks ambiguous after stripping is dropped anyway.
    """
    out = []
    i = 0
    n = len(query_text)
    while i < n:
        ch = query_text[i]
        if ch in ("'", '"'):
            quote = ch
            out.append(quote)
            i += 1
            while i < n and query_text[i] != quote:
                out.append(" ")
                i += 1
            if i < n:  # closing quote
                out.append(quote)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# A subquery / derived table: an opening paren whose first significant token is
# SELECT (after literals are blanked). Catches `FROM (SELECT ...)`, `IN (SELECT
# ...)`, `= (SELECT ...)`, scalar subqueries, etc.
_SUBQUERY_RE = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
# A leading WITH (CTE) before the main query.
_LEADING_WITH_RE = re.compile(r"^\s*WITH\b", re.IGNORECASE)
# A clean ORDER BY ITEM: a `(alias.)?ident` and NOTHING else but an optional
# direction (ASC/DESC) and NULLS FIRST/LAST. Matching the WHOLE comma-item (not
# just its first whitespace token) is what rejects expression sort keys like
# `o.created_at + interval '1 day'` or `o.a || o.b` — those start with a valid
# column token but continue into an expression, so indexing the leading column
# alone would be misleading. Also rejects positional (`1`) and `lower(x)`.
_ORDER_ITEM_RE = re.compile(
    r"^\s*(?P<col>[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)"
    r"(?:\s+(?:ASC|DESC))?"
    r"(?:\s+NULLS\s+(?:FIRST|LAST))?\s*$",
    re.IGNORECASE,
)


def _sanitize_index_name(raw: str) -> str:
    """Coerce a candidate index name into a valid, length-capped identifier.

    Generated from table + column names which may contain quotes/dots, so we
    collapse anything that is not [A-Za-z0-9_] to underscores and trim length.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        cleaned = "idx"
    return cleaned[:_MAX_INDEX_NAME_LEN]


def _parse_query(query_text: str) -> dict | None:
    """Best-effort, heuristic parse of a single query into index candidates.

    Returns a dict {table, columns, clauses} or None when the query cannot be
    parsed confidently (no driving table, or no usable column). `clauses` records
    which SQL clause each column came from so the rationale can explain itself.

    Column ordering follows the classic composite-index rule: equality WHERE
    columns first, then JOIN keys, then ORDER BY columns. We dedupe while keeping
    first-seen order.

    ERR TOWARD SKIPPING: returns None for any CTE/subquery/derived-table shape (we
    cannot reliably attribute inner columns to the driving table) and for any table
    that is not a simple unquoted identifier. Columns that are not simple unquoted
    identifiers are individually dropped.
    """
    # Work on a literal-stripped copy so parens/keywords inside string literals
    # ('(SELECT', 'WHERE x', etc.) don't trigger the structural guards below.
    stripped = _strip_literals(query_text)

    # Issue #1: any CTE or subquery/derived table → skip entirely. We cannot
    # attribute columns across query levels, so guessing risks misleading DDL.
    if _LEADING_WITH_RE.search(stripped):
        return None
    if _SUBQUERY_RE.search(stripped):
        return None

    from_match = _FROM_RE.search(stripped)
    if not from_match:
        return None

    # A `FROM (` is a derived table — the regex won't have matched a name, but be
    # explicit and defensive in case the engine ever surfaces it differently.
    table_qual, table_name = _split_ref(from_match.group("table"))

    # Issue #3: the table must be a simple unquoted identifier. A quoted/reserved
    # table ("User", "order") would have to be re-quoted to be valid, and stripping
    # the quotes case-folds it incorrectly — so we skip rather than emit bad DDL.
    if table_qual and not _is_simple_ident(table_qual):
        return None
    if not _is_simple_ident(table_name):
        return None

    full_table = f"{table_qual}.{table_name}" if table_qual else table_name

    # Build the alias -> "is this the driving table?" map. A column is only usable
    # for the driving table's index if it is unqualified or qualified by an alias
    # that resolves to the driving table.
    driving_aliases = {table_name.lower(), full_table.lower()}
    alias = from_match.group("alias")
    if alias:
        alias_clean = alias.strip().lower()
        # Guard against treating a keyword (WHERE/JOIN/etc.) as an alias.
        if alias_clean not in _SQL_KEYWORDS:
            driving_aliases.add(alias_clean)

    columns: list[str] = []
    clauses: dict[str, str] = {}

    def _add(col: str, clause: str) -> None:
        # Issue #3: only emit a column we can render verbatim as valid DDL.
        if col and _is_simple_ident(col) and col not in columns:
            columns.append(col)
            clauses[col] = clause

    def _belongs_to_driving(qualifier: str) -> bool:
        # Unqualified columns are assumed to belong to the driving table (best
        # effort — wrong for multi-table FROM lists, but we skip those via parse
        # confidence elsewhere). Qualified columns must match a driving alias.
        return qualifier == "" or qualifier.lower() in driving_aliases

    # 1) WHERE equality/range predicates (highest selectivity, lead the index).
    where_match = _WHERE_RE.search(stripped)
    if where_match:
        body = where_match.group("body")
        for pred in _PREDICATE_RE.finditer(body):
            qualifier, col = _split_ref(pred.group("col"))
            if _belongs_to_driving(qualifier):
                _add(col, "WHERE predicate")

    # 2) JOIN keys — index the driving-table side of each equi-join.
    for join in _JOIN_RE.finditer(stripped):
        for side in (join.group("left"), join.group("right")):
            qualifier, col = _split_ref(side)
            if _belongs_to_driving(qualifier):
                _add(col, "JOIN key")

    # 3) ORDER BY columns (let the index satisfy the sort). Accept ONLY a clean
    # `(alias.)?ident` token — reject positional (`ORDER BY 1`) and expressions
    # (`ORDER BY lower(email)`). CRUCIALLY, only trust a token that is QUALIFIED by
    # the driving alias (`o.created_at`): a BARE token like `ORDER BY email_key`
    # could be a SELECT-list alias (`SELECT lower(email) AS email_key`), not a base
    # column, and indexing it would be invalid/misleading. A bare column that is a
    # real base column will already have been picked up by WHERE/JOIN; ORDER BY
    # never introduces a new unqualified column on its own. (err toward skipping)
    order_match = _ORDER_BY_RE.search(stripped)
    if order_match:
        for raw_item in order_match.group("body").split(","):
            m = _ORDER_ITEM_RE.match(raw_item)
            if not m:
                continue  # expression / positional / multi-token → not a plain column
            qualifier, col = _split_ref(m.group("col"))
            if qualifier and qualifier.lower() in driving_aliases:
                _add(col, "ORDER BY")

    if not columns:
        return None

    return {"table": full_table, "columns": columns, "clauses": clauses}


def _build_ddl(table: str, columns: list[str]) -> str:
    """Render the CREATE INDEX CONCURRENTLY DDL for a (table, columns) pair.

    CONCURRENTLY builds the index without taking an ACCESS EXCLUSIVE lock, so an
    online table keeps serving writes during the build — at the cost of not being
    runnable inside a transaction block (surfaced in the tool-level note).
    """
    short_table = table.split(".")[-1]
    name = _sanitize_index_name("idx_" + short_table + "_" + "_".join(columns))
    col_list = ", ".join(columns)
    return f"CREATE INDEX CONCURRENTLY {name} ON {table} ({col_list});"


def _fetch_table_stats(cache: CacheClient, cluster_id: str) -> dict:
    """Pull the latest per-table seq_scan/idx_scan/n_live_tup from `table_stats`.

    Mirrors the DISTINCT ON ... ORDER BY snapshot_time DESC, cluster-scoped pattern
    in vacuum_stats. Keyed by table_name (the parser cannot reliably recover the
    schema) so it can corroborate "is this table actually getting scanned?". On any
    error or empty cache we degrade gracefully and return {} so the caller still
    emits query-derived recommendations.
    """
    sql = """
        SELECT DISTINCT ON (schema_name, table_name)
               schema_name, table_name, seq_scan, idx_scan, n_live_tup
        FROM table_stats
        WHERE cluster_id = :cluster_id
          AND snapshot_time > NOW() - INTERVAL '24 hours'
        ORDER BY schema_name, table_name, snapshot_time DESC
    """
    try:
        result = cache.execute(sql, {"cluster_id": cluster_id})
    except Exception as e:  # noqa: BLE001 — degrade gracefully, table_stats is optional
        print(f"[recommend_index] table_stats lookup failed: {e}")
        return {}

    stats = {}
    for row in result.rows:
        name = row.get("table_name")
        if name:
            stats[name] = {
                "seq_scan": row.get("seq_scan") or 0,
                "idx_scan": row.get("idx_scan") or 0,
                "n_live_tup": row.get("n_live_tup") or 0,
            }
    return stats


def recommend_index_impl(cache: CacheClient, cluster_id: str, min_seq_scan_ratio: float = 0.5) -> dict:
    """Suggest CREATE INDEX DDL by parsing the heavy queries in the cache.

    Selects queries from `query_stats` (last 24h) that read far more than they hit
    in cache (`shared_blks_read > shared_blks_hit * min_seq_scan_ratio`) ordered by
    total_time_ms, parses each to derive (table, columns), and emits a sanitized
    `CREATE INDEX CONCURRENTLY` per distinct pair. Recommendations are deduped (and
    their calls/time summed). When `table_stats` is available, recommendations are
    annotated with seq_scan/idx_scan and those whose table shows real sequential
    scanning are boosted; when it is empty/unavailable the query-derived
    recommendations are returned as-is.

    NOTE: the DDL is read-only advice. This tool never executes it.
    """
    sql = """
        SELECT query_hash, query_text, total_time_ms, calls,
               COALESCE(shared_blks_read, 0) AS blocks_read,
               COALESCE(shared_blks_hit, 0) AS blocks_hit
        FROM query_stats
        WHERE cluster_id = :cluster_id
          AND snapshot_time > NOW() - INTERVAL '24 hours'
          AND shared_blks_read > shared_blks_hit * :ratio
        ORDER BY total_time_ms DESC
        LIMIT :limit
    """
    params = {
        "cluster_id": cluster_id,
        "ratio": min_seq_scan_ratio,
        "limit": _CANDIDATE_QUERY_LIMIT,
    }
    result = cache.execute(sql, params)

    table_stats = _fetch_table_stats(cache, cluster_id)

    # Dedupe by (table, columns); merge calls/time across snapshots of the same
    # query and across distinct queries that want the same index.
    merged: dict[tuple, dict] = {}
    for row in result.rows:
        query_text = row.get("query_text") or ""
        parsed = _parse_query(query_text)
        if not parsed:
            continue

        key = (parsed["table"], tuple(parsed["columns"]))
        clauses = parsed["clauses"]
        clause_set = sorted(set(clauses.values()))
        rationale = (
            f"Columns {parsed['columns']} cover this query's "
            f"{', '.join(clause_set)} — a composite index avoids the sequential scan."
        )

        total_time = float(row.get("total_time_ms") or 0)
        calls = int(row.get("calls") or 0)

        if key in merged:
            merged[key]["total_time_ms"] += total_time
            merged[key]["calls"] += calls
        else:
            merged[key] = {
                "table": parsed["table"],
                "columns": parsed["columns"],
                "ddl": _build_ddl(parsed["table"], parsed["columns"]),
                "rationale": rationale,
                "source_query": query_text[:_SOURCE_QUERY_TRUNCATE],
                "total_time_ms": total_time,
                "calls": calls,
            }

    recommendations = []
    for rec in merged.values():
        short_table = rec["table"].split(".")[-1]
        stat = table_stats.get(short_table)
        if stat is not None:
            seq = stat["seq_scan"]
            idx = stat["idx_scan"]
            rec["seq_scan"] = seq
            rec["idx_scan"] = idx
            rec["n_live_tup"] = stat["n_live_tup"]
            # Real sequential scanning corroborates the recommendation; flag it so
            # the agent/DBA can prioritize. We never DROP query-derived advice on a
            # weak table — we just don't boost it.
            rec["seq_scan_confirmed"] = seq > idx * _SEQ_SCAN_DOMINANCE
        recommendations.append(rec)

    # Confirmed-by-stats first, then by aggregate time spent.
    recommendations.sort(
        key=lambda r: (r.get("seq_scan_confirmed", False), r["total_time_ms"]),
        reverse=True,
    )

    return {
        "cluster_id": cluster_id,
        "recommendations": recommendations,
        "count": len(recommendations),
        "note": (
            "heuristic suggestions from query-text parsing — validate with EXPLAIN "
            "and test on a replica before creating; CREATE INDEX CONCURRENTLY avoids "
            "long locks but cannot run inside a transaction block."
        ),
    }

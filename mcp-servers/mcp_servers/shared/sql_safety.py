"""sql_safety — shared SQL read-only/side-effect classification.

Single source of truth for "is this statement actually read-only?" Used by the
agent's execute_sql read fast-path AND by explain_plan's analyze=True gate
(EXPLAIN ANALYZE executes the statement, so it must not run a data-modifying CTE
or a side-effecting function). Keeping the patterns + the literal-stripping
scanner here prevents the two call sites from drifting apart.
"""

import re

# Statement PREFIXES that look like a read. Intent, not enforcement — pair with
# the side-effect / multi-statement checks below.
SAFE_PATTERNS = [r"^\s*SELECT\b", r"^\s*EXPLAIN\b", r"^\s*SHOW\b", r"^\s*DESCRIBE\b"]

# Destructive statements that require an explicit force flag at the write path.
DANGEROUS_PATTERNS = [r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\s+FROM\b"]

# Constructs that read like a SELECT/EXPLAIN but actually mutate data or change
# server state. A prefix match (^SELECT/^EXPLAIN) is intent, not enforcement:
# PostgreSQL exposes plenty of side effects from inside a SELECT.
SIDE_EFFECTING_PATTERNS = [
    r"\bEXPLAIN\b[\s(]+(?:[^;]*\b)?ANALYZE\b",  # EXPLAIN ANALYZE actually runs the stmt
    r"\bINTO\b",                                  # SELECT ... INTO creates a table
    r"\bFOR\s+(?:UPDATE|NO\s+KEY\s+UPDATE|SHARE)\b",  # locking reads
    r"\bNEXTVAL\b", r"\bSETVAL\b",               # sequence mutation
    r"\bSET_CONFIG\b",                            # runtime GUC mutation from a SELECT
    r"\bPG_TERMINATE_BACKEND\b", r"\bPG_CANCEL_BACKEND\b",
    r"\bPG_RELOAD_CONF\b", r"\bPG_ROTATE_LOGFILE\b",
    r"\bPG_SWITCH_WAL\b", r"\bPG_SWITCH_XLOG\b",
    r"\bPG_CREATE_RESTORE_POINT\b", r"\bPG_PROMOTE\b",
    r"\bPG_STAT_RESET\w*\b",
    r"\bPG_\w*ADVISORY\w*\b",                     # all advisory lock/try/unlock/xact variants
    r"\bPG_\w*REPLICATION_SLOT\w*\b",             # create/drop logical/physical slots
    r"\bPG_LOGICAL_EMIT_MESSAGE\b", r"\bPG_REPLICATION_ORIGIN_\w+\b",
    r"\bLO_IMPORT\b", r"\bLO_EXPORT\b",
    r"\bPG_READ_FILE\b", r"\bPG_READ_BINARY_FILE\b",
    r"\bDBLINK\w*\b", r"\bPG_SLEEP\w*\b",
]

# Command keywords that, when they appear AFTER a statement-separating ';',
# indicate a stacked/multi-statement payload.
_STACKED_STMT_RE = re.compile(
    r";\s*(SELECT|INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|EXPLAIN|WITH|VACUUM|ANALYZE|CALL|DO|SET|MERGE|COMMENT|REINDEX|CLUSTER)\b",
    re.IGNORECASE,
)

# Data-modifying / DDL command keywords appearing ANYWHERE in the (literal-
# stripped) statement. Catches data-modifying CTEs like
# `WITH x AS (DELETE ... RETURNING *) SELECT ...` that a prefix check misses.
_WRITE_CMD_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|"
    r"CALL|DO|VACUUM|REINDEX|CLUSTER|COPY)\b",
    re.IGNORECASE,
)


def strip_sql_literals(s: str) -> str:
    """Replace string literals, quoted identifiers, and comments so keyword
    classification reflects SQL STRUCTURE, not data. Without this, a benign
    `SELECT note FROM t WHERE note = 'paid into account'` would match
    `\\bINTO\\b`, and `WHERE label = 'a;b'` would look multi-statement.

    This is a single left-to-right scanner, NOT a sequence of regex replaces:
    interleaving matters. Stripping comments first would let `SELECT '--';
    UPDATE t SET x=1` hide its stacked statement (the `--` lives inside a
    string, but a naive comment regex would erase the rest of the line). The
    scanner recognizes whichever construct STARTS first, so a `--` inside a
    string is data and a quote inside a comment is a comment."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":                      # line comment
            i += 2
            while i < n and s[i] != "\n":
                i += 1
            out.append(" ")
        elif c == "/" and nxt == "*":                    # block comment
            i += 2
            while i < n and not (s[i] == "*" and i + 1 < n and s[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
        elif c == "'":                                   # '...' string literal
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":    # '' escaped quote
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif c == '"':                                   # "..." quoted identifier
            i += 1
            while i < n:
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append('""')
        elif c == "`":                                   # `...` MySQL identifier
            i += 1
            while i < n and s[i] != "`":
                i += 1
            i += 1
            out.append("``")
        elif c == "$":                                   # $tag$...$tag$ dollar-quote (PG)
            m = re.match(r"\$([A-Za-z_]\w*)?\$", s[i:])
            if m:
                tag = m.group(0)
                end = s.find(tag, i + len(tag))
                i = end + len(tag) if end != -1 else n
                out.append(" '' ")
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def is_multi_statement(sql: str) -> bool:
    """True if `sql` carries more than one statement. Pass the literal-stripped
    form; we drop a single trailing ';' (the common, harmless case) and then
    look for any ';' followed by a SQL command keyword."""
    body = sql.strip().rstrip(";").strip()
    return bool(_STACKED_STMT_RE.search(body))


def is_read_only_safe(sql: str) -> bool:
    """True only if the statement neither mutates data nor changes server state
    and is a single statement. Used to gate EXPLAIN ANALYZE (which executes its
    target): a data-modifying CTE, a side-effecting function, or a stacked
    statement must NOT be run. Operates on a literal/comment-stripped copy so
    data like `WHERE note = 'delete me'` doesn't trip the keyword checks."""
    stripped = strip_sql_literals(sql)
    if is_multi_statement(stripped):
        return False
    upper = stripped.upper()
    if any(re.search(p, upper) for p in SIDE_EFFECTING_PATTERNS):
        return False
    if _WRITE_CMD_RE.search(stripped):
        return False
    return True

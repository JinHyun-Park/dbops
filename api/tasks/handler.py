"""Agent Tasks REST API: the fleet-wide RCA inbox (list / get / create).

Backs the /tasks UI and the manual "run agent task" button. Reads the
agent-tasks DynamoDB table populated by alert_evaluator (auto-RCA), the
scheduler, and this handler's own POST (manual runs). Creating a task just
writes a ``pending`` row, and the table's stream drives the task_worker that
actually executes it.

/tasks is the ONE destination for a finished analysis. Two pages for the same
artifact would create two competing histories, so this route carries the whole
inbox: fleet-wide by default, newest first.

Four properties the list response is built around:

1. It is a SUMMARY, not the report. Measured on the live deployment before this
   projection: GET /api/tasks returned the FULL ``result`` per row, so 14 tasks
   came back as 127 KB while the page polls every 5s. Three auto-RCA producers
   exist now where there was one, so 100 reports would be ~900 KB per load. The
   heavy fields (candidates[], evidence, query_text, scoring_weights, trace)
   come from GET /api/tasks/{id} when a row is opened.
2. "New" means a READABLE REPORT ARRIVED, which is ``published_at`` and nothing
   else. Not queued, not an internal status flip, not the incident's own clock.
3. Tenancy is resolved BEFORE any row, count or mark is computed, so a cluster
   the caller cannot see cannot leak through a badge number either, and the
   page is ASSEMBLED under it rather than filtered after a DynamoDB Limit:
   filtering a capped page gave a minority tenant an empty inbox and a null
   publication mark, which is a permanently uninitialized client watermark.
4. A page may be short, but a short page says so. ?kind and ?cursor exist
   because recurring digests share status "done" with reports and can bury
   every report behind the newest page, and ``next_cursor`` is null only when
   the index is genuinely exhausted.

The row headline is a HYPOTHESIS. The ranker's base weights (a recent schema
change starts at 5.0, a slow query at 2.0) are an investigation policy, not
proof of causation, so no score and no confidence figure leaves this projection.

See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.
"""

import base64
import json
import os
import time
import uuid

import boto3
import tenancy
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TTL_DAYS = 30
KINDS_MANUAL = {"manual_rca"}  # kinds a user may trigger by hand (read-only RCA)
# Every kind the table actually carries: two RCA producers plus the recurring
# digest. ?kind is validated against this, because a digest carries status
# "done" exactly like a report, so ?status cannot tell them apart.
KINDS_ALL = {"auto_rca", "manual_rca", "scheduled_report"}

# Rows the caller MAY SEE in the newest-first fleet window scanned for the
# publication high-water mark. Bounded on purpose: the mark answers "has
# anything published since I last looked", and only the recent end of the table
# can change that answer.
HWM_WINDOW = 100

# Index pages one request may read while looking for the rows it was asked for.
# Bounded because /tasks polls every 5s, so an unbounded walk over a 30-day
# table is a cost and not a feature. A walk that stops on this budget hands
# back a resume cursor, so a short page is never silently short.
MAX_PAGES = 5


def _table():
    return boto3.resource("dynamodb").Table(os.environ["AGENT_TASKS_TABLE"])


def _clusters_table():
    name = os.environ.get("CLUSTERS_TABLE", "")
    return boto3.resource("dynamodb").Table(name) if name else None


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _caller_name(event: dict) -> str:
    hdrs = event.get("headers") or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return "unknown"
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return (
        claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("email")
        or "unknown"
    )


def _cluster_exists(cluster_id: str) -> bool:
    t = _clusters_table()
    if t is None:
        return True  # registry not wired on this deployment, don't block
    try:
        return "Item" in t.get_item(Key={"cluster_id": cluster_id})
    except Exception:
        return True  # fail-open on a registry read error, POST isn't destructive


def _cluster_item(cluster_id: str) -> dict:
    """Fetch {cluster_id, team_id} from the clusters registry for a single
    cluster. Returns {} on miss or infra error (caller's cluster_visible treats
    missing team_id as default-open)."""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not cluster_id or not table_name:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[tasks] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _recent_fleet(visible, limit=500):
    """The newest `limit` task rows THE CALLER MAY SEE across the fleet, newest
    first.

    recency-index is the constant-partition GSI (record_type == "task", sorted
    by created_at), which is exactly fleet-wide recency ordering.

    Backs the statistics badge, and goes through _walk_visible for the same
    reason the list and the mark do: a raw Limit caps the result BEFORE tenancy
    runs, so a caller whose rows all sit behind the newest `limit` fleet rows
    read zeros in a badge that is supposed to describe their own fleet. Same
    defect, same shape, one helper.

    The badge is a coarse summary, so it takes whatever the walk's page budget
    reaches and does not paginate: unlike the list, an approximate count over
    the recent end of the table is the thing being asked for.
    """
    rows, _ = _walk_visible(
        {
            "IndexName": "recency-index",
            "KeyConditionExpression": Key("record_type").eq("task"),
            "ScanIndexForward": False,
        },
        _key_names(""),
        visible,
        limit,
    )
    return rows


def _as_ms(ts) -> int:
    """ms-epoch string to int for comparison. 0 on anything unparseable, so a
    malformed stamp sorts below every real one instead of raising."""
    try:
        return int(str(ts))
    except (TypeError, ValueError):
        return 0


def _as_int(value):
    """DynamoDB numbers deserialize to Decimal, and json.dumps only survives a
    Decimal through default=str, so duration_ms reached the client as the STRING
    "4200". The row does arithmetic on it, so the projection hands over a real
    number, or null when the row has none."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _published_at(item: dict):
    """When this task's report became READABLE through GET /api/tasks/{id}.

    This is the ONLY definition of "new" on this route. Deliberately not
    created_at (queued), not started_at (claimed), not result.anchor_time (the
    incident's own clock, a different clock entirely), and not a generic
    updated_at that any field touch would move.

    task_worker._finish stamps published_at in the same write that puts the
    result on the row. The fallback covers rows published before that stamp
    shipped: for a done row that carries a result, the publication instant IS
    the completed_at of that write. A row with no readable result has no
    publication time at all, which is what keeps "new" from ever meaning
    "queued" or "failed".

    Returns an ms-epoch string, or None when nothing readable exists yet.
    """
    stamped = item.get("published_at")
    if stamped:
        return str(stamped)
    if item.get("status") == "done" and item.get("result") is not None:
        completed = item.get("completed_at")
        return str(completed) if completed else None
    return None


def _finding(item: dict):
    """The row headline: the top-ranked candidate's category and its own
    summary line, plus how many candidates were ranked.

    A HYPOTHESIS, and the projection is shaped to keep it readable as one. No
    score and no confidence: score = base_weight x recency x a category factor,
    where a recent schema change starts at 5.0 and a slow query at 2.0. That
    ranking says where to investigate next, not what has been proven, and a
    percentage in a row would claim the second thing. The scores, the
    score_breakdown and the evidence are all one click away on the single-task
    read, where there is room to show the derivation alongside them.

    None for a row with no ranked candidates (pending, running, failed, and
    scheduled_report digests, whose result carries `lines` instead).
    """
    result = item.get("result")
    if not isinstance(result, dict):
        return None
    cands = result.get("candidates")
    if not isinstance(cands, list) or not cands:
        return None
    top = cands[0] if isinstance(cands[0], dict) else {}
    return {
        "category": str(top.get("category") or ""),
        "summary": str(top.get("summary") or ""),
        "candidate_count": len(cands),
    }


def _anchor_time(item: dict):
    """The INCIDENT's clock, from the RCA payload. Report time and incident
    time are different clocks: an RCA published this morning about yesterday's
    event is new content about an old incident, so the row carries both."""
    result = item.get("result")
    if isinstance(result, dict) and result.get("anchor_time"):
        return str(result["anchor_time"])
    return None


def _summary_row(item: dict) -> dict:
    """Project one task row for the list: what the row renders, nothing else.

    Absent by design, and served by GET /api/tasks/{id}: result (all of it,
    including candidates[], evidence, query_text, scoring_weights,
    scoring_note, schema_observation, narrative, recommendations), trace,
    started_at, completed_at, ticket_url.
    """
    return {
        "task_id": str(item.get("task_id") or ""),
        "cluster_id": str(item.get("cluster_id") or ""),
        "kind": str(item.get("kind") or ""),
        "trigger": str(item.get("trigger") or ""),
        "status": str(item.get("status") or ""),
        "created_at": str(item.get("created_at") or ""),
        "published_at": _published_at(item),
        "duration_ms": _as_int(item.get("duration_ms")),
        "title": item.get("title"),
        "summary": item.get("summary"),
        "error": item.get("error"),
        "finding": _finding(item),
        "anchor_time": _anchor_time(item),
    }


def _visible_only(items, visible):
    """Drop rows for clusters the caller may not see. `visible` is None for an
    admin (no filter), else the allowed cluster_id set from tenancy."""
    if visible is None:
        return list(items)
    return [it for it in items if it.get("cluster_id") in visible]


def _key_names(cluster: str) -> tuple:
    """Key attributes DynamoDB requires in an ExclusiveStartKey for the index
    this request reads: the GSI's own two keys plus the table's key. Passing a
    set with any other shape is a ValidationException, which is why a cursor is
    checked against this before it is used."""
    if cluster:
        return ("cluster_id", "created_at", "task_id")
    return ("record_type", "created_at", "task_id")


def _key_of(source, key_names):
    """ExclusiveStartKey built from a row, or from a page's own
    LastEvaluatedKey. None when an attribute is missing: "cannot resume" is
    reported as an exhausted index rather than guessed at. Every row in these
    GSIs carries all three by definition, so this is a floor, not a path."""
    key = {}
    for name in key_names:
        value = (source or {}).get(name)
        if value is None or value == "":
            return None
        key[name] = str(value)
    return key


def _encode_cursor(key: dict) -> str:
    """Opaque resume token for the client.

    Carries a scan POSITION: the index sort key and the table key of one row,
    which for a walk that ran out of budget without finding anything visible
    may be a row the caller cannot see. That is a task uuid and a timestamp, no
    cluster identity and no content, so it cannot become the badge-shaped leak
    this route is otherwise written to avoid.
    """
    raw = json.dumps(key, sort_keys=True, default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(raw: str, key_names):
    """Cursor -> ExclusiveStartKey, or None when it is not one for THIS index.

    Client input at a trust boundary, so the shape is checked here: a cursor
    minted on the fleet GSI has different key names from one minted on the
    per-cluster GSI, and handing either to the wrong query is a DynamoDB
    ValidationException, which would surface as a 500 on a legitimate retry.
    """
    try:
        key = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if not isinstance(key, dict) or set(key) != set(key_names):
        return None
    if not all(isinstance(v, str) and v for v in key.values()):
        return None
    return key


def _walk_visible(index_kwargs: dict, key_names, visible, want: int, start_key=None):
    """Up to `want` rows the caller MAY SEE off a newest-first GSI, plus the
    cursor for whatever is behind them.

    The defect this exists for: a single query with Limit=want caps the result
    BEFORE tenancy is applied. Measured on a 150-row fleet whose newest 100
    rows belong to a noisier tenant, the tenant owning rows 101 to 150 got 0
    rows and a null publication mark, and a null mark never initializes the
    client watermark, so every freshness badge stays on "first visit". A fixed
    over-fetch multiplier would only move that cliff, so the filter runs per
    page and the walk continues while the page is still short.

    Returns (rows, next_key). next_key is None ONLY when the index is
    genuinely exhausted, so a short page always says whether more rows exist.
    """
    table = _table()
    rows = []
    for _ in range(MAX_PAGES):
        kwargs = dict(index_kwargs, Limit=want)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.query(**kwargs)
        rows.extend(_visible_only(resp.get("Items", []), visible))
        start_key = resp.get("LastEvaluatedKey")
        if len(rows) >= want:
            # Resume from the last row RETURNED, not from how far this page
            # read: a page that over-fetched holds rows trimmed below, and a
            # cursor taken from the page itself would skip them for good.
            return rows[:want], _key_of(rows[want - 1], key_names)
        if not start_key:
            return rows, None
    return rows, _key_of(start_key, key_names)


def _high_water_mark(visible):
    """Newest publication instant the caller may see, over a window of
    HWM_WINDOW rows THE CALLER MAY SEE. None when nothing visible there has
    published yet, or when the walk spent its page budget before reaching any
    of the caller's rows.

    Its own read, not a max over the returned rows, because the client must be
    able to persist "what I had seen" without inventing it from whatever page it
    happens to hold: a ?cluster or ?status page carries no fleet truth, and a
    page of pending rows carries no publication at all.

    Tenancy-filtered INSIDE the walk, so the mark cannot move because of a
    cluster the caller cannot see, and cannot read null just because such a
    cluster owns the newest rows.
    """
    rows, _ = _walk_visible(
        {
            "IndexName": "recency-index",
            "KeyConditionExpression": Key("record_type").eq("task"),
            "ScanIndexForward": False,
        },
        _key_names(""),
        visible,
        HWM_WINDOW,
    )
    stamps = [p for p in (_published_at(r) for r in rows) if p]
    return max(stamps, key=_as_ms) if stamps else None


def _stats(visible=None):
    rows = _visible_only(_recent_fleet(visible), visible)
    by_status, by_kind, durs = {}, {}, []
    for r in rows:
        st = str(r.get("status", "unknown"))
        by_status[st] = by_status.get(st, 0) + 1
        kd = str(r.get("kind", "unknown"))
        by_kind[kd] = by_kind.get(kd, 0) + 1
        if st == "done" and r.get("duration_ms") is not None:
            try:
                durs.append(int(r["duration_ms"]))
            except (TypeError, ValueError):
                pass
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    finished = done + failed
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_kind": by_kind,
        "success_rate": round(done / finished, 4) if finished else 0,
        "avg_duration_ms": int(sum(durs) / len(durs)) if durs else 0,
        "recent_failures": failed,
    }


def _limit(qsp: dict) -> int:
    try:
        return min(200, max(1, int(qsp.get("limit", "50"))))
    except (TypeError, ValueError):
        return 50


def _kinds(qsp: dict) -> set:
    """?kind=auto_rca,manual_rca -> the requested kinds, empty for no filter.

    A set because the view the inbox needs, "RCA reports", is two kinds. Values
    are validated by the caller against KINDS_ALL rather than dropped here:
    silently ignoring a typo hands back the digest-flooded page this filter
    exists to escape, which reads as "there are no RCA reports".
    """
    raw = (qsp.get("kind") or "").strip()
    return {k.strip() for k in raw.split(",") if k.strip()} if raw else set()


def _list(qsp: dict, visible, start_key=None):
    """One page of task rows the caller may see, newest first, plus the cursor
    for the page behind it.

    With no ?cluster this is FLEET-WIDE over recency-index. ?cluster narrows to
    one cluster's own history over cluster-created-index. ?status and ?kind
    filter within either.

    Tenancy is applied INSIDE the walk, never to the output of a capped query:
    a Limit runs before both the FilterExpression and this handler, so the page
    has to be assembled page by page until it is full. Returns (rows, cursor),
    cursor None only when the index is exhausted.
    """
    cluster = (qsp.get("cluster") or "").strip()
    status = (qsp.get("status") or "").strip()
    kinds = _kinds(qsp)

    kwargs = {"ScanIndexForward": False}
    filters = []
    if status:
        filters.append(Attr("status").eq(status))
    if kinds:
        # The whole reason ?kind exists: a scheduled_report digest carries
        # status "done" exactly like a finished report, so ?status cannot
        # separate them, and a few hourly schedules fill the newest 100 rows
        # within a day while rows live 30 days.
        filters.append(Attr("kind").is_in(sorted(kinds)))
    if filters:
        combined = filters[0]
        for extra in filters[1:]:
            combined = combined & extra
        kwargs["FilterExpression"] = combined

    if cluster:
        kwargs["IndexName"] = "cluster-created-index"
        kwargs["KeyConditionExpression"] = Key("cluster_id").eq(cluster)
    else:
        kwargs["IndexName"] = "recency-index"
        kwargs["KeyConditionExpression"] = Key("record_type").eq("task")

    # conditions.Attr() emits reserved-word-safe expressions via the SDK (auto
    # #name aliases), so the FilterExpression is safe despite "status" being a
    # DynamoDB reserved word.
    # ponytail: both GSIs project ALL, so the full result blobs still cross from
    # DynamoDB into the Lambda even though only the summary crosses the wire,
    # and a multi-page walk multiplies that internal read for a caller whose
    # rows sit deep. The 127 KB that was measured was the wire payload on a 5s
    # poll, which is what this route controls. Cutting the internal read needs
    # an INCLUDE-projection GSI, which is a CDK change, not a handler change.
    return _walk_visible(kwargs, _key_names(cluster), visible, _limit(qsp), start_key)


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path_params = event.get("pathParameters") or {}
    qsp = event.get("queryStringParameters") or {}
    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    task_id = path_params.get("id")
    raw_path = event.get("rawPath") or event.get("path") or ""

    if method == "GET" and raw_path.endswith("/stats"):
        try:
            # Tenancy BEFORE the counts. This endpoint is pure aggregate: every
            # number in it is a badge, so an unfiltered count here would be the
            # exact indirect leak the scoped list is written to prevent.
            visible = tenancy.visible_set_from_registry(event)
            return {"statusCode": 200, "headers": headers, "body": json.dumps(_stats(visible), default=str)}
        except Exception as e:
            print(f"[tasks] stats query failed: {e}")
            return {"statusCode": 500, "headers": headers,
                    "body": json.dumps({"error": "작업 통계를 집계하지 못했습니다. 잠시 후 다시 시도하세요."})}

    if method == "GET" and task_id:
        try:
            item = _table().get_item(Key={"task_id": task_id}).get("Item")
        except Exception as e:
            print(f"[tasks] get_item failed for {task_id}: {e}")
            return {"statusCode": 500, "headers": headers,
                    "body": json.dumps({"error": "작업을 불러오지 못했습니다. 잠시 후 다시 시도하세요."})}
        if item and not tenancy.cluster_visible(event, _cluster_item(item.get("cluster_id", ""))):
            return {"statusCode": 403, "headers": headers,
                    "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."})}
        if item:
            # The full row, heavy fields included: this read is the reason the
            # list can be a summary. published_at is added for a legacy row
            # that predates the producer stamp, so the client compares the same
            # field it sorted the inbox by.
            item = dict(item)
            item["published_at"] = _published_at(item)
        return {
            "statusCode": 200 if item else 404,
            "headers": headers,
            "body": json.dumps(item or {"error": "not found"}, default=str),
        }

    if method == "GET":
        # TENANCY FIRST, and one resolution shared by every number below. The
        # order is load-bearing: rows, count and the publication mark are all
        # computed from the FILTERED rows, so an invisible cluster cannot show
        # up as a row, inside a count, or as a mark that moved. Moving this
        # below the projection would leak it through the count while the rows
        # still looked correct.
        visible = tenancy.visible_set_from_registry(event)

        kinds = _kinds(qsp)
        if kinds - KINDS_ALL:
            return {"statusCode": 400, "headers": headers,
                    "body": json.dumps({"error": f"kind must be one of {sorted(KINDS_ALL)}"})}
        raw_cursor = (qsp.get("cursor") or "").strip()
        start_key = None
        if raw_cursor:
            start_key = _decode_cursor(raw_cursor, _key_names((qsp.get("cluster") or "").strip()))
            if start_key is None:
                return {"statusCode": 400, "headers": headers,
                        "body": json.dumps({"error": "invalid cursor for this query"})}

        try:
            # The page is assembled under `visible`, so the rows below ARE the
            # visible rows: nothing is filtered after a cap any more.
            rows, next_key = _list(qsp, visible, start_key)
            mark = _high_water_mark(visible)
        except Exception as e:
            print(f"[tasks] list query failed: {e}")
            return {"statusCode": 500, "headers": headers,
                    "body": json.dumps({"error": "작업 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요."})}
        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({
                "tasks": [_summary_row(r) for r in rows],
                "count": len(rows),
                "limit": _limit(qsp),
                # Non-null means rows remain behind this page, which a SHORT
                # page may also have: the walk is page-budgeted, so "fewer than
                # limit" never on its own means "that is all there is".
                "next_cursor": _encode_cursor(next_key) if next_key else None,
                "published_high_water_mark": mark,
                "high_water_mark_window": HWM_WINDOW,
            }, default=str),
        }

    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "invalid JSON body"})}
        cluster_id = (body.get("cluster_id") or "").strip()
        kind = (body.get("kind") or "manual_rca").strip()
        if not cluster_id:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "cluster_id required"})}
        if kind not in KINDS_MANUAL:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"kind must be one of {sorted(KINDS_MANUAL)}"})}
        if not _cluster_exists(cluster_id):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"unknown cluster {cluster_id}"})}
        if not tenancy.cluster_visible(event, _cluster_item(cluster_id)):
            return {"statusCode": 403, "headers": headers,
                    "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."})}

        # The requester's console language, recorded so the worker can GENERATE
        # the narrative and the recommendations in the language this operator
        # reads (model prose is not an i18n key, so a render site cannot
        # translate it afterwards). Allowlisted to the two languages the console
        # offers, because the value ends up steering a Bedrock prompt: anything
        # else is DROPPED rather than stored, and a row with no locale makes the
        # worker fall back to the deployment default, which is Korean unless an
        # admin changed it. So an older frontend that sends nothing keeps
        # today's behaviour exactly.
        locale = (body.get("locale") or "").strip().lower()

        now_ms = int(time.time() * 1000)
        new_task_id = str(uuid.uuid4())
        item = {
            "task_id": new_task_id,
            "record_type": "task",
            "cluster_id": cluster_id,
            "kind": kind,
            "trigger": f"manual:{_caller_name(event)}",
            "status": "pending",
            "created_at": str(now_ms),
            "title": f"수동 RCA ({cluster_id})",
            "ttl": int(time.time()) + TTL_DAYS * 24 * 60 * 60,
            **({"locale": locale} if locale in ("ko", "en") else {}),
        }
        try:
            _table().put_item(Item=item)
        except ClientError as e:
            print(f"[tasks] put_item failed for {cluster_id} ({kind}): {e}")
            return {"statusCode": 500, "headers": headers,
                    "body": json.dumps({"error": "작업 생성에 실패했습니다. 잠시 후 다시 시도하세요."})}
        return {"statusCode": 201, "headers": headers, "body": json.dumps(item, default=str)}

    return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "Method not allowed"})}

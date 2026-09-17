"""task_worker: executes agent-tasks rows as they are inserted.

Triggered by the ``agent-tasks`` DynamoDB stream. For each INSERTed row with
``status=pending`` it atomically claims the row (pending -> running), runs the
generator for the row's ``kind``, and writes the result back (done / failed)
plus a best-effort in-app WebSocket push.

This is the SINGLE processing path for all task sources: alert auto-RCA,
scheduled reports, and manual runs each just write a pending row; this worker is
the only thing that executes them. See
docs/superpowers/specs/2026-06-18-agent-tasks-design.md.

RCA is deterministic at its core: it reuses the incident server's
``diagnose_root_cause`` tool (the same one the agent calls), so the ranking is
fast, cheap and safe to run unattended in a Lambda. ONE best-effort Bedrock call
sits on top of it (``_narrative``), writing the prose assessment and the
recommendations in the task's own language; it can fail without taking the task
with it, and the ranked signals ship either way.
"""

import json
import os
import re
import time
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl
from mcp_servers.incident.tools.health_status import get_health_status_impl
from mcp_servers.shared.app_config import get_config
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.workers.ticketing import get_provider

_DESER = TypeDeserializer()

# The two languages the console offers, and the ONLY two values that may reach a
# prompt. Exact-match allowlist, not a prefix test: a task row is data, and the
# language it asks for steers a model prompt, so an unrecognised value is
# rejected rather than normalised into something that gets interpolated.
_LOCALES = ("ko", "en")

# Every piece of prose whose language follows the task locale, both languages in
# one table so a reviewer can check on one screen that the hedge survived the
# crossing (RISK: a lost "가능성 / likely" is the damage no test catches).
#
# The two branches may differ in the LANGUAGE DIRECTIVE only, never in what the
# report contains. The English "analyze" entry is longer because it restates
# the hedge and the verbatim-identifier rule that the shared Korean prompt body
# carries implicitly for a model writing Korean; an extra clause that asks for
# extra CONTENT (a "what to check" line, say) would give the same incident two
# differently shaped reports depending on who requested it.
_LANG = {
    "ko": {
        # The output-language directive inside the analysis prompt.
        "analyze": "한국어로 분석하세요. ",
        # The trailing clause of the system prompt.
        "answer": "항상 한국어로 답합니다.",
        # Trace detail. rca-report.tsx renders it through t(), NOT raw, so this
        # ko value needs an en-server.ts key: a ko-locale task is readable on an
        # en console, and the label is the one part of a frozen Korean report
        # that can still be translated.
        # ponytail: the label alone has a key. With duplicates dropped the
        # rendered detail is label + "dropped", which no exact-equality lookup
        # can match; upgrade path is a structured trace field, not a key per
        # count.
        "trace": "한국어 narrative+권장조치",
        "dropped": ", 중복 권장 {n}건 제거",
    },
    "en": {
        "analyze": (
            "Write the analysis in English. Keep the hedging the Korean asks "
            "for: state what these signals make LIKELY, never assert a "
            "confirmed cause. Keep metric names, parameter names, SQL and "
            "cluster IDs verbatim. "
        ),
        "answer": (
            "Always answers in English, and hedges any cause the signals only "
            "suggest rather than establish."
        ),
        "trace": "English narrative + recommendations",
        "dropped": ", {n} duplicate recommendation(s) removed",
    },
}


def _task_locale(row_locale) -> str:
    """The language this task's narrative and recommendations are GENERATED in.

    FAIL-SAFE TO KOREAN. Korean is what every deployment ships today, and
    silently switching an existing deployment's report language is not this
    function's call, so anything unrecognised ends at "ko".

    A MANUAL RCA has a caller: api/tasks stamps the operator's console locale on
    the row from the POST body. An AUTOMATED RCA has none, because this worker
    runs off the agent-tasks DynamoDB stream with no request behind it: the
    alert_evaluator / event_processor / task_scheduler producers enqueue a row
    with no locale, so the DEPLOYMENT DEFAULT decides. That default is read
    through get_config, i.e. the app-config table (admin-editable in /settings)
    over the env var over "ko", and get_config never raises.

    The default is resolved HERE, in the one consumer, on purpose: the four
    task_enqueue.py copies are a whole-file byte-identical parity family and
    three of them cannot import mcp_servers, so teaching the producers about a
    default would mean two more copies of app_config.py to read one string.

    The return value is always a member of _LOCALES, so no caller-supplied text
    can reach the prompt: the prompt reads _LANG[lang], never the raw value.
    """
    loc = str(row_locale or "").strip().lower()
    if loc in _LOCALES:
        return loc
    default = str(get_config("DEFAULT_LOCALE", "ko") or "").strip().lower()
    return default if default in _LOCALES else "ko"

# Lazily built so a cold container that only handles a malformed record never
# pays the cache-client init.
_cache = None


def _get_cache() -> CacheClient:
    global _cache
    if _cache is None:
        _cache = CacheClient()
    return _cache


def _table():
    return boto3.resource("dynamodb").Table(os.environ["AGENT_TASKS_TABLE"])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ddb_safe(value):
    """boto3's DynamoDB resource rejects Python floats ("Float types are not
    supported"). diagnose_root_cause returns scores/ratios as floats, so convert
    them to Decimal recursively before persisting the result. Mirrors
    request_approval._ddb_safe."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb_safe(v) for v in value]
    return value


def _deser_image(image: dict) -> dict:
    """DynamoDB stream NewImage is low-level ({"k": {"S": "v"}}); deserialize."""
    return {k: _DESER.deserialize(v) for k, v in (image or {}).items()}


def _broadcast(payload: dict) -> int:
    """Push `payload` to all connected WS clients. Best-effort, never raises.

    Copied from data-pipeline/.../ws_notify.broadcast (kept in sync): the
    broadcasting Lambdas each carry their own copy rather than share a layer.
    """
    table_name = os.environ.get("WS_CONNECTIONS_TABLE")
    endpoint = os.environ.get("WS_MGMT_ENDPOINT")
    if not table_name or not endpoint:
        return 0  # push channel not configured on this deployment
    ddb = boto3.resource("dynamodb").Table(table_name)
    mgmt = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
    data = json.dumps(payload, default=str).encode("utf-8")

    items = []
    scan_kwargs = {"ProjectionExpression": "connection_id"}
    try:
        while True:
            resp = ddb.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        print(f"[task-worker] connections scan failed: {type(e).__name__}: {e}")
        return 0

    delivered = 0
    for it in items:
        cid = it.get("connection_id")
        if not cid:
            continue
        try:
            mgmt.post_to_connection(ConnectionId=cid, Data=data)
            delivered += 1
        except mgmt.exceptions.GoneException:
            try:
                ddb.delete_item(Key={"connection_id": cid})
            except Exception:
                pass
        except Exception as e:
            print(f"[task-worker] post_to_connection {cid[:8]} failed: {type(e).__name__}")
    return delivered


def _claim(task_id: str) -> bool:
    """Atomically move pending -> running. Returns True iff we won the claim.

    A stream record can be redelivered (shard retry, at-least-once) and the same
    INSERT can surface on a re-drive. The conditional write makes execution
    idempotent: only the first claimer runs the work."""
    try:
        _table().update_item(
            Key={"task_id": task_id},
            UpdateExpression="SET #s = :running, started_at = :ts",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":pending": "pending",
                ":ts": str(_now_ms()),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _finish(task_id, *, status, result=None, summary=None, error=None, ticket_url=None, trace=None, duration_ms=None):
    names = {"#s": "status"}
    vals = {":s": status, ":ts": str(_now_ms())}
    sets = ["#s = :s", "completed_at = :ts"]
    if result is not None:
        vals[":r"] = _ddb_safe(result)
        sets.append("#r = :r")
        names["#r"] = "result"
        # published_at marks AVAILABILITY, not progress: THIS is the write that
        # puts a readable report on the row, so the stamp belongs here and
        # nowhere earlier. Not created_at (queued), not started_at (claimed),
        # and not completed_at, which a `failed` row carries too.
        #
        # A `failed` finish passes no result: that is a failure notification, not
        # a published report, so it stays unstamped. A result that legitimately
        # found nothing ("뚜렷한 원인 미발견, 수동 점검 권장") IS stamped, because
        # that is a real outcome the operator needs to read.
        #
        # Same convention as created_at / started_at / completed_at in this
        # table: decimal ms-epoch in a STRING. It is fixed width (13 digits from
        # 2001 until 2286), so string ordering equals numeric ordering on the
        # cluster-created-index GSI, which is what lets a reader range-query it
        # the way alert_evaluator already range-queries created_at. An ISO
        # timestamp or a number would sort wrongly against the existing rows.
        # Reuses :ts, so published_at and completed_at are the same instant by
        # construction rather than two clocks that can disagree.
        #
        # The status gate holds the same invariant the READER already asserts:
        # api/tasks._published_at falls back to completed_at only for a row that
        # is `done` AND carries a result, "which is what keeps 'new' from ever
        # meaning 'queued' or 'failed'". No caller passes a result with a
        # non-done status today, so the gate never fires on the live paths. It is
        # what keeps a future partial-result failure path from stamping a row the
        # reader would then surface as a newly arrived report, so it is tested by
        # calling _finish directly with that pair rather than through a handler
        # path that cannot produce it.
        if status == "done":
            sets.append("published_at = :ts")
    if summary is not None:
        vals[":sum"] = summary
        sets.append("summary = :sum")
    if ticket_url is not None:
        vals[":turl"] = ticket_url
        sets.append("ticket_url = :turl")
    if trace is not None:
        vals[":trace"] = _ddb_safe(trace)
        sets.append("#trc = :trace")
        names["#trc"] = "trace"
    if duration_ms is not None:
        vals[":dur"] = int(duration_ms)
        sets.append("duration_ms = :dur")
    if error is not None:
        vals[":e"] = error[:500]
        sets.append("#err = :e")
        names["#err"] = "error"
    _table().update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


def _maybe_create_ticket(task_id, cluster_id, kind, summary, result) -> Optional[str]:
    """Hand a completed task to the configured ticketing provider and return the
    ticket URL, or None when ticketing is disabled / no ticket was created.

    Inert by default (``TICKETING_PROVIDER`` unset → no-op provider → None). The
    call is isolated: any provider failure is logged and treated as "no ticket"
    so the ticketing seam can never block or fail task completion."""
    try:
        return get_provider().create_ticket(
            task_id=task_id,
            cluster_id=cluster_id,
            kind=kind,
            summary=summary,
            result=result,
        )
    except Exception as e:
        print(f"[task-worker] ticketing seam failed for {task_id}: {type(e).__name__}: {e}")
        return None


def _history_line(cache, cluster_id: str, category: str) -> str:
    """One-line track record for an 'rca:<category>' symptom, cluster then fleet
    fallback. Empty string when there's no history (caller omits the line)."""
    sclass = f"rca:{category}"
    try:
        rows = cache.execute(
            "SELECT action_class, successes, attempts FROM remediation_outcomes_agg "
            "WHERE cluster_id = :cid AND symptom_class = :sc AND attempts > 0 "
            "ORDER BY attempts DESC LIMIT 5",
            {"cid": cluster_id, "sc": sclass},
        ).rows
        if not rows:
            rows = cache.execute(
                "SELECT action_class, successes, attempts FROM remediation_outcomes_agg "
                "WHERE cluster_id = '*' AND symptom_class = :sc AND attempts > 0 "
                "ORDER BY attempts DESC LIMIT 5",
                {"sc": sclass},
            ).rows
    except Exception:
        return ""
    if not rows:
        return ""
    parts = [f"{r['action_class']} {int(r['successes'])}/{int(r['attempts'])}" for r in rows]
    return "과거 효과 이력(조치 성공/시도): " + ", ".join(parts)


def _narrative(cluster_id: str, rca: dict, lang: str = "ko"):
    """Hybrid layer: turn the deterministic candidate signals into a root-cause
    narrative + concrete recommendations via ONE Bedrock call, in `lang`.

    The prose is GENERATED in the operator's language, never translated after
    the fact: model output is not an i18n key, so the render site cannot look it
    up. `lang` must already be a member of _LOCALES (see _task_locale); it
    selects a fixed directive out of _LANG and is never interpolated itself.
    Defaults to "ko", the same fail-safe _task_locale lands on.

    Best-effort: returns None (and the task still completes with the raw
    ranked signals) if the model isn't configured or the call/parse fails. The
    prompt is constrained to the supplied signals so the model can't invent
    causes the data doesn't support."""
    model_id = os.environ.get("RCA_NARRATIVE_MODEL_ID", "")
    candidates = rca.get("candidates") if isinstance(rca, dict) else None
    if not model_id or not candidates:
        return None
    words = _LANG.get(lang) or _LANG["ko"]

    lines = [
        f"- [{c.get('category')}] {c.get('summary')} (score {c.get('score')}, {c.get('when')})"
        for c in candidates[:8]
    ]
    top_category = candidates[0].get("category", "") if candidates else ""
    history = ""
    if top_category:
        try:
            history = _history_line(_get_cache(), cluster_id, top_category)
        except Exception:
            history = ""
    history_section = (
        f"\n\n{history}\n위 '과거 효과 이력'을 근거로 우선순위를 정하되, 이력에 없는 효과는 단정하지 마세요."
        if history else ""
    )
    prompt = (
        f"클러스터 '{cluster_id}'의 근본원인 분석 후보 신호입니다 "
        "(상관관계 기반 증거, 점수 내림차순):\n"
        + "\n".join(lines)
        + f"\n\n검사한 신호 수: {rca.get('signals_examined', {})}"
        + history_section
        # Only the output-language directive moves. The instruction bodies stay
        # Korean on purpose, the same call agent/prompts/system_prompt.py makes:
        # the model follows a Korean instruction to answer in English perfectly
        # well, and rewriting tuned prompt text is a behaviour change nobody
        # asked for. It also keeps the hedge ("가장 가능성 높은") in ONE place
        # instead of two that can drift apart.
        + "\n\n위 신호만 근거로(신호에 없는 원인은 추측 금지) "
        + words["analyze"]
        + "반드시 아래 JSON 형식만 출력하세요:\n"
        '{"narrative": "가장 가능성 높은 근본 원인을 2-3문장으로", '
        '"recommendations": ["구체적이고 실행 가능한 권장 조치", "..."]}'
    )
    try:
        resp = boto3.client("bedrock-runtime").converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": (
                "당신은 Aurora/RDS 데이터베이스 운영(DBA) 전문가입니다. 제공된 신호만으로 "
                "간결하고 실무적으로 진단합니다. " + words["answer"]
            )}],
            # NO `temperature`. Claude Sonnet 5 and the rest of the Claude 5 family
            # REJECT it: converse returns ValidationException "`temperature` is
            # deprecated for this model". Reproduced 2026-09-15 against
            # global.anthropic.claude-sonnet-5 in ap-northeast-2, and the identical call
            # with the key removed returns 200 on the same model, account and region, so
            # it is not IAM, not the VPC, not model access.
            #
            # This broke silently for 12 days. RCA_NARRATIVE_MODEL_ID moved to
            # claude-sonnet-5 on 2026-09-03 (4326c0f) while this line still sent
            # temperature, so _narrative raised on EVERY call and returned None. The task
            # still finished `done` with the trace line "모델 미설정/실패 - 스킵", so the
            # RCA shipped its ranked candidates with no narrative and no recommendations
            # and nothing reported a failure. Pinning an inference parameter that a model
            # family can retire is what made a model swap a silent feature outage.
            # 900 was measured sitting exactly ON the limit, not under it: two
            # identical opus-5 calls with the worker's own prompt spent 861 and 900
            # output tokens, the second stopping at `max_tokens`. A truncated
            # response cuts the JSON mid-object, so json.loads fails and the RCA
            # ships with no narrative and no recommendations, which is the same
            # silent outcome as the model being misconfigured. Headroom is cheap
            # here: this runs once per incident, not once per chat turn.
            inferenceConfig={"maxTokens": 2000},
        )
        # EVERY text block, not content[0]. Indexing the first block assumes a
        # response shape, and this file already has a 12-day outage on record from
        # assuming something about a model family (a pinned `temperature` that
        # Claude 5 retired). Measured live on 2026-09-15 with opus-5: one RCA
        # produced a narrative and the next died on `KeyError: 'text'`, same code,
        # same model, same prompt shape, so content[0] is not reliably a text block.
        # A response that carries reasoning or any other block type first now costs
        # nothing instead of the whole narrative.
        blocks = resp.get("output", {}).get("message", {}).get("content") or []
        text = "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b).strip()
        stop = resp.get("stopReason")
        if not text:
            # The block keys and the stop reason, because `KeyError: 'text'` told us
            # nothing about what DID come back and cost a live debugging round.
            print(f"[task-worker] narrative had no text block for {cluster_id}: "
                  f"stopReason={stop} blocks={[sorted(b) for b in blocks if isinstance(b, dict)]}")
            return None
        if stop == "max_tokens":
            # Truncation cuts the JSON mid-object, so json.loads below fails and the
            # narrative vanishes with no stated reason. Logged as the cause it is.
            print(f"[task-worker] narrative truncated at maxTokens for {cluster_id}; "
                  "the JSON is incomplete and will not parse")
        # Models sometimes wrap JSON in prose / fences, so extract the object.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        obj = json.loads(text[start : end + 1])
        out = {}
        if obj.get("narrative"):
            out["narrative"] = str(obj["narrative"])
        if isinstance(obj.get("recommendations"), list):
            out["recommendations"] = [str(r) for r in obj["recommendations"] if r]
        if out:
            # Stamped on the RESULT, not just asked for on the way in, because a
            # stored narrative is frozen in the language that produced it and the
            # task inbox is fleet-wide: two operators with different consoles read
            # the same row. This tells a reader what they are looking at instead
            # of making them guess from the characters. Only set when there IS
            # prose, so the field never claims a language for a missing narrative.
            out["narrative_locale"] = lang
        return out or None
    except Exception as e:
        print(f"[task-worker] narrative gen failed for {cluster_id}: {type(e).__name__}: {e}")
        return None


# Measured, not guessed. Over hand-built Korean and English pairs, genuinely
# different advice ("세션을 종료" vs "인덱스를 추가" after an identical first
# clause) topped out at 0.5 token overlap, while real paraphrases scored 0.667 to
# 1.0. Character-level similarity was tried first and REJECTED: it scored a
# reordered paraphrase at 0.53 and a different-action pair at 0.77, i.e. inverted.
_DUP_ADVICE_OVERLAP = 0.6

_HANGUL = re.compile(r"[가-힣]+")


def _advice_tokens(text: str) -> set:
    """Comparable content tokens of one piece of advice.

    Set semantics drop word order, which is most of what paraphrasing changes.
    Two normalizations on top:
      - a Korean word is cut to its first two syllables, where the stem sits, so
        "낮추세요" and "낮추십시오" collapse to one token and the model's choice of
        verb ending stops reading as new information.
      - a Korean particle stuck on an identifier is stripped, so "work_mem을" and
        "work_mem" are one token.
    """
    out = set()
    for word in re.sub(r"[^0-9a-z가-힣\s_]+", " ", text.lower()).split():
        if _HANGUL.fullmatch(word):
            if len(word) >= 2:
                out.add(word[:2])
        else:
            word = _HANGUL.sub("", word)  # identifier + particle -> identifier
            if len(word) > 1:
                out.add(word)
    return out


def _same_advice(a: str, b: str) -> bool:
    """True when two pieces of advice tell the operator to do the same thing.

    Conservative by construction, because dropping a DIFFERENT recommendation is
    worse than showing a near-duplicate:
      1. the identifiers (metric names, parameter names, numbers) must match
         exactly, because advice about two different knobs can be word-for-word
         identical apart from the name. Measured: "shared_buffers 값을 늘려 캐시
         적중률을 높이세요" against the same line with work_mem shares 5 of 7
         tokens, 0.714, so overlap alone WOULD have eaten one of the two
         changes.
      2. then 60% of all tokens have to be shared (see _DUP_ADVICE_OVERLAP).

    Both sides are prose from the same narrative model in the same language,
    whichever language that task asked for, so lexical comparison is comparing
    like with like. That is why this stays inside ONE list (see _dedupe_advice).

    IT IS STRICTER ON ENGLISH, and the measurements say how much. Every English
    token is ASCII, so gate 1 degenerates to "the entire token sets must be
    equal" and an English paraphrase dies THERE, without ever being scored: the
    0.6 threshold is unreachable for pure English, and only a reordered or
    repunctuated restatement (identical token sets) collapses. Measured:
    "Raise work_mem to 16MB." against "Increase work_mem to 16MB." scores 0.600
    overlap and returns False, rejected at gate 1 on raise vs increase.

    The consequence, stated plainly: within-list dedupe is effectively INERT on
    an English task. Measured on one three-item list holding the same advice
    twice plus one different item, ko dropped 1 and en dropped 0, so an English
    reader sees the model's restatements a Korean reader never saw.

    It stays that way anyway. Relaxing gate 1 to identifier-like tokens only
    would collapse "Add an index on orders(created_at)" against the same line
    naming users(created_at) at 0.714, deleting advice about a different table,
    and "Raise work_mem to 16MB" against "Raise shared_buffers to 16MB" scores
    0.600 for the same reason on a parameter. Deleting different advice is worse
    than showing a near-duplicate, so the strictness is a deliberate ceiling,
    not a claim that the dedupe works on English.
    ponytail: lexical ceiling, adequate because it only ever sees one model's
    output in one language. The upgrade path is Titan embeddings, already wired
    in incident/tools/similar_incidents.py, if paraphrases stop being lexical.
    """
    ta, tb = _advice_tokens(a), _advice_tokens(b)
    if not ta or not tb:
        return False
    if {t for t in ta if t.isascii()} != {t for t in tb if t.isascii()}:
        return False
    return len(ta & tb) / len(ta | tb) >= _DUP_ADVICE_OVERLAP


def _dedupe_advice(res: dict) -> int:
    """Drop `recommendations` that restate an earlier one, keeping the first of
    each duplicate pair, and return how many were dropped.

    Deduped HERE, where the result is assembled, so every reader benefits
    instead of each UI fixing it for itself. The model restates itself across
    candidates of the same category, which is the repetition this removes.

    Scope is the model's own list ONLY. It deliberately does NOT compare a
    recommendation against a candidate's `suggested_action`, and the reason is
    NOT that the two lists are in different languages. They used to be, because
    the collectors write English and this narrative was pinned to Korean; now
    the narrative follows the task locale, so on an English task both lists are
    English and _same_advice COULD pair them. The comparison still stays off:

      1. the side that would lose is the evidence-bound one. This keeps the
         FIRST occurrence, and `recommendations` are pushed before
         `suggested_action`, so extended across lists the per-candidate action
         is what gets dropped, and a signal with no next step is worse than a
         repeated bullet. That argument never depended on the language, and
         after the measurements below it is the load-bearing reason on its own.
      2. the reader is not shown a bare repeat anyway. The UI renders the
         model's advice and each candidate's action with their source and
         category attached (rca-report-model.nextSteps), so a near-duplicate
         reads as "the same advice, and here is the signal demanding it".
      3. it would NOT rarely fire, which is why ground 1 has to carry the
         decision. _advice_tokens is set-based, so word order does not survive:
         measured on collector-vs-model text where only a parenthetical is
         reordered ("Investigate what drove cpu_utilization up around this
         window (load change, plan regression, runaway query)." against the
         same line listing the three causes in another order), _same_advice
         returns True at 1.000 overlap. Turning the comparison on would delete
         real per-candidate actions readily, not occasionally.
      4. the cheap guard is weaker than it looks. nextSteps drops an EXACT text
         match across both lists, and one character defeats it: measured, the
         model's "Check the current plan with EXPLAIN in Query Lab." and the
         collector's same sentence without the full stop are not equal and both
         render, though _same_advice would have paired them at 1.000. So a
         near-duplicate does reach the reader. That is the accepted cost of
         ground 1, not a free win.
    """
    recs = res.get("recommendations")
    if not isinstance(recs, list) or not recs:
        return 0
    kept = []
    for rec in recs:
        text = str(rec)
        # Compared against what was KEPT, so a third restatement of a line that
        # was already dropped is still measured against the one line that stays.
        if any(_same_advice(text, other) for other in kept):
            continue
        kept.append(text)
    res["recommendations"] = kept
    return len(recs) - len(kept)


def _run_rca(cluster_id: str, observed_at: str = "", locale: str = ""):
    """Deterministic RCA via the incident diagnose_root_cause tool, with a
    hybrid narrative + recommendations layered on in the task's own language
    (best-effort LLM).

    `locale` is the requester's console language, carried on the task row by the
    /tasks POST for a manual run and absent for an automated one; _task_locale
    resolves the deployment default for that case and fails safe to Korean.

    `observed_at` is when the incident was OBSERVED, carried on the task row by the
    producer that enqueued it. It matters because it is not this moment: the RCA used
    to anchor on task-execution time, and alert_evaluator reads a 10-minute lookback
    on a 5-minute poll, so the breach is routinely 10-15 minutes older. Measured on
    the 2026-08-30 auto-RCA: the CPU breach was at 19:54:00 and 19:56:00, the anchor
    landed at 19:59:30, and CPU was already back to 49.6 by 19:59:00. The analysis
    described the recovery. Empty falls back to now, which is right for a manual RCA
    where the DBA is looking at the present.

    Returns (result_dict, one_line_summary, steps). The summary is the
    top-ranked candidate's own summary line, so the toast / list reads
    meaningfully without the DBA opening the full result. steps is a list of
    trace dicts recording each tool invocation with timing."""
    steps = []
    lang = _task_locale(locale)
    t = time.time()
    res = diagnose_root_cause_impl(_get_cache(), cluster_id, around_time=observed_at or "")
    cands = res.get("candidates", []) if isinstance(res, dict) else []
    examined = res.get("signals_examined", {}) if isinstance(res, dict) else {}
    nsrc = len([k for k, v in examined.items() if v]) if isinstance(examined, dict) else 0
    steps.append({"step": "진단", "tool": "diagnose_root_cause",
                  "ms": int((time.time() - t) * 1000),
                  "detail": (f"{nsrc}개 소스 검사, 후보 {len(cands)}"
                             + (f", 앵커 {observed_at}" if observed_at
                                else ", 앵커 현재시각"))})
    if isinstance(res, dict):
        t = time.time()
        narr = _narrative(cluster_id, res, lang)
        if narr:
            res.update(narr)  # adds narrative + recommendations + narrative_locale
            dropped = _dedupe_advice(res)
            steps.append({"step": "서술 생성", "tool": "bedrock",
                          "ms": int((time.time() - t) * 1000),
                          # States the language it actually generated in, so
                          # "한국어 ..." on an English report would be a false
                          # claim in the UI. rca-report.tsx renders it through
                          # t(), so a ko label read on an en console is
                          # translated while the FACT it states (the prose is
                          # Korean) survives: see the en-server.ts key.
                          "detail": (_LANG[lang]["trace"]
                                     + (_LANG[lang]["dropped"].replace("{n}", str(dropped))
                                        if dropped else ""))})
        else:
            steps.append({"step": "서술 생성", "tool": "bedrock", "ms": 0,
                          "detail": "모델 미설정/실패, 스킵"})
    summary = (cands[0].get("summary") or cands[0].get("category") or "신호 감지") if cands \
        else "자동 수집 신호에서 뚜렷한 원인 미발견, 수동 점검 권장"
    return res, summary, steps


def _run_report(cluster_id: str):
    """Recurring health digest (scheduled_report). Reuses the incident
    health_status tool and normalizes it into display `lines` so the UI renders
    reports generically without coupling to health_status internals.

    health_status returns `health` as an overall string (healthy/warning/
    critical), cluster meta, and `current_metrics` (per-metric avg/max over the
    last 10 min). The digest surfaces all three.

    Returns (report_dict, one_line_summary, steps)."""
    steps = []
    t = time.time()
    res = get_health_status_impl(_get_cache(), cluster_id)
    lines = []
    health = res.get("health") if isinstance(res, dict) else None
    if health:
        lines.append({"label": "헬스", "value": str(health)})
    cluster = (res.get("cluster") if isinstance(res, dict) else None) or {}
    if isinstance(cluster, dict):
        for k in ("status", "engine", "engine_version"):
            if cluster.get(k):
                lines.append({"label": k, "value": str(cluster[k])})
    for m in (res.get("current_metrics") if isinstance(res, dict) else None) or []:
        if isinstance(m, dict) and m.get("metric_type") is not None:
            lines.append({
                "label": str(m["metric_type"]),
                "value": f"avg {m.get('avg_val')} / max {m.get('max_val')}",
            })
    summary = f"헬스 다이제스트: {health}" if health else "헬스 다이제스트"
    report = {"report_kind": "health_digest", "lines": lines, "raw": res}
    steps.append({"step": "헬스 다이제스트", "tool": "health_status",
                  "ms": int((time.time() - t) * 1000),
                  "detail": f"헬스 {health}, 메트릭 {len(lines)}개"})
    return report, summary, steps


def lambda_handler(event, context):
    processed = 0
    skipped = 0
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        img = _deser_image(rec.get("dynamodb", {}).get("NewImage", {}))
        task_id = img.get("task_id")
        kind = img.get("kind")
        cluster_id = img.get("cluster_id")
        status = img.get("status")
        if not task_id or status != "pending":
            skipped += 1
            continue
        if not _claim(task_id):
            skipped += 1  # someone else is handling it
            continue

        t0 = time.time()
        try:
            if kind in ("auto_rca", "manual_rca"):
                result, summary, steps = _run_rca(
                    cluster_id,
                    observed_at=str(img.get("observed_at") or ""),
                    # Absent on every automated row: see _task_locale.
                    locale=str(img.get("locale") or ""),
                )
            elif kind == "scheduled_report":
                result, summary, steps = _run_report(cluster_id)
            else:
                # Any future kind lands here until its generator ships. Fail
                # loudly so it shows in the task list instead of hanging at
                # "running" forever.
                raise NotImplementedError(f"task kind {kind!r} not yet supported by the worker")

            # Ticketing seam: inert by default (provider "none" → None). When a
            # provider is wired and returns a URL, persist it on the task and
            # surface it in the push so the toast/list can link to the ticket.
            ticket_url = _maybe_create_ticket(task_id, cluster_id, kind, summary, result)
            _finish(task_id, status="done", result=result, summary=summary,
                    ticket_url=ticket_url, trace=steps,
                    duration_ms=int((time.time() - t0) * 1000))
            is_report = kind == "scheduled_report"
            payload = {
                "type": "task",
                "task_kind": "report_ready" if is_report else "rca_ready",
                "task_id": task_id,
                "cluster_id": cluster_id,
                "severity": "warning",
                "title": f"{'리포트' if is_report else 'RCA'} 준비됨: {cluster_id}, {summary}",
            }
            if ticket_url:
                payload["ticket_url"] = ticket_url
            _broadcast(payload)
            processed += 1
        except Exception as e:
            print(f"[task-worker] task {task_id} ({kind}) failed: {type(e).__name__}: {e}")
            try:
                _finish(
                    task_id,
                    status="failed",
                    summary=f"작업 실패: {type(e).__name__}",
                    # `error` is persisted on the task row and rendered verbatim
                    # in the Tasks UI (app/tasks/page.tsx), so it must be STATIC:
                    # a Data API / boto exception here carries the cache cluster
                    # ARN, the secret ARN and SQL. `summary` already carries the
                    # exception CLASS, which is the failure distinction the DBA
                    # needs; the full detail is in the print above (CloudWatch).
                    error="작업 실행 중 오류가 발생했습니다. 자세한 원인은 서버 로그(CloudWatch)를 확인하세요.",
                    duration_ms=int((time.time() - t0) * 1000),
                )
            except Exception as e2:
                print(f"[task-worker] could not mark {task_id} failed: {type(e2).__name__}")

    return {"processed": processed, "skipped": skipped}

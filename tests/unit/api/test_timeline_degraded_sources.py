"""A timeline signal stream that could not be READ must not read as "it did not
fire".

`_timeline` names a failed source in `degraded_sources`, and the schema_change
stream reads `schema_snapshots`, which arrives in schema_v26: on a cache DB that
has not run the migrator the query raises and the category is simply ABSENT from
the response. An absent category on a timeline reads as "no DDL happened during
the incident", which is precisely the defect the field was added for.

It was added and then consumed NOWHERE. `grep -rn degraded_sources frontend/src`
returned zero hits, including the TimelineResponse type, so the operator saw
exactly what the original bug produced. A fix that lands in a payload nobody
renders is not a fix, so this module guards BOTH ends:

  server   `_timeline` is driven with a failing schema_snapshots read and the
           field is asserted, along with the absence of any exception text.
  page     frontend/src/app/timeline/page.tsx is parsed: the field is read, the
           banner is rendered from it, and the "nothing happened" empty state is
           replaced by a "we could not look" one when a stream is degraded.
  type     TimelineResponse must declare it, or `data.degraded_sources` is not
           reachable from TypeScript at all.

No JS runtime in CI, so the page is parsed structurally (the idiom of
tests/unit/test_anomalies_panel_empty_state.py and
test_capacity_panel_family_table.py) rather than grepped: a substring can be
satisfied by a comment or a dead const, a brace-balanced JSX slice cannot.

MUTATION-CHECKED (break, observe, restore from a file backup):
  * delete the `{degraded.length > 0 && (...)}` banner
      -> test_the_page_renders_a_banner_naming_the_unread_streams FAILED
  * revert the EmptyState title/description to their unconditional strings
      -> test_the_empty_state_stops_claiming_nothing_happened FAILED
  * drop `degraded_sources` from TimelineResponse
      -> test_the_response_type_declares_the_field FAILED
  * remove `degraded.append("schema_change")` from the handler
      -> test_a_failed_schema_snapshots_read_is_named FAILED

THE OTHER HALF, added by the SEVENTH pass. `degraded_sources` covers a read that
FAILED. It cannot express a read that SUCCEEDED over a cluster whose schemas nobody
can currently confirm: the schema_change stream replays stored diffs, an
unconfirmable schema files none, and the category goes quiet with nothing failing.
`_timeline` is the FIFTH interpreter of schema_snapshots, it answers the same
question diagnose_root_cause does, and the sixth pass swapped its SQL to the shared
ALL_ROWS fragment while giving it no observation channel at all, which is the
identical shape to the fifth pass leaving the panel out. Both ends are guarded
below, and the mechanical guard that CAUGHT it (per-function, not per-file) lives in
tests/unit/data_pipeline/test_schema_snapshot_parity.py.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_DASH = _ROOT / "api" / "dashboard"
_PAGE = (_ROOT / "frontend/src/app/timeline/page.tsx").read_text()
_CLIENT = (_ROOT / "frontend/src/lib/api-client.ts").read_text()

sys.path.insert(0, str(_DASH))
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
_spec = importlib.util.spec_from_file_location("dashboard_handler_degraded", _DASH / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

_LEAK = (
    'relation "schema_snapshots" does not exist; secret '
    "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbops-cache-AbCdEf"
)


_SCOPE = "appdb/16401"
_CONFIRMED_AT = "2026-07-27 00:02:00+00"


def _obs_row(name="app", *, confirmed=True, holds="y", scope=_SCOPE):
    return {"schema_name": name, "read_scope": scope or "",
            "last_seen": _CONFIRMED_AT if confirmed else "2026-07-01 00:00:00+00",
            "holds_tables": holds,
            "age_sec": 60 if confirmed else 40 * 24 * 3600}


def _query(fail_on=(), obs=None, scope=_SCOPE, engine="aurora-postgresql"):
    """event_log / audit_log answer normally; anything named in `fail_on` raises.

    `obs` is the row set the SHARED observation probe reads. It defaults to one
    confirmed schema, so a test that is not about the observation gets a cluster the
    stream fully covered rather than a silently blind one.
    """
    obs_rows = [_obs_row()] if obs is None else list(obs)

    def query(sql, params=None):
        for name in fail_on:
            if name in sql:
                raise RuntimeError(_LEAK)
        # ORDER MATTERS: the observation statements name schema_snapshots too, so
        # they are claimed before anything generic.
        if "FROM cluster_meta" in sql:
            return [] if engine is None else [{"engine": engine}]
        if "read_scope IS NOT NULL" in sql:
            return [] if scope is None else [{"read_scope": scope}]
        if "holds_tables" in sql:
            return obs_rows
        if "event_log" in sql:
            return [{"id": 1, "event_time": "2026-07-27 00:00:00+00",
                     "event_type": "alert", "severity": "warning", "source": "cw",
                     "message": "CPU high", "raw_event": None}]
        return []
    return query


# ===========================================================================
# server
# ===========================================================================

def test_a_failed_schema_snapshots_read_is_named():
    got = handler._timeline(_query(fail_on=("schema_snapshots",)), "c1", 24, None)
    assert got["degraded_sources"] == ["schema_change"], got
    # The category is absent from `categories`, which is exactly why the naming
    # has to exist: absence alone is indistinguishable from "did not happen".
    assert "schema_change" not in got["categories"], got["categories"]


def test_a_healthy_read_names_nothing():
    """Mutation guard: a field that is always non-empty would banner every
    timeline and be ignored within a week."""
    got = handler._timeline(_query(), "c1", 24, None)
    assert got["degraded_sources"] == []


def test_both_optional_streams_can_degrade_together():
    got = handler._timeline(_query(fail_on=("schema_snapshots", "audit_log")),
                            "c1", 24, None)
    assert sorted(got["degraded_sources"]) == ["audit", "schema_change"]


def test_no_exception_text_reaches_the_payload():
    got = handler._timeline(_query(fail_on=("schema_snapshots",)), "c1", 24, None)
    blob = json.dumps(got, default=str)
    for leaked in ("secretsmanager", "does not exist", "RuntimeError", "arn:aws"):
        assert leaked not in blob, leaked


# ===========================================================================
# the page, parsed
# ===========================================================================

def _balanced(src: str, i: int, opener: str = "(", closer: str = ")") -> str:
    """The balanced `opener...closer` slice starting at index i, which must be on
    the opener. Brace-balanced rather than indentation-sliced so the guard
    survives a reformat."""
    assert src[i] == opener, src[i:i + 40]
    depth, j = 0, i
    while j < len(src):
        if src[j] == opener:
            depth += 1
        elif src[j] == closer:
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("unbalanced")


def _flat(s: str) -> str:
    return " ".join(s.split())


def test_the_page_reads_the_field_at_all():
    assert "data?.degraded_sources ?? []" in _PAGE, (
        "the page does not read degraded_sources, so a stream that could not be "
        "read is invisible and the timeline reads as 'nothing happened'"
    )


def test_the_page_renders_a_banner_naming_the_unread_streams():
    i = _PAGE.index("{degraded.length > 0 && (")
    banner = _flat(_balanced(_PAGE, _PAGE.index("(", i + 1)))
    # It names WHICH streams...
    assert "{degraded.join(\", \")}" in banner, banner
    # ...and says what their absence does and does not mean.
    assert "발생하지 않았다는 뜻이 아니라" in banner
    assert "확인하지 못했다는 뜻입니다" in banner
    # The actionable hint for the one stream that has a known cause, and only for
    # that stream: audit_log ships in the base schema, so schema_v26 is not its
    # explanation.
    assert 'degraded.includes("schema_change")' in banner
    assert "schema_v26" in banner


def test_the_banner_is_outside_the_empty_state():
    """It has to render when items EXIST too: that is the dangerous case, because
    a populated timeline gives the operator no reason to suspect a whole stream is
    missing."""
    banner_at = _PAGE.index("{degraded.length > 0 && (")
    empty_at = _PAGE.index("<EmptyState")
    list_at = _PAGE.index("<TimelineList items={visibleItems} />")
    assert banner_at < empty_at < list_at, (banner_at, empty_at, list_at)


def _attr(name: str) -> str:
    """The value of an `attr={...}` on the EmptyState element."""
    start = _PAGE.index("<EmptyState")
    i = _PAGE.index(f"{name}={{", start) + len(name) + 1
    return _flat(_balanced(_PAGE, i, "{", "}"))


def test_the_empty_state_stops_claiming_nothing_happened():
    """With a stream unread OR a schema nobody could confirm, "이 cluster에서 ...
    없습니다" is a claim about signals nobody looked at. Both are `blind`, and the
    empty state is keyed to that one name so the next kind of blindness is a
    one-line change instead of a second forgotten condition."""
    title, desc = _attr("title"), _attr("description")
    assert "blind" in title, title
    assert "blind" in desc, desc
    # The absence sentence must be confined to the arm where nothing was blind.
    absence = "이 cluster에서 최근 발생한"
    assert absence in desc
    head, _, tail = desc.partition("blind")
    assert absence not in head, (
        "the unconditional absence claim is still reachable with a blind stream"
    )
    assert "확인하지 못했습니다" in tail
    # The two arms are different sentences, not the same one twice.
    assert desc.count(absence) == 1


# ===========================================================================
# the OTHER blindness: the read succeeded and still could not cover the question
# ===========================================================================

def test_the_timeline_carries_the_shared_observation_block():
    """The FIFTH interpreter gets the SAME channel as the other four. Without it an
    empty schema_change category is indistinguishable from "no DDL happened", which
    is the defect `degraded_sources` was added for in the case where nothing
    failed."""
    got = handler._timeline(_query(), "c1", 24, None)
    obs = got["observation"]
    assert obs["status"] == "fresh", obs
    assert obs["read_scope"] == _SCOPE, obs
    assert obs["unconfirmed_schemas"] == [], obs
    # A cluster the stream fully covered gets NO sentence, or the banner fires on
    # every timeline and is ignored within a week.
    assert obs["note"] == "", obs


def test_a_schema_nobody_can_confirm_is_named_on_the_timeline():
    got = handler._timeline(
        _query(obs=[_obs_row("app"), _obs_row("gone_s", confirmed=False)]),
        "c1", 24, None)
    obs = got["observation"]
    assert obs["status"] == "not_seen", obs
    assert obs["unconfirmed_schemas"] == ["gone_s"], obs
    # named, dated, and never as a drop: the accepted cost of this surface, in the
    # same words the other four consumers use.
    assert "gone_s" in obs["note"]
    assert "삭제로 단정하지 않고" in obs["note"]
    for drop_word in ("삭제됨", "dropped"):
        assert drop_word not in obs["note"], drop_word
    # and it does NOT masquerade as a failed read
    assert got["degraded_sources"] == []


def test_a_cluster_with_no_snapshot_history_says_the_stream_had_no_capability():
    """`not_seen_note` names schemas, and there are none here, so it says nothing.
    The stream still had no detection capability, and on a timeline that is the
    difference between "no DDL during the incident" and "we have no DDL data"."""
    got = handler._timeline(_query(obs=[], scope=None), "c1", 24, None)
    assert got["observation"]["status"] == "no_snapshots"
    assert "DDL이 없었다는 뜻이 아닙니다" in got["observation"]["note"]


def test_the_observation_note_never_carries_exception_text():
    got = handler._timeline(_query(fail_on=("schema_snapshots",)), "c1", 24, None)
    assert got["observation"]["status"] == "unavailable"
    blob = json.dumps(got, default=str)
    for leaked in ("secretsmanager", "does not exist", "RuntimeError", "arn:aws"):
        assert leaked not in blob, leaked


def test_the_page_reads_and_renders_the_observation_note():
    assert 'data?.observation?.note ?? ""' in _PAGE, (
        "the page does not read observation.note, so a schema nobody can confirm is "
        "invisible and an empty schema_change category reads as 'no DDL happened'"
    )
    i = _PAGE.index("{schemaNote && (")
    banner = _flat(_balanced(_PAGE, _PAGE.index("(", i + 1)))
    assert "{schemaNote}" in banner, banner
    assert "unconfirmed_schemas" in banner, banner
    assert "schema_change" in banner, banner


def test_the_page_renders_the_item_title_and_detail_verbatim():
    """WHY THIS IS A GUARD AND NOT A TAUTOLOGY. On a refused dialect the server
    labels each replayed DDL item IN `title` and `detail` (handler
    `_TL_DDL_UNSOUND_TAG`), precisely because a timeline item is rendered standalone
    in a category-filtered list where the cluster-level banner is nowhere near it.
    That only reaches the operator while the page renders those two fields as they
    arrive; a future edit deriving the title from `category` + a parsed summary would
    drop the qualification and put `dropped 1` back on the screen unqualified.
    """
    flat = _flat(_PAGE)
    assert "{item.title}" in flat, flat[:200]
    assert "{item.detail}" in flat
    # and the label the server sends is a plain string in those fields, so nothing
    # in the page needs to know the reason to show it.
    assert "_TL_DDL_UNSOUND" not in flat, (
        "the page must not reimplement the label; it is server-composed so the five "
        "consumers cannot describe one state five ways")


def test_the_observation_banner_is_outside_the_empty_state_too():
    """Same reason as the degraded banner: a POPULATED timeline is exactly where a
    missing DDL event is invisible."""
    assert (_PAGE.index("{schemaNote && (") < _PAGE.index("<EmptyState")
            < _PAGE.index("<TimelineList items={visibleItems} />"))


def test_blind_is_both_conditions_and_not_just_one():
    """MUTATION-CHECKED: with `blind` defined as `degraded.length > 0` alone, the
    successful-but-blind cluster falls back into the "아무것도 없습니다" arm, which is
    the exact defect this pass is closing one layer down."""
    assert 'const blind = degraded.length > 0 || schemaNote !== "";' in _flat(_PAGE), (
        "the empty state's blindness test is not the disjunction of BOTH channels, "
        "so one kind of blindness falls back into the 'nothing happened' arm")


def test_the_response_type_declares_the_observation_block():
    i = _CLIENT.index("export interface TimelineResponse {")
    body = _CLIENT[i:_CLIENT.index("\n}\n", i)]
    assert "observation?: {" in body, body
    for field in ("note?: string", "unconfirmed_schemas?: string[]"):
        assert field in body, (field, body)


def test_the_response_type_declares_the_field():
    """Optional, because a static export can meet an api Lambda older than the
    field. Not declared at all means `data.degraded_sources` does not compile."""
    i = _CLIENT.index("export interface TimelineResponse {")
    body = _CLIENT[i:_CLIENT.index("\n}", i)]
    assert "degraded_sources?: string[]" in body, body

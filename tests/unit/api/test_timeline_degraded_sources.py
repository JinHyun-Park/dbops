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


def _query(fail_on=()):
    """event_log / audit_log answer normally; anything named in `fail_on` raises."""
    def query(sql, params=None):
        for name in fail_on:
            if name in sql:
                raise RuntimeError(_LEAK)
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
    """With a stream unread, "이 cluster에서 ... 없습니다" is a claim about signals
    nobody looked at."""
    title, desc = _attr("title"), _attr("description")
    assert "degraded.length > 0" in title, title
    assert "degraded.length > 0" in desc, desc
    # The absence sentence must be confined to the arm where nothing degraded.
    absence = "이 cluster에서 최근 발생한"
    assert absence in desc
    head, _, tail = desc.partition("degraded.length > 0")
    assert absence not in head, (
        "the unconditional absence claim is still reachable with a degraded stream"
    )
    assert "확인하지 못했습니다" in tail
    # The two arms are different sentences, not the same one twice.
    assert desc.count(absence) == 1


def test_the_response_type_declares_the_field():
    """Optional, because a static export can meet an api Lambda older than the
    field. Not declared at all means `data.degraded_sources` does not compile."""
    i = _CLIENT.index("export interface TimelineResponse {")
    body = _CLIENT[i:_CLIENT.index("\n}", i)]
    assert "degraded_sources?: string[]" in body, body

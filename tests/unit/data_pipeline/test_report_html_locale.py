"""The downloadable HTML report's SHELL follows the language of the prose in it.

Since the summary started following DEFAULT_LOCALE, an en deployment produced a
document whose English prose sat in hardcoded Korean chrome under
``<html lang="ko">``: a lang attribute that misinforms screen readers and
translation tooling, and 26 Korean fragments around an English paragraph.

Asserted in BOTH DIRECTIONS on purpose. A one-direction test ("the Korean
document says 클러스터") passes unchanged on the hardcoded Korean shell this
change exists to remove, so it proves nothing: the two documents have to
actually DIFFER. The third assertion is the one that catches a fragment somebody
forgot, by feeding ASCII-only data and demanding the English document contain no
Hangul at all.

DBA-facing jargon is deliberately NOT translated (AAS avg, AAS max, the Δ),
which is this project's standing rule, so it is pinned as jargon rather than
allowed to drift into prose.
"""

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_DIR = Path(__file__).resolve().parents[3] / "data-pipeline" / "report_generator"
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

_spec = importlib.util.spec_from_file_location("report_html_locale", _DIR / "report_html.py")
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

_HANGUL = re.compile(r"[가-힣]")

# ASCII-only data, so any Hangul left in the English document is CHROME.
_DATA = {
    "aas": {"avg_aas": 1.2, "max_aas": 4.5},
    "aas_series": [
        {"ts": "2026-09-17T00:00:00Z", "value": 1.0},
        {"ts": "2026-09-17T01:00:00Z", "value": 4.5},
    ],
    "top_slow_queries": [
        {"query_hash": "abc", "query_excerpt": "SELECT 1", "calls": 2, "total_ms": 30.0},
    ],
    "top_alerts": [{"rule_id": "high-cpu", "fired_count": 3, "last_fired": "2026-09-17T01:00:00Z"}],
    "connections": {"max_conn": 45},
}

# health values here are ASCII on purpose: in production they are the Korean
# "주의"/"정상" written by handler._fleet_row, which are stored DATA, not chrome,
# and are out of scope for a translation table.
_FLEET = {
    "clusters_total": 2,
    "engine_counts": {"aurora-postgresql": 2},
    # The health values the PRODUCER actually writes. The fixture used to say
    # "warn"/"ok", which handler._generate_fleet_rollup never emits (it writes
    # "주의" if there are alerts else "정상", and keys the distribution by the
    # same strings), so nothing here exercised the health column at all.
    "health_distribution": {"주의": 1, "정상": 1},
    "totals": {"alerts": 3, "slow_queries": 1},
    "worst_clusters": [{"cluster_id": "pg-1", "engine": "aurora-postgresql", "health": "주의",
                        "aas_avg": 1.0, "aas_max": 4.0, "alert_count": 3,
                        "slow_query_count": 1, "storage_delta_bytes": 1024}],
    # TWO rows, one per health value, so the health COLUMN is exercised for
    # both. With a single 주의 row, dropping the "정상" entry from _EN left the
    # no-Korean assertion green: the fixture did not fill the slot.
    "clusters": [{"cluster_id": "pg-1", "engine": "aurora-postgresql", "health": "주의",
                  "aas_avg": 1.0, "aas_max": 4.0, "alert_count": 3,
                  "slow_query_count": 1, "storage_delta_bytes": 1024},
                 {"cluster_id": "pg-2", "engine": "aurora-postgresql", "health": "정상",
                  "aas_avg": 0.2, "aas_max": 0.9, "alert_count": 0,
                  "slow_query_count": 0, "storage_delta_bytes": 0}],
}


def _cluster(loc, summary="Nothing notable happened."):
    return rh.build_report_html("pg-1", "2026-09-17", "daily", summary, _DATA, loc)


def _fleet(loc, summary="Nothing notable happened."):
    return rh.build_fleet_report_html("2026-09-17", "daily", summary, _FLEET, loc)


def test_lang_attribute_and_headers_differ_between_the_two_locales():
    """The two-direction assertion. Equality here would mean the shell is still
    hardcoded, whichever language it is hardcoded in.

    lang= follows the locale, so both halves of each pair move together with the
    chrome. The separate question of what language the PROSE is in is pinned
    below, and is deliberately not this attribute's job."""
    ko, en = _cluster("ko", "특이사항 없습니다."), _cluster("en")
    assert '<html lang="ko">' in ko and '<html lang="en">' not in ko
    assert '<html lang="en">' in en and '<html lang="ko">' not in en
    # The alerts table header, which is the per-cluster report's header row.
    assert "<th>규칙</th>" in ko and "<th>Rule</th>" not in ko
    assert "<th>Rule</th>" in en and "<th>규칙</th>" not in en
    assert ko != en

    fko, fen = _fleet("ko", "특이사항 없습니다."), _fleet("en")
    assert '<html lang="ko">' in fko and '<html lang="en">' in fen
    # The fleet table header, quoted in full in both languages.
    assert ("<tr><th>클러스터</th><th>엔진</th><th>상태</th><th>AAS avg</th>"
            "<th>AAS max</th><th>경보</th><th>슬로우</th><th>스토리지 Δ</th></tr>") in fko
    assert ("<tr><th>Cluster</th><th>Engine</th><th>Status</th><th>AAS avg</th>"
            "<th>AAS max</th><th>Alerts</th><th>Slow</th><th>Storage Δ</th></tr>") in fen
    assert fko != fen


def test_the_english_document_has_no_korean_chrome_left():
    """With ASCII-only data and an ASCII summary, ANY Hangul in the output is a
    fragment that was missed. This is the assertion that scales: it fails on the
    next hardcoded Korean string somebody adds to the shell."""
    for html in (_cluster("en"), _fleet("en")):
        left = sorted(set(_HANGUL.findall(html)))
        assert not left, f"untranslated chrome, Hangul left: {''.join(left)}"


def test_empty_state_placeholders_follow_the_locale_too():
    """The placeholders are chrome as much as the headings are, and they are the
    ones reached on a quiet day, i.e. most days."""
    ko = rh.build_report_html("pg-1", "2026-09-17", "daily", "", {}, "ko")
    en = rh.build_report_html("pg-1", "2026-09-17", "daily", "", {}, "en")
    assert "데이터 없음" in ko and "발생한 알림 없음" in ko
    assert "No data" in en and "No alerts fired" in en
    assert not _HANGUL.search(en)
    fen = rh.build_fleet_report_html("2026-09-17", "daily", "", {}, "en")
    assert "No data" in fen and "No engine information" in fen
    assert not _HANGUL.search(fen)


def test_dba_jargon_stays_english_in_korean_too():
    """Standing rule: a DBA reads these as identifiers, not as prose."""
    for html in (_cluster("ko"), _fleet("ko")):
        assert "AAS" in html
    assert "AAS avg" in _fleet("ko") and "AAS max" in _fleet("ko")
    assert "Δ" in _fleet("ko") and "Δ" in _fleet("en")


def test_an_unrecognised_locale_reads_korean():
    """Same fail-closed allowlist as handler._report_locale: Korean is what
    every deployment ships, and the lang attribute must not carry a caller
    string it cannot vouch for.

    The summary is Korean here because that is what an unrecognised locale
    actually produces: _report_locale hands the generator "ko" as well."""
    for loc in ("", None, "EN", "en-US", "fr", 7):
        html = _cluster(loc, "특이사항 없습니다.")
        assert '<html lang="ko">' in html, loc
        assert "<th>규칙</th>" in html, loc
        # and with no prose to read, the fallback is still the locale
        assert '<html lang="ko">' in _cluster(loc, ""), loc


# ---------------------------------------------------------------------------
# lang= describes the PROSE, not the request.
#
# `locale` is the language the summary was ASKED for. The summary is model
# output, and a model can disobey the English directive, in which case a lang=
# taken from the locale states something the document contradicts on its first
# line, to exactly the screen readers and translation tools that cannot check.
# Measured before the fix: build_report_html(..., "이 클러스터는 안정적입니다.",
# ..., "en") emitted <html lang="en"> around Korean prose.
# ---------------------------------------------------------------------------


def test_lang_follows_the_locale_and_not_the_prose():
    """MEASURED 2026-09-17, and the reason the script-test version was reverted.

    An English summary routinely contains Hangul, because _build_summary_prompt
    feeds the model up to 120 characters of raw query_excerpt plus the Korean
    section labels. So a summary that merely quotes a customer's SQL literal
    scored "ko" while the document was English. Both of these were wrong under
    the script test and are right here.
    """
    quotes_korean_data = (
        "Activity stayed inside the usual range. The heaviest statement was "
        "SELECT * FROM orders WHERE status = '배송중' ORDER BY created_at DESC."
    )
    html = _cluster("en", quotes_korean_data)
    assert '<html lang="en">' in html
    assert '<html lang="ko">' not in html
    assert "<th>Rule</th>" in html

    # The other direction, and the stronger argument: lang= scopes the WHOLE
    # document, and everything but one <div> is chrome the locale authored. A
    # ko document holding an English paragraph is still a Korean document.
    ko = _cluster("ko", "The cluster was stable for 24 hours.")
    assert '<html lang="ko">' in ko
    assert "<th>규칙</th>" in ko


def test_the_prose_language_is_not_this_attributes_job():
    """The discriminating pin against reintroducing the script test: for one
    locale, the attribute must be IDENTICAL across summaries in either script.

    Where the prose's own language IS answered is the frontend, which labels the
    summary from its own text against the console locale, at the one place a
    reader sees the two side by side (narrative-language.summaryLanguage).
    """
    for loc, want in (("en", 'lang="en"'), ("ko", 'lang="ko"')):
        seen = {
            rh._lang(loc, summary)
            for summary in (
                "",
                "   ",
                None,
                "The cluster was stable.",
                "이 클러스터는 안정적이었습니다.",
                "Stable. 상태 정상.",
            )
        }
        assert len(seen) == 1, f"{loc}: the summary moved the attribute: {seen}"
        assert want in _cluster(loc, "Stable. 상태 정상.")


# ---------------------------------------------------------------------------
# D3: `locale` stays a required positional in both builders.
#
# Verified by mutation: giving it a default of "ko" left all 17 tests across
# test_report_html.py, this file and test_report_generator_fleet.py passing. A
# default is precisely the failure mode, because a future caller that forgets
# the argument gets a silently Korean document on an English deployment and
# nothing anywhere raises: both HTML puts in the handler sit inside
# `except Exception: print(...)`.
# ---------------------------------------------------------------------------


def test_locale_is_a_required_positional_in_both_builders():
    for fn in (rh.build_report_html, rh.build_fleet_report_html):
        params = inspect.signature(fn).parameters
        assert "locale" in params, fn.__name__
        assert params["locale"].default is inspect.Parameter.empty, (
            f"{fn.__name__}(locale=...) must have NO default: a caller that omits "
            "it would ship a Korean document on an en deployment, and the "
            "handler swallows the exception that would otherwise show it"
        )


# ---------------------------------------------------------------------------
# The handler actually forwards it. Without this, a signature mismatch would be
# swallowed: both HTML puts sit inside `except Exception: print(...)`, so the
# document would just silently stop existing.
# ---------------------------------------------------------------------------


def test_handler_forwards_the_generation_locale_into_both_documents(monkeypatch):
    handler_path = _DIR / "handler.py"
    hspec = importlib.util.spec_from_file_location("report_generator_handler_locale", handler_path)
    handler = importlib.util.module_from_spec(hspec)
    hspec.loader.exec_module(handler)
    # Other tests in this suite leave a MagicMock at sys.modules["report_html"];
    # the handler imports it lazily, so pin the REAL module for this run.
    sys.modules["report_html"] = rh

    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:us-east-1:123:cluster:t")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:t")
    monkeypatch.setenv("ARCHIVE_BUCKET", "bucket")

    mock_rds = MagicMock()

    def _execute(**kwargs):
        if "cluster_meta" in kwargs.get("sql", ""):
            return {"columnMetadata": [{"name": "cluster_id"}, {"name": "engine"}],
                    "records": [[{"stringValue": "pg-1"}, {"stringValue": "aurora-postgresql"}]]}
        return {"columnMetadata": [], "records": []}

    mock_rds.execute_statement.side_effect = _execute
    mock_s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b'{"content":[{"text":"Nothing notable happened."}]}'
    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model.return_value = {"body": body}

    def _client(service, **kwargs):
        return {"s3": mock_s3, "rds-data": mock_rds, "bedrock-runtime": mock_bedrock}.get(
            service, MagicMock()
        )

    with patch.object(handler, "boto3") as mock_boto3, \
         patch.object(handler, "get_config", return_value="en"):
        mock_boto3.client.side_effect = _client
        assert handler.lambda_handler({}, {})["statusCode"] == 200

    htmls = [
        c.kwargs["Body"] for c in mock_s3.put_object.call_args_list
        if c.kwargs.get("Key", "").endswith(".html")
    ]
    assert len(htmls) == 2, [c.kwargs.get("Key") for c in mock_s3.put_object.call_args_list]
    for doc in htmls:
        assert '<html lang="en">' in doc, doc[:200]

def test_an_english_document_contains_no_korean_at_all():
    """The end state, asserted as an absence so a new hardcoded string fails it.

    This is what caught the last gap: the health column's VALUES are a closed
    set the collectors write, so they are not chrome and were left out of _EN,
    and an English fleet document contained exactly two Hangul words, both of
    them health cells.
    """
    en = _fleet("en", "No notable events.")
    leftover = sorted(set(re.findall(r"[가-힣]+", en)))
    assert leftover == [], f"Korean left in an English fleet report: {leftover}"

    en_cluster = _cluster("en", "Activity stayed inside the usual range.")
    leftover = sorted(set(re.findall(r"[가-힣]+", en_cluster)))
    assert leftover == [], f"Korean left in an English report: {leftover}"

    # The mirror, so the assertion above cannot be satisfied by a shell that
    # lost its Korean in both locales.
    ko = _fleet("ko", "특이사항 없습니다.")
    assert re.search(r"[가-힣]", ko), "the Korean report must still be Korean"

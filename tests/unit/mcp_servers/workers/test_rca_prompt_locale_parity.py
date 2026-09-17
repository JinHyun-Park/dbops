"""The RCA prompt differs between ko and en in its LANGUAGE DIRECTIVE only.

data-pipeline's report prompt already has this guard
(tests/unit/data_pipeline/test_report_summary_locale.py). The RCA prompt, which
is the one that actually diverged, had neither it nor a pinned directive, and
that is how the divergence shipped: the English "analyze" entry grew a "and
what to check" clause, while the shared JSON-format line asks `narrative` for
"가장 가능성 높은 근본 원인을 2-3문장으로" and leaves next steps to
`recommendations`. Same incident, two differently shaped reports depending on
who requested it.

TWO ASSERTIONS, ON PURPOSE, because either one alone has a blind spot:

  * the PARITY test substitutes _LANG[loc][...] out of both prompts, so a change
    to the DIRECTIVE'S OWN TEXT substitutes identically on both sides and the
    comparison still passes. The report test's own docstring records this: a
    retuned directive passed 15/15 there, which is exactly the hole the clause
    above came through.
  * the PINNED text catches that, but says nothing about the rest of the prompt,
    where a language switch could smuggle in a reworded body.
"""

from unittest.mock import MagicMock, patch

import mcp_servers.workers.task_worker as tw
import pytest

_RCA = {
    "candidates": [
        {"category": "metric_spike", "summary": "cpu_utilization 급증",
         "score": 3.0, "when": "2026-09-17T03:00:00Z"},
        {"category": "schema_change", "summary": "orders 컬럼 변경",
         "score": 1.5, "when": "2026-09-17T02:50:00Z"},
    ],
    "signals_examined": {"metrics": 12, "events": 3},
}

# Non-empty on purpose: the history section is shared Korean text, so it has to
# be inside the byte-comparison rather than short-circuited to "".
_HISTORY = "과거 효과 이력(조치 성공/시도): param_change 3/4, index_add 1/2"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RCA_NARRATIVE_MODEL_ID", "model-x")


def _prompts(lang):
    """(user prompt, system prompt) for one locale, read off the real call."""
    bedrock = MagicMock()
    bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": '{"narrative":"n"}'}]}}
    }
    with patch.object(tw.boto3, "client", return_value=bedrock), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "_history_line", return_value=_HISTORY):
        tw._narrative("pg-prod-1", _RCA, lang)
    kwargs = bedrock.converse.call_args.kwargs
    return kwargs["messages"][0]["content"][0]["text"], kwargs["system"][0]["text"]


def test_only_the_directive_differs_between_the_two_languages():
    ko_user, ko_sys = _prompts("ko")
    en_user, en_sys = _prompts("en")

    ko_user = ko_user.replace(tw._LANG["ko"]["analyze"], "<<DIRECTIVE>>")
    en_user = en_user.replace(tw._LANG["en"]["analyze"], "<<DIRECTIVE>>")
    assert ko_user == en_user, (
        "the analysis prompt differs beyond its output-language directive:\n"
        f"ko: {ko_user!r}\nen: {en_user!r}"
    )

    ko_sys = ko_sys.replace(tw._LANG["ko"]["answer"], "<<DIRECTIVE>>")
    en_sys = en_sys.replace(tw._LANG["en"]["answer"], "<<DIRECTIVE>>")
    assert ko_sys == en_sys, (
        "the system prompt differs beyond its answer-language directive:\n"
        f"ko: {ko_sys!r}\nen: {en_sys!r}"
    )


def test_the_directive_text_itself_is_pinned():
    """The blind spot in the test above. Retuning a directive has to be a
    deliberate act that updates this test.

    The English entries are LONGER than the Korean ones, and only for reasons
    that compensate for something the shared Korean prompt body carries
    implicitly: the hedge (a model told to write English does not inherit the
    restraint of "가장 가능성 높은") and the verbatim-identifier rule (an English
    writer will happily render db_connections as "database connections", which
    an operator cannot grep for). A clause that asks for extra CONTENT does not
    belong here, and that is what this pin exists to stop.
    """
    assert tw._LANG["ko"]["analyze"] == "한국어로 분석하세요. "
    assert tw._LANG["en"]["analyze"] == (
        "Write the analysis in English. Keep the hedging the Korean asks "
        "for: state what these signals make LIKELY, never assert a "
        "confirmed cause. Keep metric names, parameter names, SQL and "
        "cluster IDs verbatim. "
    )
    assert tw._LANG["ko"]["answer"] == "항상 한국어로 답합니다."
    assert tw._LANG["en"]["answer"] == (
        "Always answers in English, and hedges any cause the signals only "
        "suggest rather than establish."
    )
    assert set(tw._LANG) == set(tw._LOCALES)
    assert set(tw._LANG["ko"]) == set(tw._LANG["en"])


def test_the_english_directive_asks_for_no_extra_content():
    """Stated as its own failure, because it is the one the pin above is FOR.

    `narrative` is specified once, in shared text, as
    "가장 가능성 높은 근본 원인을 2-3문장으로". What to do next is the
    `recommendations` field's job. An English directive that also asks for it
    folds next steps into the narrative a Korean reader keeps out of it.
    """
    en = tw._LANG["en"]["analyze"]
    assert "what to check" not in en
    # The hedge and the identifier rule are what the length IS for.
    assert "LIKELY" in en and "never assert a confirmed cause" in en
    assert "verbatim" in en


def test_the_korean_prompt_body_is_shared_and_stays_korean():
    """The model READS these; they are not the answer. Translating them would be
    retuning the prompt, which the locale switch deliberately does not do."""
    for lang in ("ko", "en"):
        user, system = _prompts(lang)
        assert "근본원인 분석 후보 신호입니다" in user, lang
        assert "검사한 신호 수" in user, lang
        assert _HISTORY in user, lang
        assert '{"narrative": "가장 가능성 높은 근본 원인을 2-3문장으로"' in user, lang
        assert "Aurora/RDS 데이터베이스 운영(DBA) 전문가" in system, lang

"""Guard: the RCA report must LABEL a narrative written in the other language,
and must only say so when it is actually true.

WHY THIS IS A TEST AND NOT A COMMENT. The RCA inbox is fleet-wide, so two
operators whose consoles are set to different languages open the SAME stored
report, and the narrative is model prose frozen in the language that generated
it: it is not an i18n key, so no render site can translate it. The only honest
move is to tell the reader what they are looking at. Two ways to get that
wrong, and both are silent:

  1. the label never renders, and a Korean report lands in an English console
     with nothing saying why;
  2. the label renders when the languages MATCH, which puts a false statement
     on a report surface whose one governing rule is to never look more certain
     than the system is.

`narrativeLanguage()` itself is covered by `node tools/rca-report-check.mjs`
(pure, runnable under bare node). The GUARD lives in rca-report.tsx, which no
runner in this repo can execute: there is no JS unit runner, the Playwright
smoke needs a deployed URL plus a stored report in each language, and tsc
cannot see a flipped comparison. So it is pinned here, the same way
test_responsive_display_pattern.py and the panel-state tests pin frontend
source they cannot run.
"""

from __future__ import annotations

from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
_REPORT = _FRONTEND / "src" / "components" / "rca" / "rca-report.tsx"
_MESSAGES = _FRONTEND / "src" / "lib" / "messages" / "en.ts"

# The label's Korean source string, which is also its en.ts key.
_LABEL_KEY = (
    "이 서술과 권장 조치는 {n}로 생성되었습니다. 요청한 운영자의 콘솔 언어로 "
    "생성되며(자동 작업은 배포 기본 언어), 저장된 문장은 번역하지 않습니다."
)


def test_label_is_gated_on_a_real_mismatch():
    """The guard compares the stored narrative's language against the console's,
    and holds off until the console locale is actually resolved.

    `ready` matters because LocaleProvider starts every render at "ko" (this is
    a static export, so the prerendered HTML is Korean) and resolves in an
    effect. Without it, an English console reading an English report would flash
    "generated in English" on the first paint.
    """
    src = _REPORT.read_text()
    assert (
        "const otherLanguage = ready && narrLang !== null && narrLang !== locale;"
        in src
    ), "the mismatch guard changed shape; re-read it before trusting this file"
    # Rendered under that guard, and nowhere else.
    assert src.count("otherLanguage &&") == 1, src.count("otherLanguage &&")
    assert src.count(_LABEL_KEY) == 1


def test_label_names_the_language_from_the_narrative_not_the_console():
    """The language NAME in the sentence has to come from the narrative, or the
    label would tell an English reader the Korean prose is English."""
    src = _REPORT.read_text()
    assert 'narrLang === "ko" ? "한국어" : "영어"' in src, (
        "the label must derive its language name from narrLang; deriving it "
        "from `locale` would name the reader's language, not the prose's"
    )


def test_label_and_both_language_names_are_translated():
    """All three strings are FIXED literals, so the en.ts table covers them.

    A missing entry does not throw and does not fail the build: translate()
    falls back to the Korean key, so an English operator would read the
    explanation of the language mismatch in the language they just said they
    could not read.
    """
    messages = _MESSAGES.read_text()
    assert f'"{_LABEL_KEY}":' in messages
    # Korean is a valid JS identifier, so these two are written BARE.
    assert "\n  한국어: " in messages
    assert "\n  영어: " in messages

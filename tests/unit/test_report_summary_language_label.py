"""Guard: the operations report must LABEL a summary written in the other
language, and must only say so when it is actually true.

WHY THIS EXISTS. The Reports screen is FLEET-WIDE, so two operators whose
consoles are set to different languages open the SAME stored row, and the
summary is model prose frozen at generation time by DEFAULT_LOCALE: it is not
an i18n key, so no render site can translate it (translate() would miss and
hand back the input). The only honest move is to tell the reader what they are
looking at. The identical problem was solved for the RCA narrative one commit
earlier and not carried over to the surface the next commit created, which is
why the carry-over is pinned rather than trusted.

Two ways to get it wrong, both silent:

  1. no label, and an English summary lands in a Korean console with nothing
     saying why;
  2. the label renders when the languages MATCH, which puts a false statement
     on the screen.

Structure mirrors test_rca_narrative_language_label.py, with one addition:
`summaryLanguage()` has no runner of its own (tools/rca-report-check.mjs covers
`narrativeLanguage`, and that file is the RCA check), so its BEHAVIOUR is
exercised here by running it under node, which loads the .ts module directly by
type stripping. The render-site guard is pinned as source, the way this repo
pins frontend it cannot execute.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
_MODULE = _FRONTEND / "src" / "lib" / "narrative-language.ts"
_VIEWER = _FRONTEND / "src" / "components" / "reports" / "report-viewer.tsx"
_MESSAGES = _FRONTEND / "src" / "lib" / "messages" / "en.ts"

# The label's Korean source string, which is also its en.ts key.
#
# "쓰도록 요청하며", not "항상 따르며": the wording deliberately states what the
# generator is ASKED to do rather than an invariant about the result. A model
# handed the English directive can answer in Korean anyway, and a template
# fallback can too, so "always follows the deployment default" was a claim the
# code could contradict. The language NAME in front of the reader is the only
# part measured from the prose, and it stays.
_LABEL_KEY = (
    "이 요약은 {n}로 생성되었습니다. 리포트는 예약 실행이라 요청한 운영자가 없어 "
    "배포 기본 언어로 쓰도록 요청하며, 저장된 문장은 번역하지 않습니다."
)

_CASES = [
    # (summary, expected label)
    ("CPU 사용률이 평소 범위를 벗어났습니다. 슬로우 쿼리는 없었습니다.", "ko"),
    ("CPU utilization stayed inside its usual range. No slow queries.", "en"),
    # Identifiers are ASCII and stay verbatim, so an English summary full of
    # them is still English.
    ("db_connections peaked at 45 and work_mem was unchanged on pg-prod-1.", "en"),
    ("", None),
    ("   \n\t ", None),
    (None, None),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_summary_language_labels_each_script_and_claims_nothing_when_empty():
    """Behaviour, not source: run the real function.

    Empty and whitespace must return null rather than "en", or a report with no
    summary at all would get a label announcing the language of nothing.
    """
    # json.dumps of a list is also a valid JS array literal, so the cases go in
    # as data with no quoting to get wrong.
    script = (
        "import { summaryLanguage } from './src/lib/narrative-language.ts';\n"
        "const cases = " + json.dumps([c[0] for c in _CASES]) + ";\n"
        "console.log(JSON.stringify(cases.map((c) => summaryLanguage(c))));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=_FRONTEND, capture_output=True, text=True, check=True,
    )
    assert json.loads(out.stdout.strip()) == [c[1] for c in _CASES], out.stdout


def test_the_label_is_gated_on_a_real_mismatch():
    """`ready` matters: LocaleProvider starts every render at "ko" (static
    export, so the prerendered HTML is Korean) and resolves in an effect.
    Without it a Korean console reading a Korean summary would flash the label
    on the first paint."""
    src = _VIEWER.read_text()
    assert (
        "const otherLanguage = ready && sumLang !== null && sumLang !== locale;"
        in src
    ), "the mismatch guard changed shape; re-read it before trusting this file"
    # Rendered under that guard, and nowhere else.
    assert src.count("otherLanguage &&") == 1, src.count("otherLanguage &&")
    assert src.count(_LABEL_KEY) == 1


def test_the_label_names_the_language_of_the_summary_not_the_console():
    """Deriving the name from `locale` would tell an English reader that the
    Korean prose in front of them is English."""
    src = _VIEWER.read_text()
    assert 'sumLang === "ko" ? "한국어" : "영어"' in src


def test_the_label_and_both_language_names_are_translated():
    """A missing entry does not throw and does not fail the build: translate()
    falls back to the Korean key, so an English operator would read the
    explanation of the language mismatch in the language they just said they
    could not read."""
    messages = _MESSAGES.read_text()
    assert f'"{_LABEL_KEY}":' in messages
    # Korean is a valid JS identifier, so these two are written BARE.
    assert "\n  한국어: " in messages
    assert "\n  영어: " in messages


def test_the_report_wording_is_not_the_rca_wording():
    """The two sentences must not converge. They explain DIFFERENT causes: an
    RCA follows the requesting operator's console (deployment default only when
    nobody asked), while this report is written by a SCHEDULE, so there is no
    requesting operator and the deployment default is what the generator is
    asked for. Copying the RCA sentence here would describe a requesting
    operator who does not exist.

    Everything below is asserted against the SOURCE. Three assertions used to
    sit here comparing _LABEL_KEY with rca_key and with its own substrings,
    both module constants defined at the top of this file: they read as
    coverage and could not fail for any state of the tree.
    """
    rca_key = (
        "이 서술과 권장 조치는 {n}로 생성되었습니다. 요청한 운영자의 콘솔 언어로 "
        "생성되며(자동 작업은 배포 기본 언어), 저장된 문장은 번역하지 않습니다."
    )
    src = _VIEWER.read_text()
    assert rca_key not in src
    # The key this file names is the one the component actually renders. If the
    # label is reworded in the source and not here, every expectation in this
    # file is about a string that no longer exists, and this is where that
    # shows up.
    assert _LABEL_KEY in src, "report-viewer.tsx label drifted from _LABEL_KEY"
    assert f'"{_LABEL_KEY}":' in _MESSAGES.read_text(), "no en.ts value"
    # The three facts the label owes the reader, read off the rendered source:
    # why it can differ from the console (scheduled, so no requester, so the
    # deployment default is what is asked for), and that stored prose is never
    # translated afterwards. The language NAME is covered by the test above.
    for fact in ("예약 실행", "배포 기본 언어", "번역하지 않습니다"):
        assert fact in src, fact


def test_the_summary_has_no_stamp_to_read_and_that_is_recorded():
    """summaryLanguage is a SCRIPT TEST with a known ceiling, and the reason it
    is not a stamp (the reports table has no locale column, and schema_version
    is a SHA-256 over the whole migration directory) has to survive in the
    source, or the next reader re-derives it."""
    src = _MODULE.read_text()
    assert "export function summaryLanguage(" in src
    ceiling = src.split("export function summaryLanguage(")[0]
    assert "ponytail:" in ceiling.split("export function narrativeLanguage(")[-1]
    assert "schema_version" in ceiling

import sys

from . import _oe_loader as _ldr

_PATH_ADDED = _ldr.install_path()

def _load(mod_name, file_name=None):
    return _ldr.load(mod_name, file_name)

remediation_classify = _load("remediation_classify")
classify_action = remediation_classify.classify_action


def teardown_module(module):
    _ldr.teardown(_PATH_ADDED, "remediation_classify")


def test_index_and_param_and_scale():
    assert classify_action("Query Lab에서 EXPLAIN으로 플랜 확인, 필요하면 인덱스 점검") == "index_add"
    assert classify_action("work_mem 및 max_connections 파라미터를 조정하세요") == "param_change"
    assert classify_action("ACU 상한을 올려 스케일 업하세요") == "scale_up"


def test_vacuum_analyze_and_default():
    assert classify_action("autovacuum/VACUUM 점검 권장") == "vacuum"
    assert classify_action("통계가 오래됨, ANALYZE 실행") == "analyze"
    assert classify_action("원인 불명, 수동 점검 필요") == "manual"
    assert classify_action("") == "manual"


# ---------------------------------------------------------------------------
# Language parity. case_opener feeds this the MODEL's recommendations[0], and
# that prose now follows the task locale (task_worker._task_locale), so the same
# real action arrives in Korean on one deployment and English on another. A
# family with only a Korean needle forks the (cluster, symptom, action_class)
# accumulator behind the success-rate badges and the prompt injection into two
# buckets, halving the evidence behind both.
#
# Measured before the fix: "오래된 통계를 갱신하세요" -> analyze,
# "Refresh the stale table statistics" -> manual. The identifier needles
# (work_mem, max_connection) survive the crossing because the English prompt
# directive asks for parameter names verbatim; 통계 is a word, so nothing
# protected it.
# ---------------------------------------------------------------------------

_BOTH_LANGUAGES = [
    ("index_add", "orders 테이블에 인덱스를 추가하세요",
     "Add a composite index on orders(created_at)"),
    ("vacuum", "autovacuum 설정을 조정하고 VACUUM을 실행하세요",
     "Run VACUUM on the bloated table"),
    ("analyze", "오래된 통계를 갱신하세요",
     "Refresh the stale table statistics"),
    ("analyze", "테이블 통계를 다시 수집하세요",
     "Collect the table statistics again"),
    ("param_change", "work_mem 파라미터를 올리세요",
     "Raise the work_mem parameter"),
    ("param_change", "max_connections 값을 조정하세요",
     "Tune max_connections"),
    ("scale_up", "ACU 상한을 올려 스케일 업하세요",
     "Scale up the writer instance"),
    ("manual", "원인 불명, 수동 점검이 필요합니다",
     "Investigate this by hand"),
]


def test_every_family_classifies_the_same_in_korean_and_english():
    for expected, ko, en in _BOTH_LANGUAGES:
        assert classify_action(ko) == expected, ko
        assert classify_action(en) == expected, en


def test_the_statistics_family_no_longer_forks_by_language():
    """The one that was actually broken, pinned on its own so a regression names
    itself instead of hiding inside the loop above."""
    assert classify_action("Refresh the stale table statistics") == "analyze"
    assert classify_action("Update the optimizer statistic for this table") == "analyze"


def test_the_more_specific_families_still_win_over_the_new_needle():
    """Order contract: the FIRST matching family wins, most specific first. The
    English statistics needle sits in the analyze row, after index and vacuum,
    so a line naming both still classifies as the more specific action."""
    assert classify_action("Rebuild the index and refresh its statistics") == "index_add"
    assert classify_action("Run VACUUM ANALYZE to refresh statistics") == "vacuum"


def test_the_statistics_needle_does_not_steal_a_planner_parameter_change():
    """The families ordered BEFORE analyze (index, vacuum) were already covered
    above; param_change is ordered AFTER it, so "statistic" as a bare substring
    swallowed `default_statistics_target`, which is a parameter, not an ANALYZE.

    Measured with the real function, "statistic" needle present, identifier
    needle absent:
      'Raise default_statistics_target to 500'                -> analyze
      'default_statistics_target 파라미터를 500으로 올리세요'   -> analyze
    """
    assert classify_action("Raise default_statistics_target to 500") == "param_change"
    assert classify_action(
        "Set default_statistics_target = 500 on this cluster"
    ) == "param_change"


def test_the_korean_planner_parameter_line_is_a_regression_pin():
    """The one that actually CHANGED behaviour rather than merely staying wrong:
    this line classified as param_change before the "statistic" needle shipped
    (via the 파라미터 needle) and as analyze after it. Pinned on its own so a
    reordering of _RULES names this case instead of burying it in a loop."""
    assert classify_action(
        "default_statistics_target 파라미터를 500으로 올리세요"
    ) == "param_change"
    # The control from the same measurement: an identifier with no "statistic"
    # substring was never affected, and must stay unaffected.
    assert classify_action("work_mem 파라미터를 올리세요") == "param_change"

def test_a_planner_parameter_named_without_its_identifier_is_still_a_param_change():
    """MEASURED 2026-09-17. `default_statistics_target` alone was not enough:
    "Increase the statistics target parameter" and the Korean "통계 목표를 500으로
    올리세요" name the same parameter without spelling the identifier, and both
    classified as `analyze` because the statistics needle is checked first.

    Raising a planner parameter is a parameter change, not an ANALYZE run. They
    have different outcomes, so sharing an accumulator key would pool the
    success rate of two different actions.
    """
    for text in (
        "Increase the statistics target parameter",
        "통계 목표를 500으로 올리세요",
        "Raise default_statistics_target to 500",
        "default_statistics_target 파라미터를 500으로 올리세요",
    ):
        assert classify_action(text) == "param_change", text

    # And the statistics family still wins when no parameter is named, which is
    # the language fork these needles were added for in the first place.
    for text in (
        "Refresh the stale table statistics",
        "오래된 통계를 갱신하세요",
        "테이블 통계를 다시 수집하세요",
        "Collect the table statistics again",
    ):
        assert classify_action(text) == "analyze", text

"""Map a free-text recommendation (or RCA category) to a normalized action_class.

Pure + deterministic so the same (symptom, action) key is produced wherever a case
is opened. Order matters: the FIRST matching family wins, most-specific first.
"""

# (substring, action_class): checked in order; Korean + English keywords.
_RULES = [
    ("인덱스", "index_add"), ("index", "index_add"),
    ("vacuum", "vacuum"), ("배큠", "vacuum"), ("autovacuum", "vacuum"),
    # "statistic" covers both "statistics" and "statistic". The needle is not
    # redundant with "통계": the recommendation text this classifies is model
    # prose that now follows the task locale, so the same real action arrives as
    # "오래된 통계를 갱신하세요" on a ko task and "Refresh the stale table
    # statistics" on an en task. Without the English needle the second one fell
    # through to "manual", and the (cluster, symptom, action_class) accumulator
    # behind the success-rate badges split into two buckets on a mixed-locale
    # deployment. The identifier needles (work_mem, max_connection) are safe
    # because the prompt asks for parameter names verbatim; 통계 is a word, so
    # nothing protected it.
    # A param_change needle placed BEFORE the analyze row on purpose, using the
    # order contract at the top of this file: the "statistic" needle above is a
    # substring of the identifier `default_statistics_target`, so "Raise
    # default_statistics_target to 500" classified as analyze, and its Korean
    # twin "default_statistics_target 파라미터를 500으로 올리세요" changed from
    # param_change to analyze when that needle landed. Raising a planner
    # parameter is a parameter change, not an ANALYZE run: they are different
    # actions with different outcomes, so they must not share an accumulator key.
    ("default_statistics_target", "param_change"),
    # Same reason, one step more general: "Increase the statistics target
    # parameter" and "통계 목표를 500으로 올리세요" name the same parameter
    # without spelling the identifier, and measured as analyze before this.
    ("statistics target", "param_change"), ("통계 목표", "param_change"),
    ("analyze", "analyze"), ("통계", "analyze"), ("statistic", "analyze"),
    ("work_mem", "param_change"), ("max_connection", "param_change"),
    ("파라미터", "param_change"), ("parameter", "param_change"),
    ("스케일", "scale_up"), ("scal", "scale_up"), ("acu", "scale_up"),
]


def classify_action(text: str, category: str = "") -> str:
    hay = f"{text or ''} {category or ''}".lower()
    for needle, action in _RULES:
        if needle in hay:
            return action
    return "manual"

"""Map a free-text recommendation (or RCA category) to a normalized action_class.

Pure + deterministic so the same (symptom, action) key is produced wherever a case
is opened. Order matters: the FIRST matching family wins, most-specific first.
"""

# (substring, action_class) — checked in order; Korean + English keywords.
_RULES = [
    ("인덱스", "index_add"), ("index", "index_add"),
    ("vacuum", "vacuum"), ("배큠", "vacuum"), ("autovacuum", "vacuum"),
    ("analyze", "analyze"), ("통계", "analyze"),
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

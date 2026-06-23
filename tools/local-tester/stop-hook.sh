#!/usr/bin/env bash
# Claude Code Stop hook (registered as a 2nd Stop entry in .claude/settings.json).
# If the background post-commit tester found issues on the CURRENT HEAD commit,
# block the stop ONCE and surface the findings so the dev agent fixes them
# before finishing. The dev agent fixes (this hook never edits anything).
#
# No-op (allow stop, exit 0) when: no findings file, findings are for a
# different/older commit (stale), status != issues, or already surfaced for
# this commit. → Safe in repos where the tester was never installed.
set -uo pipefail

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
F="$REPO/.omc/tester-findings.md"
[ -f "$F" ] || exit 0
HEAD="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || exit 0

FSHA="$(grep -m1 '^commit:' "$F" | awk '{print $2}')"
FSTATUS="$(grep -m1 '^status:' "$F" | awk '{print $2}')"

# Only act on issues for the CURRENT commit (ignore stale/pending findings).
[ "$FSHA" = "$HEAD" ] || exit 0
[ "$FSTATUS" = "issues" ] || exit 0

# Surface once per commit so the agent isn't hard-trapped (e.g. on a false
# positive). After a fix-commit, the new HEAD won't match FSHA → allow.
MARK="$REPO/.omc/.tester-surfaced"
[ "$(cat "$MARK" 2>/dev/null)" = "$HEAD" ] && exit 0
echo "$HEAD" > "$MARK"

BODY="$(sed -n '/^# /,$p' "$F" | head -c 6000)"
python3 - "$BODY" <<'PY'
import json, sys
body = sys.argv[1]
print(json.dumps({
    "decision": "block",
    "reason": (
        "⚠️ 백그라운드 테스터(post-commit, Codex 적대 리뷰 + 유닛 + dev 스모크)가 "
        "현재 커밋에서 문제를 발견했습니다. 마무리 전에 아래를 검토하고, 실제 결함이면 "
        "수정한 뒤 다시 커밋하세요(테스터가 새 커밋을 재검증합니다). 거짓 양성이면 무시하고 "
        "진행해도 됩니다.\n\n" + body
    ),
}))
PY
exit 0

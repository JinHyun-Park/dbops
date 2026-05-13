#!/usr/bin/env bash
# Stop hook: fires when the assistant is about to stop responding. We use
# this to surface a reminder when the most recent commit count vs. the
# previous "checkpoint" suggests a real cycle just finished — at that
# point the assistant should persist a note to project_memory so the
# next session can resume cleanly.
#
# We deliberately keep this very quiet: most stops are mid-cycle and
# don't need any action. Only when:
#   • There are new commits on origin/main since the last checkpoint, OR
#   • The .omc/state has marker files implying a major mode just ended,
# do we emit a suggestion.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
[ -z "$REPO_ROOT" ] && exit 0

CHECKPOINT="$REPO_ROOT/.claude/.last-memory-checkpoint"
HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
LAST_SHA=""
[ -f "$CHECKPOINT" ] && LAST_SHA="$(cat "$CHECKPOINT" 2>/dev/null || true)"

# No git or no progress → nothing to say.
[ -z "$HEAD_SHA" ] && exit 0
[ "$HEAD_SHA" = "$LAST_SHA" ] && exit 0

# Count commits since last checkpoint (capped at 50 to keep the reminder
# brief). If LAST_SHA is empty/invalid, fall back to "last 5".
NEW_COMMITS=""
if [ -n "$LAST_SHA" ] && git -C "$REPO_ROOT" cat-file -e "$LAST_SHA^{commit}" 2>/dev/null; then
  NEW_COMMITS="$(git -C "$REPO_ROOT" log --oneline "$LAST_SHA..HEAD" 2>/dev/null | head -50)"
else
  NEW_COMMITS="$(git -C "$REPO_ROOT" log --oneline -5 2>/dev/null)"
fi

[ -z "$NEW_COMMITS" ] && exit 0

cat <<EOF
[SESSION-END CHECKPOINT] Commits since the last memory checkpoint:

$NEW_COMMITS

If a coherent cycle just finished, persist a note before stopping so the
next session can resume without re-deriving context:

  mcp__plugin_oh-my-claudecode_t__project_memory_add_note({
    note: "<one short paragraph: what shipped, what's next, any caveats>"
  })

After writing the note, update the checkpoint so this reminder doesn't
re-fire on the same commits:

  printf '%s' "$HEAD_SHA" > .claude/.last-memory-checkpoint

Skip entirely (no cycle, just trivial tweaks): \`touch .claude/.last-memory-checkpoint\`
to mark the current HEAD as already-checkpointed without persisting a note.
EOF

exit 0

#!/usr/bin/env bash
# Pre-tool-use hook: emits a system-reminder when the assistant is about to
# run `git commit ...`, instructing it to spawn the code-reviewer subagent
# first. This adds a structural review pass before any change reaches
# origin — counters the self-review blind spot of single-agent loops.
#
# Bypass: include the substring [skip-review] in the commit message or
# touch .claude/.skip-review (single-shot, auto-removed after use).
set -euo pipefail

# Claude Code passes the tool input JSON on stdin for PreToolUse hooks.
# We only care about Bash commands that begin with `git commit`.
INPUT="$(cat || true)"

# Extract the command field with a tolerant grep — avoids requiring jq.
CMD="$(echo "$INPUT" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"command"[[:space:]]*:[[:space:]]*"//; s/"$//')"

case "$CMD" in
  *"git commit"*)
    # One-shot bypass flag wins so iterative re-commits during a verified
    # cycle don't trigger N reviews.
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$SCRIPT_DIR/.skip-review" ]; then
      rm -f "$SCRIPT_DIR/.skip-review"
      exit 0
    fi
    if echo "$CMD" | grep -q '\[skip-review\]'; then
      exit 0
    fi
    cat <<'EOF'
[CODE REVIEW GATE] `git commit` blocked — run the code-reviewer subagent first.

Steps for the assistant:
  1. Spawn the reviewer:

       Agent({ subagent_type: "oh-my-claudecode:code-reviewer",
               description: "Pre-commit review",
               prompt: "Review the staged changes for this commit. Focus on logic defects, security, and any obvious regression risk. Reply with PASS or BLOCK + reasoning." })

  2. If PASS: re-run the commit with one of these bypass paths:
       • Add `[skip-review]` to the commit message subject line, OR
       • Run `touch .claude/hooks/.skip-review` before the commit (one-shot flag).

  3. If BLOCK: address the findings, re-stage, then go to step 1 again.

  Docs-only / trivial fixes can skip review by including `[skip-review]`
  in the commit message — the reviewer is for logic-bearing changes.
EOF
    ;;
esac

exit 0

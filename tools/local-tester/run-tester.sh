#!/usr/bin/env bash
# Background post-commit tester (cross-model adversarial review + tests + smoke).
#
# Dev agent = Claude (the main Claude Code session). This tester runs the
# ADVERSARIAL review with a DIFFERENT model — Codex (openai.gpt-5.5, Bedrock) —
# plus the unit suite and an optional live dev smoke, then writes findings the
# Stop hook surfaces back to the dev agent. READ-ONLY: it never edits the tree
# or commits — the dev agent fixes (author/reviewer separation).
#
# Invoked DETACHED by .git/hooks/post-commit so it never blocks the dev loop.
# Findings: .omc/tester-findings.md  (a `commit:`/`status:` header + details).
set -uo pipefail   # intentionally NOT -e: run every step and record failures

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO" || exit 0
mkdir -p .omc
OUT=".omc/tester-findings.md"
RANGE="${1:-HEAD~1..HEAD}"
SHA="$(git rev-parse HEAD 2>/dev/null)"
SHORT="$(git rev-parse --short HEAD 2>/dev/null)"
SUBJECT="$(git log -1 --format='%s' 2>/dev/null)"
CODEX_MODEL="${DBOPS_TESTER_CODEX_MODEL:-openai.gpt-5.5}"

# --- single-flight: a newer commit's tester wins (kill the previous run) -----
PIDF=".omc/.tester.pid"
[ -f "$PIDF" ] && kill "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null
echo $$ > "$PIDF"

# --- skip trivial commits (docs/scratch/markdown only) to save Codex cost ----
# NOTE: tools/local-tester/*.sh is intentionally NOT skipped — changes to the
# gate's own logic are high-stakes (a broken gate silently passes everything),
# so the tester adversarially reviews its own script changes too.
CHANGED="$(git diff --name-only "$RANGE" 2>/dev/null)"
if [ -z "$CHANGED" ] || ! printf '%s\n' "$CHANGED" | grep -qvE '^(docs/|\.superpowers/|\.omc/|.*\.md$)'; then
  printf 'commit: %s\nstatus: clean\n\n_사소한 커밋(docs/scratch) — 테스터 스킵._\n' "$SHA" > "$OUT"
  rm -f "$PIDF"; exit 0
fi

printf 'commit: %s\nstatus: pending\n\n_테스터 실행 중: %s (%s)…_\n' "$SHA" "$SHORT" "$SUBJECT" > "$OUT"

DIFF="$(git diff "$RANGE" 2>/dev/null | head -c 60000)"

# --- 1) Codex adversarial review (cross-model) -------------------------------
PROMPT="You are an adversarial code reviewer. Review ONLY the git diff below (commit ${SHORT}: ${SUBJECT}). Surface real Critical/Important issues introduced by THIS diff — bugs, regressions, security holes, broken non-breaking invariants, missing error handling. Be concise: one bullet per issue as '- [SEV] path: what / why'. If you find no blocking issues, say so briefly. Your VERY LAST line MUST be exactly 'VERDICT: CLEAN' or 'VERDICT: ISSUES'.

DIFF:
${DIFF}"
# Codex prints: separator, the ECHOED PROMPT (under a 'user' line), a line
# 'codex', the model response, 'tokens used', N. The echoed prompt itself
# contains the literal "VERDICT:" (from our instruction), so verdict detection
# MUST run on the awk-EXTRACTED model response only — never on the raw output,
# which would false-match the echo on an auth failure that exits 0.
_extract() { printf '%s\n' "$1" | awk '/^codex$/{f=1;next} /^tokens used$/{f=0} f'; }
_has_verdict() { printf '%s' "$1" | grep -q 'VERDICT:'; }

# Config 1 (default): toolbox `codex`, AWS-internal creds. Primary path.
CODEX_RAW="$(codex exec -m "$CODEX_MODEL" -s read-only "$PROMPT" 2>&1)"; CODEX_RC=$?
CODEX_VIA="toolbox(${CODEX_MODEL})"
REVIEW="$(_extract "$CODEX_RAW")"
# Fallback → config 2 (personal Bedrock key): if config 1 produced no real model
# verdict (e.g. AWS-internal creds expired → "failed to load AWS credentials"),
# retry via the isolated CODEX_HOME + standalone binary — replicates the
# `codex-key` zsh function (~/.codex-key/credentials.env + ~/.local/bin/codex).
# Uses that config's own model (do NOT pass the toolbox -m, an internal model).
# Gate on REVIEW (extracted), not CODEX_RAW (echoes the prompt's "VERDICT:").
if { [ "$CODEX_RC" -ne 0 ] || ! _has_verdict "$REVIEW"; } \
   && [ -x "$HOME/.local/bin/codex" ] && [ -f "$HOME/.codex-key/credentials.env" ]; then
  CODEX_RAW="$( set -a; . "$HOME/.codex-key/credentials.env" 2>/dev/null; set +a
                export CODEX_HOME="$HOME/.codex-key"
                "$HOME/.local/bin/codex" exec -s read-only "$PROMPT" 2>&1 )"; CODEX_RC=$?
  CODEX_VIA="bedrock-key(fallback)"
  REVIEW="$(_extract "$CODEX_RAW")"
fi
CODEX_REVIEW="$REVIEW"
[ -z "${CODEX_REVIEW// }" ] && CODEX_REVIEW="$CODEX_RAW"   # display fallback only
# Require an explicit VERDICT line IN THE EXTRACTED RESPONSE: its absence (or a
# non-zero exit) after BOTH configs means the review did NOT actually run ->
# 'error' (surfaced, not a silent 'clean'). On 'error' the dev agent runs the
# Opus adversarial fallback (see global memory: codex-adversarial-review-fallback).
if [ "$CODEX_RC" -ne 0 ] || ! _has_verdict "$REVIEW"; then
  CODEX_ST=error
elif printf '%s' "$REVIEW" | grep -q 'VERDICT: ISSUES'; then
  CODEX_ST=issues
else
  CODEX_ST=clean
fi

# --- 2) Unit tests -----------------------------------------------------------
TEST_RAW="$(python3 -m pytest tests/unit -q 2>&1)"; TEST_RC=$?
TEST_TAIL="$(printf '%s\n' "$TEST_RAW" | tail -1)"
# Use pytest's EXIT CODE, not output text: 0 = all passed; any non-zero (test
# failures, usage error, no tests collected, interrupted, pytest missing) is
# NOT a pass — text-grepping for 'failed|error' missed those cases.
[ "$TEST_RC" -eq 0 ] && TEST_ST=pass || TEST_ST=fail

# --- 3) Optional live dev smoke (pluggable, best-effort) ---------------------
SMOKE_OUT="(스킵 — tools/local-tester/dev-smoke.sh 없음/비실행)"; SMOKE_ST=skip
if [ -x tools/local-tester/dev-smoke.sh ]; then
  SMOKE_OUT="$(tools/local-tester/dev-smoke.sh 2>&1 | tail -20)"
  if printf '%s' "$SMOKE_OUT" | grep -q 'SMOKE: FAIL'; then SMOKE_ST=fail
  elif printf '%s' "$SMOKE_OUT" | grep -q 'SMOKE: PASS'; then SMOKE_ST=pass; else SMOKE_ST=skip; fi
fi

# --- aggregate + write findings ---------------------------------------------
STATUS=clean
{ [ "$CODEX_ST" = issues ] || [ "$CODEX_ST" = error ]; } && STATUS=issues
[ "$TEST_ST" = fail ] && STATUS=issues
[ "$SMOKE_ST" = fail ] && STATUS=issues

{
  echo "commit: $SHA"
  echo "status: $STATUS"
  echo ""
  echo "# 백그라운드 테스터 — ${SHORT} (${SUBJECT})"
  echo "- Codex 적대 리뷰: **${CODEX_ST}** (via ${CODEX_VIA})"
  echo "- 유닛 테스트(tests/unit): **${TEST_ST}** — ${TEST_TAIL}"
  echo "- dev 스모크: **${SMOKE_ST}**"
  if [ "$CODEX_ST" = error ]; then
    echo ""
    echo "> ⚠️ Codex 두 설정(toolbox + bedrock-key) 모두 실패 — 교차모델 적대 리뷰가 실행되지 않음."
    echo "> **폴백:** dev 에이전트가 별도 Opus 서브에이전트로 이 diff의 적대적 리뷰를 수행할 것."
    echo "> (기본 codex 복구: \`aws sso login\` 또는 mwinit; 폴백 상세는 전역 메모리 codex-adversarial-review-fallback.)"
  fi
  echo ""
  echo "## Codex 적대 리뷰"
  printf '%s\n' "$CODEX_REVIEW"
  echo ""
  echo "## 유닛 테스트 (tail)"
  printf '%s\n' "$TEST_RAW" | tail -6
  echo ""
  echo "## dev 스모크"
  printf '%s\n' "$SMOKE_OUT"
} > "$OUT"

rm -f "$PIDF"
exit 0

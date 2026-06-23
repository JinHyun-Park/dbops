# Local background tester (post-commit adversarial review + tests + smoke)

A local, **GitHub-Actions-free** feedback loop: after each commit a detached
tester runs a **cross-model adversarial review** (Codex `openai.gpt-5.5`, since
the dev agent is Claude) + the **unit suite** + an optional **dev smoke**, then
a Claude Code **Stop hook** surfaces any issues back to the dev agent so it can
fix them. The tester is **read-only** — it never edits/commits; the dev agent
fixes (author/reviewer separation).

## Pieces

- `run-tester.sh` — the tester. Codex review (`-m openai.gpt-5.5 -s read-only`)
  - `pytest tests/unit` + `dev-smoke.sh` (if present). Writes
    `.omc/tester-findings.md` (`commit:`/`status:` header + details). Skips
    trivial (docs/scratch) commits; single-flight (a newer commit's run wins).
- `stop-hook.sh` — Claude Stop hook. If findings for the CURRENT HEAD show
  `status: issues`, blocks the stop **once** and injects the findings as the
  reason. No-op when there are no/stale/clean findings (safe without the tester).
- `dev-smoke.sh` — optional READ-ONLY live smoke (Cognito token + key API
  health). Starter only — extend `ENDPOINTS`.

## Install (local, per-clone — not committed)

1. Make scripts executable: `chmod +x tools/local-tester/*.sh`
2. Git post-commit hook (runs the tester detached):
   ```bash
   cat > .git/hooks/post-commit <<'EOF'
   #!/usr/bin/env bash
   REPO="$(git rev-parse --show-toplevel)"
   nohup bash "$REPO/tools/local-tester/run-tester.sh" "HEAD~1..HEAD" >/dev/null 2>&1 &
   exit 0
   EOF
   chmod +x .git/hooks/post-commit
   ```
3. Stop hook is registered in `.claude/settings.json` (committed; it no-ops
   without a findings file, so it's safe for everyone). Restart the Claude Code
   session to load it.

## How the loop works

dev commits → post-commit launches the tester (detached, non-blocking) →
tester writes findings → on the dev agent's next **Stop**, if the current
commit has issues, the hook blocks once + shows them → dev fixes → commits →
tester re-runs on the new commit → clean findings → Stop passes.

Feedback lands on a **later turn/commit** (the tester takes ~30–90s) — by
design. The dev agent fixes; the tester never does.

## Cost / tuning

- Codex runs on **every non-trivial commit** → tokens per commit. To reduce:
  switch the hook to **post-merge** (only on merges to main) by writing the
  same launcher to `.git/hooks/post-merge` with range `ORIG_HEAD..HEAD` and
  removing `post-commit`; or gate `run-tester.sh` further.
- Model override: `DBOPS_TESTER_CODEX_MODEL` env (default `openai.gpt-5.5`).
- Smoke env: `DBOPS_API_URL`, `DBOPS_COGNITO_CLIENT_ID`, `AWS_REGION`.

## Disable

Remove `.git/hooks/post-commit` (stops the tester). The Stop hook then no-ops
(no fresh findings). Or delete `.omc/tester-findings.md`.

## Notes / gotchas

- Live smoke needs a deployed dev env + a valid local session
  (`frontend/e2e/.auth/state.json`) + AWS creds; it SKIPs (never fails) otherwise.
- Codex uses the local Bedrock config (`~/.codex/config.toml`, model
  `openai.gpt-5.5`) — no login. A wrong model id 404s (use the `openai.` prefix).
- The tester reads the **committed** state; run a fix as a new commit to
  re-trigger it. It never edits the working tree.

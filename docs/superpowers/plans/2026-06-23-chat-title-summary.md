# Chat Session Title Auto-Summary — Design + Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Replace the chat session title (currently the first user message sliced to 50 chars) with a concise LLM-generated title after the first exchange. Frontend-only; mirrors the existing `generateFollowups` pattern.

**Architecture:** Add `generateTitle(convId, userText, assistantText)` to `chat-panel.tsx`, a near-copy of `generateFollowups` (chat-panel.tsx:627-684): a throwaway `streamChat` session (`title-${convId}-${Date.now()}`) so the main conversation memory stays clean, a title-specific prompt, parse the result, set the conversation title (only when it's still the default first-message-slice), and persist via the existing `putChatSession` path. Best-effort/silent on failure (like followups).

## Global Constraints

- Frontend-only — no backend/CDK/openapi. Reuse `streamChat` + the existing `persist`/`putChatSession` flow.
- **Throwaway session id** (`title-...`) so title-gen never pollutes the conversation's agent memory (exactly like `generateFollowups`).
- Trigger ONCE, on the FIRST exchange only (when the title is still the auto first-message-slice). Do not regenerate on every turn or overwrite a user-set/handoff title (e.g. the `RCA · {cluster}` handoff title at chat-panel.tsx:405).
- Korean title, ≤ ~6 words / ≤ 50 chars; strip surrounding quotes/markdown/code-fences; if generation fails or is empty, keep the existing first-message title (no regression).
- Best-effort + silent (no error surfaced); abortable like followups (a `titleAbortRef`), cancelled on a new send.
- Commit: conventional subject; NO `Co-Authored-By: Claude` trailer; no internal-roadmap refs. Frontend prettier hook → `git add -A` + re-commit.

## Task 1: generateTitle + first-turn trigger

**File:** Modify `frontend/src/components/chat/chat-panel.tsx`.

- [ ] **Step 1: `generateTitle`** — copy `generateFollowups` (lines 627-684) into a new `generateTitle(convId, userText, assistantText)` useCallback:

  - Skip if `assistantText.trim().length < 40` (too short to title meaningfully).
  - Add a `titleAbortRef = useRef<AbortController|null>(null)`; abort prior before starting.
  - Prompt (Korean): `이 질문/답변을 6단어 이내의 간결한 한국어 제목으로 요약해줘. 제목 텍스트만 출력 — 따옴표·마크다운·코드펜스·접두어 금지.\n\nQ: ${userText}\n\nA: ${assistantText.slice(0,2000)}`
  - throwaway session id `title-${convId}-${Date.now()}`, `modelId`.
  - On done: take the buffer, trim, strip wrapping quotes/backticks, collapse newlines, cap to 50 chars; if non-empty, `persist` the conversation's `title` (only if that conv's current title still equals the first-message-slice — guard against overwriting a user/handoff title). The existing persist→putChatSession path saves it.
  - deps: `[modelId, persist]`.

- [ ] **Step 2: Trigger on first turn** — find where `generateFollowups(...)` is called after the main `streamChat` completes (the main turn's onComplete near chat-panel.tsx:782). Capture whether this turn was the conversation's FIRST exchange (e.g., a `const isFirstTurn = cleared.length === 0` captured in `sendText` at line ~720/728, threaded into the onComplete closure). In onComplete, if `isFirstTurn`, also call `generateTitle(convId, userText, finalAssistantText)`. (Mirror exactly how `generateFollowups` gets the final assistant text.)

- [ ] **Step 3: Build** — `cd frontend && npm run build` → exit 0, `/chat` in route list.

- [ ] **Step 4: Commit** — `git add frontend/src/components/chat/chat-panel.tsx` ; `git commit -m "feat(chat): auto-summarize session title after first exchange"` (prettier → `git add -A` + re-run).

## Self-Review

- Frontend-only, additive; mirrors proven generateFollowups (throwaway session, silent best-effort). ✓
- First-turn-only + title-still-default guard → no overwrite of user/handoff titles, no per-turn regen. ✓
- Failure keeps the first-message title (no regression). ✓

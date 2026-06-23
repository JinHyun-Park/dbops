# Query Rewrite Suggestion + EXPLAIN before/after (Query Lab)

> Version: 2.0
> Date: 2026-06-23
> Status: Draft
> Author: AI-assisted design (Claude Opus 4.8)

## 1. Overview

### 1.1 Purpose

Query Lab는 SQL을 EXPLAIN(`/api/explain` → plan-tree)·분석(`handleAnalyze` → 에이전트
`streamChat`)할 수 있지만, **재작성(rewrite) 제안**은 사용자가 채팅에 직접 물어야 한다. 본 기능은
일급 **"리라이팅 제안"** 액션을 추가한다: 에이전트가 현재 SQL(+EXPLAIN plan 근거)로 재작성안을
제안하고, **원본 vs 제안의 EXPLAIN plan(추정 cost)을 before/after로 비교**한다.

### 1.2 핵심 설계 결정 (안전)

- **제안은 advisory.** 의미적 동등성 자동 보장 안 함 — 에이전트 생성, DBA 검토·검증, **실행/적용 없음**.
- **🔴 before/after는 plan-only(추정 cost)로 비교한다 — 제안 SQL을 절대 실행하지 않는다.**
  현재 `/api/explain`(PG)은 `EXPLAIN (ANALYZE, …)` 로 **쿼리를 실제 실행**한다. 에이전트가 만든
  **미검증** 재작성을 ANALYZE하면 그 SELECT를 실행하게 되어(슬로우 쿼리 재작성이라 비싸고
  미검증이라 위험) 안 된다. → `/api/explain`에 `analyze` 플래그(기본 true=하위호환; rewrite 경로는
  **false=plan-only**, ANALYZE 없는 추정 cost)를 추가하고, before/after는 **양쪽 모두 plan-only
  추정 total cost**로 비교한다(공정·안전).
- **기존 인프라 재사용** — `streamChat`(에이전트), `/api/explain`(plan-only 모드 추가), plan-tree
  컴포넌트. 새 MCP 툴/CDK 없음. 새 openapi: `/api/explain`에 옵셔널 필드 추가뿐(라우트 불변).

### 1.3 Goals

- Query Lab "리라이팅 제안" 버튼 + `handleRewrite(sql)`(analyze 머신리 재사용, 재작성 프롬프트).
- 프롬프트: 재작성 SQL(`sql`)+근거+주의사항, **한국어**, plan 있으면 근거 포함.
- 제안 스트리밍 완료 후: 응답에서 `sql` 블록 추출 → **원본·제안 모두 plan-only EXPLAIN** →
  **추정 total cost before/after + 양쪽 plan-tree** 비교 렌더. 제안 SQL이 invalid면 EXPLAIN 실패를
  곱게 표시(advisory).
- `/api/explain`에 `analyze: boolean`(기본 true) 추가 — false면 PG도 `EXPLAIN (BUFFERS,
VERBOSE, FORMAT JSON)`(ANALYZE 제외, 실행 안 함). SELECT 제한·RBAC 등 기존 가드 유지.
- advisory 배너 + 기존 EXPLAIN/analyze/preset 동작 불변.

### 1.4 Non-Goals

- 의미적 동등성 보장 / 재작성 자동 실행·적용(쓰기 → 승인 게이트, 범위 밖).
- 제안 SQL의 ANALYZE(실제 실행) — plan-only만.
- 새 performance MCP 툴·새 REST 라우트·CDK.

## 2. Architecture

````
[Query Lab] SQL 입력
  └ "리라이팅 제안" → handleRewrite(sql):
       1) streamChat(rewrite 프롬프트 + (explain plan 요약 있으면 첨부) + ```sql```)
            → analysis 탭에 제안 스트리밍(재작성 SQL+근거+주의)
       2) onDone: 응답 텍스트에서 첫 ```sql ...``` 블록 추출 → proposedSql
       3) proposedSql 있으면:
            POST /api/explain {sql: originalSql, analyze:false}  → beforePlan (추정 cost)
            POST /api/explain {sql: proposedSql, analyze:false}  → afterPlan  (추정 cost, 실행 X)
            → before/after: total cost 비교(개선%) + 양쪽 plan-tree
          proposedSql 없거나 EXPLAIN 실패 → 비교 생략 + 곱게 안내(제안 텍스트는 그대로)
````

plan-only EXPLAIN은 쿼리를 실행하지 않으므로 미검증 제안 SQL에도 안전. cost는 planner 추정치
(real timing 아님) — advisory 비교로 충분하고 안전이 우선.

## 3. Components

### 3.1 Backend (api 스택)

- `api/explain/handler.py`: 요청 body에 `analyze`(기본 true) 수용. PG `_build_explain_sql`이
  `analyze=false`면 `EXPLAIN (BUFFERS, VERBOSE, FORMAT JSON)`(ANALYZE 제외) 생성. MySQL은 이미
  ANALYZE 미사용이라 무변. SELECT-only·RBAC·prefix-strip 등 기존 가드 전부 유지.
- 새 라우트 없음 → openapi는 `/api/explain` 요청 스키마에 옵셔널 `analyze` 추가 재생성만.
- 유닛: `analyze=false` → SQL에 ANALYZE 없음·BUFFERS 있음; 기본(미지정/true) → 기존대로 ANALYZE;
  SELECT 가드 불변.

### 3.2 Frontend

- `lib/api-client.ts`: `fetchExplain`(또는 해당 함수)에 `analyze?: boolean` 옵션 추가 → body에 전달.
  순수 헬퍼 `extractSqlBlock(markdown): string | null`(첫 `sql` 블록), `planTotalCost(plan): number|null`.
- `app/query-lab/page.tsx`: `handleRewrite(sql)`(handleAnalyze 미러 + 재작성 프롬프트). onDone에서
  `extractSqlBlock` → 원본·제안 plan-only EXPLAIN(`analyze:false`) → before/after 상태. "리라이팅 제안"
  버튼(기존 액션 옆) + advisory 배너. before/after 패널: 추정 total cost 원본 vs 제안(개선/악화 %),
  양쪽 `PlanTree` 재사용. 제안 SQL/EXPLAIN 실패는 곱게 처리. 기존 동작 불변.

## 4. Safety

- 읽기 전용·advisory. 제안 SQL은 **plan-only EXPLAIN(실행 안 함)** 으로만 평가 — 미검증 SQL 실행
  위험 제거. ANALYZE 경로는 사용자 직접 EXPLAIN(기존, analyze 기본 true)에서만.
- 기존 SELECT-only·서버측 RBAC·인증 가드 유지. 새 권한·엔드포인트 없음.
- 프롬프트/EXPLAIN은 사용자 SQL + 자신의 plan만 — 외부 유출 없음.

## 5. Increments

1. **Backend**: `/api/explain` `analyze` 플래그(plan-only 모드) + openapi 재생성. 유닛.
2. **Frontend**: `handleRewrite` + before/after EXPLAIN 비교 + 버튼/배너 + 헬퍼(extractSqlBlock,
   planTotalCost). 빌드 + 종단.

## 6. Test Strategy

- Backend 유닛: `analyze=false` → ANALYZE 없는 EXPLAIN(BUFFERS/FORMAT JSON 유지); 기본 → ANALYZE
  유지; SELECT 가드·prefix-strip 불변; `/api/explain` openapi parity.
- Frontend: 순수 헬퍼(`extractSqlBlock`/`planTotalCost`) 단위 테스트(있으면), 빌드 게이트.
- 회귀: 기존 EXPLAIN(analyze 기본 true)·analyze·preset 동작 불변.
- 종단: dev에서 슬로우 쿼리 → "리라이팅 제안" → 재작성안 스트리밍 + 원본/제안 추정 cost
  before/after + plan-tree, advisory 배너. 제안 SQL invalid 시 곱게 처리.

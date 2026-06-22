# Agent Task Execution Visibility (실행 추적 + 작업 통계)

> Version: 1.0
> Date: 2026-06-23
> Status: Draft
> Author: AI-assisted design (Claude Opus 4.8)

## 1. Overview

### 1.1 Purpose

Agent Tasks(이벤트 자동 RCA·예약 리포트·수동 실행)는 현재 결과(`result`)와 한 줄
`summary`만 보여준다. DBA가 "에이전트가 **왜** 그 결론을 냈는지, **어떤 신호/도구**를
검사했는지, **얼마나** 걸렸는지"를 볼 수 없다. 또 작업 시스템 전체가 잘 돌고 있는지
(성공률·평균 소요·실패 추이) 한눈에 볼 집계도 없다.

본 기능은 두 가지를 추가한다:

1. **per-task 실행 추적(trace)** — 워커가 실행한 단계(어떤 도구/생성기가 돌았는지),
   각 단계 소요시간, 검사·스킵한 신호 소스, 총 소요시간을 기록하고 `/tasks` 상세에
   노출한다.
2. **작업 통계(stats)** — 상태/종류별 개수, 성공률, 평균 소요, 최근 실패 수를 집계하는
   엔드포인트 + `/tasks` 상단 요약 스트립.

### 1.2 Goals

- 결정론적 RCA가 검사한 신호 소스(`signals_examined`)·스킵(`skipped`)과 단계별 타이밍을
  `result`/row에 구조화해 기록하고 상세에 렌더.
- 작업 row에 `trace`(단계 리스트)·`duration_ms`(총 소요) 추가 — additive, 비파괴.
- `GET /api/tasks/stats` 집계 엔드포인트 + `/tasks` 상단 통계 스트립.
- 기존 task 흐름·RCA 엔진·결과 렌더는 **불변**(trace 없는 과거 row도 정상 렌더).

### 1.3 Non-Goals

- 분산 트레이싱/OpenTelemetry, LLM 토큰 단위 추적 — 범위 밖(경량 단계 trace만).
- RCA 엔진(`diagnose_root_cause`) 로직 변경 — 그대로 두고 메타데이터만 노출.
- 작업 재실행/취소 UI — 별도 후속.

## 2. Architecture

### 2.1 데이터 모델 — `dbops-{env}-agent-tasks` (추가 속성, 스키마리스)

`done`/`failed`로 마무리할 때 두 속성을 추가로 기록(있을 때만; 마이그레이션 불필요):

- `trace` (List): 단계별 `{ "step": str, "tool": str, "ms": int, "detail": str }`.
  - 예(auto_rca): `[{"step":"진단","tool":"diagnose_root_cause","ms":420,"detail":"5개 소스 검사 · 후보 3"}, {"step":"서술 생성","tool":"bedrock","ms":1180,"detail":"한국어 narrative+권장조치"}]`
  - narrative 스킵 시: `{"step":"서술 생성","tool":"bedrock","ms":0,"detail":"모델 미설정 — 스킵"}`
  - report: `[{"step":"헬스 다이제스트","tool":"health_status","ms":300,"detail":"엔진 aurora-postgresql · 5개 메트릭"}]`
- `duration_ms` (int): 워커가 claim 이후 finish까지 소요한 총 ms.

신호 검사 내역(`signals_examined`, `skipped`)은 이미 RCA `result`에 들어있으므로 별도 저장
없이 상세에서 그대로 렌더(아래 §3.3).

### 2.2 데이터 흐름

```
[task_worker.lambda_handler]
  claim → t0 = now
  ├ kind=auto_rca|manual_rca:
  │   step "진단": diagnose_root_cause_impl(cache)        → ms, signals_examined
  │   step "서술 생성": _narrative(...)  (or skipped)      → ms
  ├ kind=scheduled_report:
  │   step "헬스 다이제스트": health_status_impl(cache)    → ms
  _finish(status=done, result, summary, ticket_url, trace, duration_ms=now-t0)
                       │
                       ▼
[GET /api/tasks/{id}]  → row 전체(트레이스 포함, 기존 핸들러 그대로)
[GET /api/tasks/stats] → 집계(상태/종류별·성공률·평균 소요·최근 실패)
                       │
                       ▼
[/tasks 페이지] 상세에 "실행 추적" + "검사한 신호" + 소요시간 / 상단 통계 스트립
```

### 2.3 실패 경로

`failed`도 `trace`(끝까지 못 간 단계까지)와 `duration_ms`를 기록 — 어디서 멈췄는지 보이게.
trace 기록은 best-effort: trace 조립 실패가 작업 완료를 깨지 않는다(누락 시 trace 없이 finish).

## 3. Components

### 3.1 Backend (agent 스택)

- `mcp-servers/mcp_servers/workers/task_worker.py`:

  - 경량 타이밍 헬퍼(`_step(trace, label, tool, fn)` 또는 인라인 `time.time()` 델타)로 각
    생성기 호출을 감싸 `trace`에 단계 추가.
  - `_run_rca`/`_run_report`가 `(result, summary, trace_steps)`를 반환하도록 확장(또는
    핸들러에서 단계 측정). `signals_examined` 카운트를 진단 단계 `detail`에 요약.
  - `_finish(..., trace=None, duration_ms=None)` 파라미터 추가 → SET 절(있을 때만). ms는
    int로 저장(float→Decimal 변환 불필요하나 `_ddb_safe` 통과).
  - `lambda_handler`: claim 직후 `t0`, finish 시 `duration_ms`. 실패 경로도 trace/duration 기록.

- `api/tasks/handler.py`:
  - `GET /api/tasks/{id}`는 row 전체를 반환하므로 **trace 자동 노출**(변경 없음).
  - **NEW** `GET /api/tasks/stats`: recency-index에서 최근 N(기본 500)건을 쿼리해 집계 —
    `{total, by_status:{...}, by_kind:{...}, success_rate, avg_duration_ms, recent_failures}`.
    `raw_path.endswith("/stats")` 분기로 같은 Lambda에서 처리.

### 3.2 CDK (agent 스택)

- `cdk/stacks/agent_stack.py`: `GET /api/tasks/stats` 라우트를 tasks Lambda에 개별 등록
  (add_routes 필수). 기존 tasks 라우트와 동일 integration 재사용.
- `tools/openapi_gen.py` 재생성 → `frontend/public/openapi.json`에 `/api/tasks/stats` 포함
  (`test_openapi_spec` 게이트 통과).

### 3.3 Frontend

- `lib/api-client.ts`: `AgentTask`에 `trace?: {step,tool,ms,detail}[]`·`duration_ms?: number`
  추가. `fetchTaskStats() -> Promise<TaskStats>` 추가.
- `app/tasks/page.tsx`:
  - `TaskRow` 상세(done/failed)에 **"실행 추적"** 섹션: 단계 리스트(도구·`detail`·`{ms}ms`)
    - 총 소요(`duration_ms`). RCA면 `result.signals_examined`/`skipped`를 **"검사한 신호"**
      소형 표로 렌더(소스별 카운트). 기존 narrative/candidates/lines 렌더는 그대로 위에 유지.
  - 페이지 상단에 **통계 스트립**: 총 작업·성공률·평균 소요·종류별 카운트(`fetchTaskStats`).
    숫자 ≥1000은 기존 `fmtDecimal`/`fmtExact` 사용. 빈 상태/로딩 처리.

## 4. Safety / Cost

- 전 경로 **읽기 전용**(trace는 메타데이터 기록만). 인증 authorizer 하위.
- trace에 시크릿·쿼리 본문 없음 — 도구명·카운트·소요시간·짧은 한국어 detail만.
- stats는 최근 N건 쿼리 1회(인덱스) — 저비용. N 상한으로 스캔 비용 제한.
- ms 타이밍은 `time.time()` 델타(int ms) — 기존 `_ddb_safe`로 DDB 안전.

## 5. Increments (구현 순서)

1. **Backend trace**: task_worker가 단계 trace + duration_ms를 기록(+ `_finish` 확장).
   유닛: trace 구조·duration 기록·실패 경로 trace·기존 동작 불변·float-free.
2. **API stats**: `GET /api/tasks/stats` + 라우트 등록 + openapi 재생성.
   유닛: 집계 정확성(상태/종류/성공률/평균), 빈 테이블, parity.
3. **Frontend**: 실행추적/검사신호/소요 상세 + 통계 스트립 + api-client.
   빌드·배포·종단(자동 RCA 작업 상세에서 trace·신호·소요·통계 렌더).

## 6. Test Strategy

- task_worker 유닛: trace에 진단/서술 단계 존재, narrative 스킵 시 detail 반영, duration_ms>0,
  실패 시에도 trace/duration 기록, `_finish` write 횟수 불변(2), float-free(Decimal).
- api 유닛: stats 집계(by_status/by_kind/success_rate/avg_duration), 빈 테이블 0-safe,
  `/api/tasks/stats` openapi parity.
- 기존 task_worker/tasks 회귀: trace 없는 과거 row 정상 렌더, 기존 테스트 그대로 통과.
- 종단: dev에서 자동/수동 RCA 작업 상세에 실행추적·검사신호·소요시간, 상단 통계 스트립 렌더.

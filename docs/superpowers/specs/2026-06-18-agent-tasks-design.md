# Agent Tasks — Event-driven & Scheduled Agent Work

> Version: 1.0
> Date: 2026-06-18
> Status: Draft
> Author: AI-assisted design (Claude Opus 4.8)

## 1. Overview

### 1.1 Purpose

DBOps는 현재 **동기식**이다 — DBA가 채팅으로 묻거나 대시보드를 봐야만 에이전트가
일한다. 본 기능은 에이전트가 **사용자 없이도** 일하게 하는 Task 서브시스템을 추가한다:

1. **이벤트 기반 자동 RCA** — 경보가 발생하면 자동으로 근본원인 분석을 생성해
   둔다. DBA가 토스트를 클릭하면 _이미 분석된_ RCA로 바로 연결된다.
2. **예약 작업** — "매주 월요일 top slow query 리포트" 같은 반복 작업.
3. **수동 작업** — 대시보드/Fleet에서 임의 클러스터에 대해 작업을 즉시 실행.

DBOps의 기존 alert_evaluator + WebSocket 푸시 + RCA 드로어와 직결되어, 운영자가 개입하지 않아도 분석·리포트가 준비되도록 한다.

### 1.2 Goals

- 경보→자동 RCA→토스트 연결 (2026-06-18 토스트 딥링크의 논리적 다음 단계).
- 세 가지 트리거(alert / schedule / manual)를 **단일 처리 경로**로 수용.
- RCA는 **결정론적** 엔진(`diagnose_root_cause`)으로 — Lambda에서 LLM 없이
  수초 내 랭킹된 후보 생성. 저비용·안정·재시도 가능.
- CDK-only. 스택 의존성(foundation→data→agent) 위배 없음.

### 1.3 Non-Goals (이번 범위 밖)

- LLM 서술형 RCA 자동 생성 (온디맨드 RCA 드로어가 계속 담당; 추후 하이브리드 가능).
- 작업 결과의 외부 티켓팅 전송 (별도 후속 작업).
- 복잡한 cron 표현식 — 우선 interval(매일/매주/매시간) 수준.

## 2. Architecture

### 2.1 처리 경로 (단일 경로, DynamoDB Streams)

```
[alert_evaluator]  (data stack)     ┐
[task_scheduler]   (data stack)     ├─► agent-tasks 테이블(pending 행 INSERT)
[POST /api/tasks]  (agent stack)    ┘            │
                                                 │ DynamoDB Stream (NEW_IMAGE)
                                                 ▼
                                         [task_worker] (agent stack)
                                          status=running
                                          ├─ kind=auto_rca|manual_rca → diagnose_root_cause(cache)
                                          └─ kind=scheduled_report     → report builder(cache)
                                          status=done + result + summary
                                                 │
                                                 ├─► agent-tasks UPDATE
                                                 └─► WS broadcast {type:"task", task_kind:"...ready", ...}
                                                          │
                                                          ▼
                                              [frontend] 토스트 → 저장된 RCA로 연결 / 작업 목록
```

**디커플링 근거**: 스택 의존성은 foundation→data→agent 단방향이라 data 스택의
alert_evaluator가 agent 스택의 worker를 직접 invoke할 수 없다. agent-tasks 테이블을
**foundation 스택**에 두고 **Streams**를 켜면, 누가 pending 행을 넣든(세 트리거 전부)
worker(agent 스택)가 스트림으로 트리거된다 — 역방향 의존성 없이 단일 경로.

### 2.2 데이터 모델 — `dbops-{env}-agent-tasks` (DynamoDB, foundation)

| 속성                         | 설명                                                             |
| ---------------------------- | ---------------------------------------------------------------- |
| `task_id` (PK)               | uuid                                                             |
| `cluster_id`                 | 대상 클러스터                                                    |
| `kind`                       | `auto_rca` \| `manual_rca` \| `scheduled_report`                 |
| `trigger`                    | `alert:{rule_id}` \| `schedule:{schedule_id}` \| `manual:{user}` |
| `status`                     | `pending` \| `running` \| `done` \| `failed`                     |
| `created_at`                 | ms epoch 문자열 (정렬 키)                                        |
| `started_at`, `completed_at` | ms epoch                                                         |
| `title` / `summary`          | 토스트·목록용 짧은 텍스트                                        |
| `result`                     | JSON (RCA 후보 / 리포트 페이로드)                                |
| `error`                      | 실패 시 사유                                                     |
| `ttl`                        | created_at + 30d (자동 만료)                                     |

- **Stream**: `NEW_IMAGE`. worker는 INSERT 이며 `status=pending` 인 레코드만 처리.
- **GSI1 `byCluster`**: PK `cluster_id`, SK `created_at` — 클러스터별 최근 작업.
- **GSI2 `byRecency`**: PK `record_type`(상수 `"task"`), SK `created_at` — Fleet 전역 최근 목록.
- DDB scan은 사용하지 않음(메모리 규칙). 목록은 항상 GSI Query + 페이지네이션.

### 2.3 예약 정의 — `scheduled_tasks` (Aurora PG cache, alert_rules 패턴 미러)

`(id, cluster_id, kind, interval_kind['hourly'|'daily'|'weekly'], params jsonb,
enabled bool, last_run_at, created_at)`. task_scheduler가 due 판정 후 enqueue.

## 3. Components

### 3.1 CDK

- **foundation_stack**: `agent_tasks_table`(+Streams +2 GSI), `grant_task_enqueue()`
  (put 권한) / `grant_task_manage()`(읽기·쓰기) 헬퍼.
- **data_stack**:
  - alert_evaluator에 agent_tasks put 권한 부여.
  - `task_scheduler` Lambda(EventBridge rate; 캐시 read + agent_tasks put).
- **agent_stack**:
  - `task_worker` Lambda(`../mcp-servers` 에셋 + 공유 레이어; 캐시 read; agent_tasks
    R/W; WS broadcast). 이벤트 소스 = agent_tasks 스트림(filter: INSERT).
  - API 라우트: `GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks`,
    `GET|POST|DELETE /api/scheduled-tasks`. (라우트는 path별 개별 등록 — add_routes 필수.)

### 3.2 Backend

- `mcp-servers/mcp_servers/workers/task_worker.py` — 스트림 핸들러. kind 분기:
  - `auto_rca`/`manual_rca` → `diagnose_root_cause_impl(cache, cluster_id)` +
    `correlate_signals`/`recent_events`로 요약 보강 → top 후보 + 1줄 summary.
  - `scheduled_report` → 리포트 빌더(우선 top slow query / health 요약).
  - 멱등: 이미 running/done 이면 skip. 실패 시 status=failed + error.
- alert_evaluator: 트리거 꼬리(`triggered += 1` 직전)에서 agent_tasks put.
  **디듀프**: 같은 cluster의 auto_rca 가 N분(기본 15) 내 존재하면 skip(GSI1 query).
- `api/tasks/handler.py` — list/get/create. `api/scheduled_tasks/handler.py` — CRUD.

### 3.3 Frontend

- `lib/alert-stream.ts` PushedAlert에 `type:"task"` 수용 → `use-alert-badge`가
  "RCA 준비됨: <cluster> →" 토스트(저장 RCA 딥링크).
- 저장 RCA 뷰: `rca-drawer`를 저장 결과로 렌더(에이전트 SSE 대신 task.result).
- `/tasks` 목록(또는 Activity 확장): status·kind·클러스터(엔진 배지)·결과 링크.
- 수동 실행 버튼: 대시보드/Fleet → `POST /api/tasks`.
- 예약 작업 설정 UI: Alerts 페이지 인접 섹션 — create/list/delete.

## 4. Safety

- 모든 작업은 **읽기 전용**(캐시 분석). write 액션은 기존 Approval 게이트를 거치므로
  Task가 자동으로 변경을 실행하지 않는다.
- 수동 `POST /api/tasks`는 인증 필요(authorizer). cluster_id는 레지스트리 검증.
- 디듀프 + TTL로 작업 폭주·잔존 방지.

## 5. Increments (구현 순서)

1. **백엔드 코어**: agent_tasks 테이블+스트림(foundation), task_worker(RCA),
   alert_evaluator 훅+디듀프, `GET /api/tasks`·`/api/tasks/{id}`. → 배포·종단검증
   (경보→task→worker→result→WS).
2. **프런트 자동 RCA**: 토스트 task 처리 + 저장 RCA 뷰 + `/tasks` 목록. → 검증.
3. **수동 실행**: `POST /api/tasks` + 수동 버튼. → 검증.
4. **예약 작업**: scheduled_tasks 캐시 테이블 + task_scheduler + 스케줄 CRUD API +
   설정 UI + scheduled_report 빌더. → 검증.

## 6. Test Strategy

- worker 유닛: kind 분기, 멱등, 실패 처리(diagnose_root_cause를 mock).
- alert_evaluator 유닛: 디듀프(최근 task 있을 때 put 안 함), put 페이로드.
- API 유닛: 핸들러↔스키마 parity, 페이지네이션, authorizer.
- CDK snapshot: 새 테이블/Lambda/스트림/라우트.
- 종단: dev에서 경보 발화 → task 행 → result 저장 → 토스트 → 저장 RCA 열람.

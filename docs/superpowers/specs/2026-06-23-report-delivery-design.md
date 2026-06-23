# Report Delivery (digest push + download)

> Version: 1.0
> Date: 2026-06-23
> Status: Draft
> Author: AI-assisted design (Claude Opus 4.8)

## 1. Overview

### 1.1 Purpose

`report_generator`는 매일 클러스터별 리포트를 생성해 `reports` 테이블 + S3에 저장하지만
**아무 데도 전달하지 않는다** — DBA가 `/reports`를 열어야만 본다. 본 기능은 두 가지를 추가한다:

1. **전달(delivery)** — 리포트 생성 시 NL 요약 다이제스트를 **관리형 Slack 구독자**(기존 알림
   인프라 재사용) + **SNS 토픽**(이메일 구독자, 고객 SMTP 불필요)으로 푸시.
2. **다운로드** — `/reports`에서 리포트를 파일로 받을 수 있게. `/api/reports/{id}`가 이미
   리포트 data를 반환하므로 **클라이언트 사이드 다운로드**(받은 data/summary를 Blob `.md`/`.json`
   으로 저장) — 백엔드/CDK/S3 권한 불필요, 같은 내용.

### 1.2 Goals

- 리포트 생성 직후 다이제스트를 SNS(`ALERT_TOPIC_ARN`) publish + 관리형 Slack 구독자
  (`alert_subscribers_managed`, protocol `slack-webhook`)에 POST — alert_evaluator 패턴 재사용.
- 전달은 **opt-in 플래그**(`REPORT_DELIVERY_ENABLED`, 기본 off)로 게이트 — 기존 알림 구독자가
  일일 리포트로 놀라지 않게(inert by default).
- `/reports`에 다운로드: `GET /api/reports/{id}/download` → S3 presigned URL(없으면 인라인 데이터).
- 기존 인프라 전면 재사용: SES 없음(이메일=SNS 구독), 새 구독자 타입 없음, 스케줄 변경 없음.
- 전달 실패가 리포트 생성을 깨지 않음(격리); 구독자 0/플래그 off → 완전 no-op.

### 1.3 Non-Goals

- SES/SMTP 직접 발송 — 이메일은 기존 SNS 토픽 구독으로 충족.
- 리포트 전용 별도 구독자 테이블 — 기존 `alert_subscribers_managed` 재사용(v1).
- PDF/PPTX 등 리치 포맷 생성 — 현재 JSON/요약을 그대로(다운로드는 저장된 산출물).
- 스케줄/리포트 빌드 로직 변경.

## 2. Architecture

### 2.1 전달 흐름 (report_generator, data 스택)

```
[report_generator] (EventBridge daily)
  per cluster: _build_report_data → S3 put + reports INSERT + NL summary
  └─ (NEW) REPORT_DELIVERY_ENABLED=true 일 때만:
       _deliver_report(summary, cluster_id, report_date, report_type):
         ├─ SNS publish(ALERT_TOPIC_ARN, subject+message=요약)        → 이메일 구독자
         └─ alert_subscribers_managed(enabled, protocol=slack-webhook) 순회
              → Block Kit 다이제스트 POST(webhook)                    → Slack
       실패는 로그만, 리포트 생성 비차단. 구독자 0 → no-op.
```

- 구독자 읽기/Slack POST/상태기록은 alert_evaluator의 동일 패턴을 **복제**(이 repo 관행 —
  브로드캐스트 Lambda는 공유 레이어 대신 각자 작은 복사본 유지). PagerDuty는 리포트에 부적절
  하므로 제외(slack-webhook + SNS만).

### 2.2 다운로드 흐름 (프런트 전용)

```
[/reports] 행 선택 → 이미 GET /api/reports/{id}로 summary+data 보유 → "다운로드" 버튼
  → 클라이언트에서 마크다운(요약+핵심 data) 문자열 생성 → Blob → <a download> 클릭
  (백엔드/CDK 변경 없음; 같은 산출물 내용).
```

## 3. Components

### 3.1 Backend / data 스택

- `data-pipeline/report_generator/handler.py`: `_deliver_report(...)` + `_build_report_slack_blocks(...)`
  - `_post_json(...)`(alert_evaluator에서 복제). `lambda_handler`가 저장 후, `REPORT_DELIVERY_ENABLED`
    truthy면 호출. SNS publish는 `boto3.client("sns").publish`. 전체 try/except 격리.
- `cdk/stacks/data_stack.py`: report_generator env에 `ALERT_TOPIC_ARN`(=self.alert_topic.topic_arn)
  - `REPORT_DELIVERY_ENABLED`(=`getattr(Settings,"REPORT_DELIVERY_ENABLED",False)` → "true"/"false"
    문자열). `self.alert_topic.grant_publish(self.report_generator)`. (구독자 테이블은 캐시에 있어
    이미 read 가능.)
- `cdk/config/settings.example.py`: 문서화된 `REPORT_DELIVERY_ENABLED = False`.

### 3.2 Backend / api 스택

- 변경 없음. `GET /api/reports/{id}`가 이미 summary+data를 반환하므로 다운로드는 프런트에서
  처리(새 엔드포인트/라우트/openapi/S3 권한 불필요).

### 3.3 Frontend

- `app/reports/page.tsx`: 선택 리포트(이미 fetch된 summary+data)에 "다운로드" 버튼 → 마크다운
  문자열 조립(제목·요약·핵심 data 키/값) → `Blob` + 임시 `<a download="report-{cid}-{date}.md">`
  클릭 → revokeObjectURL. data 없으면 버튼 비활성/숨김. (필요 시 작은 헬퍼 `lib/`에 분리.)

## 4. Safety / Cost

- 전달은 **opt-in**(`REPORT_DELIVERY_ENABLED` 기본 false) → 배포해도 동작 무변화(inert).
- 전 경로 읽기 전용(리포트 산출물 전달/다운로드만). 다운로드 URL은 5분 만료 presigned.
- presigned URL에 자격증명 없음; SNS/Slack 페이로드에 시크릿·쿼리 본문 없음(요약 텍스트만).
- 비용: SNS publish + Slack POST(구독자 수 비례, 일 1회) — 미미. presigned 생성 무료.

## 5. Increments (구현 순서)

1. **전달**: report_generator `_deliver_report`(SNS + 관리형 Slack) + 플래그 게이트 + CDK
   (ALERT_TOPIC_ARN env, grant_publish, REPORT_DELIVERY_ENABLED) + settings.example.
   유닛: 플래그 off→no-op, 구독자 0→no-op, SNS publish 호출, Slack POST 호출, 전달 실패가
   생성 비차단.
2. **다운로드**: 프런트 전용 — `/reports`에 클라이언트 사이드 마크다운 다운로드 버튼.
   게이트: 빌드 통과 + 종단(버튼 클릭 시 파일 저장). 백엔드/openapi 변경 없음.

## 6. Test Strategy

- report_generator 유닛: `REPORT_DELIVERY_ENABLED` off → SNS/POST 미호출; on + 구독자 0 →
  SNS publish는 호출(이메일)·Slack POST 미호출; on + Slack 구독자 → POST 호출; 전달 예외가
  lambda_handler 완료를 안 막음. (boto3 sns/\_post_json mock.)
- 다운로드: 프런트 전용 — 유닛 테스트 없음(빌드 + 종단). 마크다운 조립 헬퍼를 분리하면
  순수함수 단위 테스트 가능(선택).
- 회귀: 기존 report 생성/저장·`/api/reports` list/get 불변.
- 종단: dev에서 플래그 on + 테스트 Slack 구독자로 수동 리포트 트리거 시 전달 확인(또는 SNS
  publish 로그) + /reports 다운로드 버튼으로 파일 저장 확인.

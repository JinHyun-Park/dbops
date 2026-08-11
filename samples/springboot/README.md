# Sample Spring Boot App — EC2 APM 테스트 타깃

DBOps의 **APM** 기능(`/apm` 페이지)이 실제로 모니터링할 대상이에요. 작은 Spring Boot
To-Do/Task CRUD 앱을 **Private Subnet EC2**에 배포하고, CloudWatch Agent가 애플리케이션
로그(JSON)와 호스트 메트릭(CPU/메모리/디스크)을 CloudWatch로 올려요. 의도적으로 심은
백엔드 버그 3종이 `ERROR`/`WARN` 로그로 남아서, 대시보드에서 로그 추적이 되는지 확인할 수
있어요.

> **비용 주의.** 상시 과금이 있어요 — **NAT Gateway 1개(~월 $32 + 데이터)** + `t3.small`
> 1대. 테스트가 끝나면 아래 *정리* 섹션대로 꼭 destroy 해주세요.

## 구성 요약

- **네트워크**: 자체 VPC. 앱 EC2는 **Private Subnet**에 있고 **Public IP 없음, 인바운드 SG
  규칙 없음**(egress only). 아웃바운드는 **NAT Gateway**로만 나가요. 접속은 **SSM Session
  Manager**로 해요.
- **앱**: Spring Boot 3(Java 17), H2 인메모리 DB, `:8080`. `systemd` 서비스(`todoapp`)로 상시 기동.
- **로그**: Logback → `/var/log/todoapp/app.log`에 **JSON**(각 줄에 `level` 필드 포함).
  CloudWatch Agent가 이 파일을 tail → Log Group **`/dbops/apm/todoapp`**.
- **트래픽**: VPC 내부 Lambda(2분 주기)가 정상 요청 다수 + 버그 유발 요청 소수를 섞어 호출해요.
  Private-only라 브라우저 직접 접속 대신 이 방식으로 로그·메트릭이 계속 흐르게 했어요.

## 사전 준비

- 빌드 호스트에 **Java 17 + Maven**.
- **CDK CLI ≥ 2.1134.0** (`npm i -g aws-cdk@2`), Python 3.9+.
- 배포 대상은 **DBOps와 동일 계정 `571850511781` / 리전 `us-east-1`** (`cdk/config/settings.py` 기준).

## 배포

```bash
# 1) fat jar 빌드 (CDK가 S3 asset으로 올릴 대상)
cd samples/springboot/app
mvn -q package -DskipTests      # target/todoapp.jar 생성

# 2) 스택 배포
cd ../cdk
pip install -r requirements.txt
cdk deploy dbops-dev-springboot-apm
```

배포가 끝나면 CfnOutput으로 `InstanceId`, `LogGroup`, `Region`, `VpcId`가 나와요. APM 타깃
등록에 그대로 쓰면 돼요.

## 앱 기동 확인 (SSM)

앱 인스턴스는 Public이 없으니 Session Manager로 들어가요.

```bash
aws ssm start-session --target <InstanceId> --region us-east-1
# 세션 안에서:
systemctl status todoapp
journalctl -u todoapp -n 50 --no-pager
tail -n 20 /var/log/todoapp/app.log      # JSON 로그가 쌓이는지 확인
```

첫 부팅은 Java 설치 + jar 다운로드 때문에 1~2분 걸릴 수 있어요. 로드 제너레이터가 한 번
돌고 나면 로그에 정상 200과 함께 버그 로그가 섞여 보여요.

## DBOps `/apm`에서 타깃 등록

`/apm` 페이지에서 타깃을 등록해요:

| 필드 | 값 |
| --- | --- |
| `instance_id` | CfnOutput `InstanceId` |
| `log_group` | `/dbops/apm/todoapp` |
| `region` | `us-east-1` |
| `spoke_role_arn` | **비워둠** |

`spoke_role_arn`을 비워두는 이유: DBOps와 **동일 계정**이라서 APM Lambda가 자기 실행 역할로
CloudWatch를 바로 읽어요. `api/apm/handler.py`의 `_session_for()`가 `role_arn`이 없으면
로컬 boto3 세션을 반환하고, 값이 있을 때만 `sts:assume_role`로 크로스 계정을 타거든요.

## 무엇을 보게 되나요

- **로그 검색**(기본 필터 ERROR+WARN): 아래 버그 3종의 로그가 잡혀요 — NPE 스택트레이스,
  DB 제약 위반, 리소스 누수 WARN/ERROR.
- **호스트 메트릭 카드**: EC2 CPU(`AWS/EC2`) + 메모리/디스크(`CWAgent`)가 채워져요.
- **APM/latency 카드는 비어 있어요 (의도된 동작).** 이 샘플은 CloudWatch Agent만 설치하고
  ADOT/Application Signals는 쓰지 않아요. latency/error-rate 같은 APM 메트릭은 그 계측이
  있어야 나오거든요.

## 의도적으로 심은 버그 3종

| 버그 | 트리거 | 결과 |
| --- | --- | --- |
| **NPE (500)** | `POST /api/tasks`에 `note`만 있고 `title` 없음 | `title.trim()`에서 NPE → `ERROR` 스택트레이스 + 500 |
| **미검증 입력 → DB 제약 위반** | 이미 있는 `title`로 `POST /api/tasks` | UNIQUE 제약 위반 `DataIntegrityViolationException` → `ERROR` + 500 |
| **리소스 누수** | `GET /api/leak` | 호출마다 1MB 버퍼를 static 리스트에 쌓고 안 놓음 → `WARN`, 임계치(10) 초과 시 `ERROR`. 시간이 지나며 힙/메모리 상승 |

정상 트래픽(대부분의 `POST/GET /api/tasks`, `GET /api/health`)은 200이에요. 버그는 특정
입력/엔드포인트에서만 소량 발동해서, 대시보드엔 건강한 200 사이에 ERROR/WARN이 꾸준히
조금씩 섞여 보여요.

## 로그 검색이 AccessDenied가 나면

동일 계정이면 대개 바로 되지만, 만약 `/apm` 로그 검색에서 `AccessDenied`가 나면 APM Lambda
실행 역할에 same-account CloudWatch 읽기 권한이 빠진 거예요. `cdk/stacks/agent_stack.py`의
APM Lambda 역할에 `logs:StartQuery`/`logs:GetQueryResults`/`logs:FilterLogEvents`,
`cloudwatch:GetMetricData`를 추가하면 돼요. (이 샘플 범위 밖의 후속 작업이에요.)

## 정리

이 스택은 자체 VPC라서 메인 DBOps 스택과 순서 의존성이 없어요. 단독으로 지우면 돼요.

```bash
cd samples/springboot/cdk
cdk destroy dbops-dev-springboot-apm
```

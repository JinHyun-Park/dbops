# Sample Spring Boot App — EC2 APM 테스트 타깃

DBOps의 **APM** 기능(`/apm` 페이지)이 실제로 모니터링할 대상이에요. 작은 Spring Boot
To-Do/Task CRUD 앱을 **Private Subnet EC2**에 배포하고, CloudWatch Agent가 애플리케이션
로그(JSON)와 호스트 메트릭(CPU/메모리/디스크)을 CloudWatch로 올려요. 의도적으로 심은
백엔드 버그 3종이 `ERROR`/`WARN` 로그로 남아서, 대시보드에서 로그 추적이 되는지 확인할 수
있어요.

> **비용 주의: 트래픽이 0이어도 월 $78 정도가 계속 나갑니다.**
>
> | 리소스                    | 시간당  | 월(730h) |
> | ------------------------- | ------- | -------- |
> | NAT Gateway 1개           | $0.059  | ~$43     |
> | Application Load Balancer | $0.0225 | ~$16     |
> | EC2 `t3.small` 1대        | $0.026  | ~$19     |
> | **고정 합계**             |         | **~$78** |
>
> 여기에 사용량 과금이 더 붙습니다: NAT 데이터 처리(GB당), ALB LCU, CloudFront 요청/전송,
> EBS 루트 볼륨. 이 표는 2026-08-13에 AWS Price List API로 조회한 **ap-northeast-2** 요율이고,
> 요율은 리전마다 다릅니다. 이 스택은 `cdk/config/settings.py`의 `REGION`에 배포되니 다른
> 리전이면 숫자가 달라집니다.
>
> 이 표는 정정된 값입니다. 이전에는 "NAT Gateway 1개(~월 $32) + t3.small"로만 적혀 있었는데,
> $32는 us-east-1 NAT 요율이었고 ALB와 CloudFront가 아예 빠져 있어서 실제의 절반 수준으로
> 읽혔습니다.
>
> **이건 DBOps 플랫폼의 일부가 아니라 APM 기능을 시연할 테스트 대상입니다.** 메인
> `cdk deploy`로는 절대 생성되지 않고(별도 CDK 앱이라 이 디렉터리에서 직접 배포해야 합니다),
> 시연이 끝나면 아래 _정리_ 섹션대로 destroy 해주세요.

## 구성 요약

- **네트워크**: 자체 VPC. 앱 EC2는 **Private Subnet**에 있고 **Public IP 없음, 앱 SG는
  ALB에서 오는 8080만 허용**. 아웃바운드는 **NAT Gateway**로만 나가요. 인스턴스 접속은 **SSM
  Session Manager**로 해요.
- **브라우저 접속**: **ALB**(public subnet)가 private EC2(:8080)를 앞단에서 받고, 그 앞에
  **CloudFront**를 둬서 안정적인 https URL로 열려요. 배포 후 `CloudFrontUrl` CfnOutput으로
  나와요. 브라우저나 `curl`로 버그를 직접 유발해 APM 로그 추적을 시연할 때 씁니다.
  ALB는 인터넷에 노출되어 있지만 **기본 응답이 403**이고, CloudFront가 붙이는
  `X-Origin-Verify` 헤더가 있는 요청만 EC2로 전달합니다. ALB DNS로 직접 오는 요청은 거부되니
  반드시 `CloudFrontUrl`을 쓰세요.
- **앱**: Spring Boot 3(Java 17), H2 인메모리 DB, `:8080`. `systemd` 서비스(`todoapp`)로 상시 기동.
- **로그**: Logback → `/var/log/todoapp/app.log`에 **JSON**(각 줄에 `level` 필드 포함).
  CloudWatch Agent가 이 파일을 tail → Log Group **`/dbops/apm/todoapp`**.
- **트래픽**: VPC 내부 Lambda(2분 주기)가 정상 요청 다수 + 버그 유발 요청 소수를 섞어 호출해서
  로그·메트릭이 상시 흐르게 해요. 여기에 더해 위 CloudFront URL로 직접 버그를 유발할 수도 있어요.

## 사전 준비

- 빌드 호스트에 **Java 17 + Maven**.

  > **`java -version`이 17이어도 부족합니다.** Maven은 `JAVA_HOME`을 보고, 그게 더 낮은 JDK를
  > 가리키고 있으면 `<java.version>17</java.version>` 빌드가 깨집니다. 실제로 겪은 상태:
  > `java -version`은 17.0.15인데 `JAVA_HOME`은 zulu-11이라 `mvn -version`이 Java 11로
  > 나왔습니다. 빌드 전에 확인하고, 필요하면 이 빌드에만 지정하세요.
  >
  > ```bash
  > mvn -version | grep 'Java version'          # Maven 이 실제로 쓰는 JDK
  > /usr/libexec/java_home -V                   # macOS: 설치된 JDK 목록
  > export JAVA_HOME=$(/usr/libexec/java_home -v 17)
  > ```

- **CDK CLI ≥ 2.1134.0** (`npm i -g aws-cdk@2`), Python 3.9+.
- 배포 대상은 **DBOps와 동일 계정/리전**이어야 해요. `app.py`가 `cdk/config/settings.py`의
  `Settings.ACCOUNT_ID`/`REGION`을 그대로 읽어 배포하거든요. 그러니 별도로 계정·리전을
  지정할 필요는 없고, **`settings.py`가 DBOps 배포에 쓰는 그 계정·리전인지**만 확인하면 돼요
  (fresh clone이라면 `settings.example.py`를 복사해 실제 값으로 채워두어야 해요. 예시 파일의
  `123456789012`/`ap-northeast-2`는 placeholder예요). 아래 명령 예시에 나오는 `us-east-1`은
  **작성 당시 환경의 값일 뿐 이 저장소의 현재 값이 아닙니다.** `settings.py`의 `REGION`으로
  바꿔서 실행하세요.

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

| 필드             | 값                     |
| ---------------- | ---------------------- |
| `instance_id`    | CfnOutput `InstanceId` |
| `log_group`      | `/dbops/apm/todoapp`   |
| `region`         | `us-east-1`            |
| `spoke_role_arn` | **비워둠**             |

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

| 버그                           | 트리거                                         | 결과                                                                                                                |
| ------------------------------ | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **NPE (500)**                  | `POST /api/tasks`에 `note`만 있고 `title` 없음 | `title.trim()`에서 NPE → `ERROR` 스택트레이스 + 500                                                                 |
| **미검증 입력 → DB 제약 위반** | 이미 있는 `title`로 `POST /api/tasks`          | UNIQUE 제약 위반 `DataIntegrityViolationException` → `ERROR` + 500                                                  |
| **리소스 누수**                | `GET /api/leak`                                | 호출마다 1MB 버퍼를 static 리스트에 쌓고 안 놓음 → `WARN`, 임계치(10) 초과 시 `ERROR`. 시간이 지나며 힙/메모리 상승 |

정상 트래픽(대부분의 `POST/GET /api/tasks`, `GET /api/health`)은 200이에요. 버그는 특정
입력/엔드포인트에서만 소량 발동해서, 대시보드엔 건강한 200 사이에 ERROR/WARN이 꾸준히
조금씩 섞여 보여요.

### 브라우저/`curl`로 직접 유발 (APM 추적 시연)

`CloudFrontUrl`(배포 CfnOutput)로 직접 버그를 일으켜 `/apm` 로그 검색에서 추적되는지
시연할 수 있어요. `CF`를 그 URL로 바꿔서:

```bash
CF=https://<distribution>.cloudfront.net
curl -s $CF/api/health                                          # 정상 200
curl -s -X POST $CF/api/tasks -H 'Content-Type: application/json' -d '{"note":"no title"}'   # BUG1 NPE → 500
curl -s -X POST $CF/api/tasks -H 'Content-Type: application/json' -d '{"title":"dup"}'       # 1회차 200
curl -s -X POST $CF/api/tasks -H 'Content-Type: application/json' -d '{"title":"dup"}'       # BUG2 제약위반 → 500
curl -s $CF/api/leak                                            # BUG3 리소스 누수 → WARN/ERROR
```

그 뒤 `/apm` 페이지(또는 `POST /api/apm/targets/<id>/logs/search`)에서 기본 ERROR+WARN
필터로 조회하면 방금 발생한 NPE 스택트레이스·제약위반·리소스 누수 로그가 잡혀요.

## 로그 검색이 AccessDenied가 나면

동일 계정이면 바로 됩니다. `cdk/stacks/agent_stack.py`의 APM Lambda 역할은 로그 검색이 실제로
쓰는 `logs:StartQuery`와 `logs:GetQueryResults`를 이미 갖고 있어요.

> **`logs:FilterLogEvents`를 추가하지 마세요.** 이 문단은 원래 그걸 추가하라고 안내했는데,
> 로그 검색 경로는 그 API를 호출하지 않습니다(`api/apm/handler.py`는 `start_query` +
> `get_query_results`만 씁니다). 게다가 그 액션을 `Resource: "*"`로 주면
> `cdk/cross-account/spoke-role-template.yaml`이 DocumentDB 프로파일러 로그 읽기를
> `/aws/docdb/*`로 좁혀둔 것을 무력화해서, 전용 회귀 테스트
> (`test_docdb_collector_log_read_is_prefix_scoped`)가 깨집니다.

그래도 `AccessDenied`가 나면 권한이 아니라 다른 원인일 가능성이 높아요. 먼저 확인할 것:
등록한 `log_group` 이름이 정확한지(오타가 있으면 CloudWatch가 권한 오류처럼 보이는 응답을
줄 수 있어요), 그리고 타깃에 등록된 `log_groups` 목록에 실제로 그 이름이 들어 있는지.
핸들러는 등록되지 않은 로그 그룹 요청을 **403으로 거부**하고 응답에 등록된 목록을 함께
돌려주니, 그 목록을 보고 맞추면 됩니다.

## 정리

이 스택은 자체 VPC라서 메인 DBOps 스택과 순서 의존성이 없어요. 단독으로 지우면 돼요.

```bash
cd samples/springboot/cdk
cdk destroy dbops-dev-springboot-apm
```

> **재배포 시 로그 그룹 충돌.** 로그 그룹 이름을 `/dbops/apm/todoapp`으로 고정해 뒀어요
> (APM 타깃 등록을 예측 가능하게 하려고요). destroy가 중간에 실패했거나 로그 그룹만 수동으로
> 남겨둔 상태에서 다시 `cdk deploy`하면 `ResourceAlreadyExistsException`이 날 수 있어요. 그럴
> 땐 배포 전에 남은 로그 그룹을 먼저 지워주세요:
> `aws logs delete-log-group --log-group-name /dbops/apm/todoapp --region us-east-1`

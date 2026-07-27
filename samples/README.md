# Samples

테스트/데모용 리소스. DBOps 플랫폼 배포와는 별도로 관리됩니다.

## 제품을 먼저 둘러보려면: 이 디렉터리가 아니라 인앱 샘플 생성

실제 DB 없이 대시보드를 채워보려면 앱의 **Clusters 페이지 → "Generate sample cluster"** 를
쓰세요. 캐시 DB에 24시간치 합성 메트릭·쿼리·이상 징후를 시드하고 모든 페이지에 DEMO 배지가
붙습니다. 즉시 동작하고 추가 비용이 없으며, 한 번의 클릭으로 되돌릴 수 있습니다.

아래 스택은 그것과 다릅니다: **실제 Aurora 클러스터를 만들고 요금이 발생합니다.**

## Sample Aurora Clusters (실제 클러스터)

`cdk/` 디렉터리에 테스트용 Aurora PostgreSQL + MySQL 클러스터와 부하 생성기가 포함되어 있습니다.

### 배포 (현재 이 파일들만으로는 실행되지 않음)

> **알려진 제약 (2026-07-24 확인).** 커밋된 파일은 그대로 실행할 수 없습니다.
>
> 1. `samples/cdk/`에 `cdk.json`이 없어 `cdk deploy`가 앱을 찾지 못하고 즉시 실패합니다.
> 2. `app.py`가 `data_stack=None`을 넘기는데 `sample_stack.py`는 `data_stack.vpc`를 읽습니다
>    (`AttributeError`). 이 스택은 메인 Data 스택의 VPC를 공유하도록 설계돼 있어, VPC를
>    별도로 넘겨주는 경로가 필요합니다.
>
> 배포하려면 `cdk.json`을 추가하고 `SampleStack`이 VPC를 받는 방식을 정해야 합니다
> (예: `-c vpc_id=vpc-...` 컨텍스트로 `ec2.Vpc.from_lookup`).
>
> **이미 배포된 `dbops-dev-sample` 스택이 있다면 그 위에 다시 배포하지 마세요.** 기존 스택은
> VPC를 다른 방식으로 참조하던 버전에서 만들어졌고, 참조 방식이 바뀌면 DB 서브넷 그룹이
> 변경되어 클러스터가 교체될 수 있습니다.

```bash
# 위 제약을 해소한 뒤에만 유효
cd samples/cdk
cdk deploy dbops-dev-sample
```

### 정리

Sample 스택은 메인 Data 스택의 VPC를 공유하므로, 전체 환경을 정리할 때 **반드시 Sample을 먼저 삭제**해야 합니다.

```bash
# 1. Sample 먼저 삭제 (VPC 참조 해제)
cd samples/cdk
cdk destroy dbops-dev-sample

# 2. 그 후 메인 스택 삭제
cd ../../cdk
cdk destroy dbops-dev-frontend
cdk destroy dbops-dev-agent
cdk destroy dbops-dev-data
cdk destroy dbops-dev-foundation
```

> **주의**: Sample 삭제 없이 Data 스택을 삭제하면 VPC Export 참조 충돌로 실패합니다.

### 포함 내용

- Aurora PostgreSQL 15 Serverless v2 (0.5-2 ACU)
- Aurora MySQL 3.08 Serverless v2 (0.5-2 ACU)
- Lambda 부하 생성기 (2분 주기로 INSERT/SELECT/JOIN 쿼리 실행)
- 샘플 테이블: orders, customers, inventory (PG) / products, sales (MySQL)

# Samples

테스트/데모용 리소스. DBOps 플랫폼 배포와는 별도로 관리됩니다.

## Sample Aurora Clusters

`cdk/` 디렉터리에 테스트용 Aurora PostgreSQL + MySQL 클러스터와 부하 생성기가 포함되어 있습니다.

### 배포

```bash
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

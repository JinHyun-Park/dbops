# Cognito 인앱(커스텀 UI) 인증 설계 가이드

> DBOps 프론트엔드가 쓰는 로그인 방식을 다른 앱에서 재현하기 위한 참고 문서.
> 에이전트가 이 문서를 읽고 그대로 구현할 수 있도록 핵심 설계 + 실제 코드 + API + IaC를 정리했어요.

## 0. 한 줄 요약

**Cognito를 인증 엔진으로 쓰되, AWS Hosted UI 화면으로 리다이렉트하지 않고 앱이 직접 그린
로그인 폼에서 `amazon-cognito-identity-js`의 SRP 흐름으로 브라우저에서 직접 인증한다.**
로그인 성공 시 Cognito가 발급한 JWT(id/access token)를 `localStorage`에 저장하고, 이후 모든 API
호출에 `Authorization: Bearer <token>`으로 실어 보낸다. 백엔드는 이 JWT를 검증한다.

## 1. 왜 이 방식인가 (Hosted UI vs 인앱 SRP)

| | Hosted UI (리다이렉트) | 인앱 SRP (이 방식) |
| --- | --- | --- |
| 로그인 화면 | AWS가 호스팅하는 별도 도메인 페이지 | 앱이 만든 자체 폼 (`/login`) |
| UX | 앱 → Cognito 도메인 → 콜백 왕복 | 앱 안에서 매끄럽게 처리 |
| 브랜딩/문구/i18n | 제약 큼 | 완전 자유 |
| 비밀번호 노출 | 없음 | 없음 (SRP: 비번이 네트워크에 안 나감) |
| 비번변경/재설정 챌린지 | Cognito 화면이 처리 | 앱이 자체 UI로 처리 |
| 구현 난이도 | 낮음 | 중간 (라이브러리가 대부분 처리) |

핵심: **인증 주체는 100% Cognito.** User Pool에 계정이 없으면 로그인 불가. 화면만 자체 제작.

## 2. 아키텍처 & 데이터 흐름

```
[브라우저: 자체 /login 폼]
   │  email + password 입력
   ▼
amazon-cognito-identity-js  ──(SRP 프로토콜, 비번 평문 미전송)──►  Cognito User Pool
   │                                                                  │
   │  ◄──────────────  id_token / access_token / refresh_token  ──────┘
   ▼
localStorage 저장 (id/access) + 라이브러리가 refresh_token 자체 보관
   │
   ▼
[앱 API 호출]  Authorization: Bearer <access_or_id_token>
   ▼
[백엔드: JWT 검증]  서명/exp 확인 → cognito:username, cognito:groups 클레임으로 인가
```

- **토큰 만료 시**: `refresh_token`으로 **조용히 갱신**(silent refresh). 사용자는 재로그인 불필요.
- **런타임 설정**: 정적 빌드된 프론트가 배포 후 `/config.json`을 fetch해서 pool id / client id /
  region을 주입받는다(빌드타임 env가 아니라 런타임 config → 환경별 재빌드 불필요).

## 3. 구성요소별 책임

| 파일 | 책임 |
| --- | --- |
| `lib/auth.ts` | 인증 코어. pool 생성, `signIn`, 비번 챌린지/재설정, 토큰 저장/조회, silent refresh, JWT 디코드, RBAC 헬퍼 |
| `app/login/page.tsx` | 로그인 폼 + "새 비밀번호 설정" 챌린지 폼(최초 로그인) |
| `components/auth-guard.tsx` | 보호 라우트 게이트. 미인증 시 `/login`으로, 주기적/포커스 시 silent refresh |
| `/config.json` | 런타임 주입: `cognitoClientId`, `cognitoUserPoolId`, `region` |
| CDK `foundation_stack` | User Pool + App Client(SRP 허용) + 도메인 + RBAC 그룹 |
| 백엔드 authorizer | 들어온 JWT 검증 + 클레임 기반 인가 |

## 4. 필수 의존성

```json
// package.json
"dependencies": {
  "amazon-cognito-identity-js": "^6.3.20"
}
```
Amplify 전체는 필요 없음. 이 한 패키지가 SRP/refresh/챌린지를 다 처리한다.

## 5. 핵심 코드 (그대로 참고)

### 5-1. Pool 생성 — 런타임 config에서 주입

```ts
import { CognitoUserPool, CognitoUser, AuthenticationDetails } from "amazon-cognito-identity-js";

let poolPromise: Promise<CognitoUserPool> | null = null;

async function getPool(): Promise<CognitoUserPool> {
  if (poolPromise) return poolPromise;
  poolPromise = (async () => {
    // 배포 후 정적 프론트가 fetch하는 런타임 설정
    const cfg = await fetch("/config.json", { cache: "no-store" }).then(r => r.json());
    const userPoolId = cfg.cognitoUserPoolId || process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
    const clientId   = cfg.cognitoClientId   || process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID;
    if (!userPoolId || !clientId) throw new Error("Cognito config missing in /config.json");
    return new CognitoUserPool({ UserPoolId: userPoolId, ClientId: clientId });
  })();
  return poolPromise;
}
```

### 5-2. 로그인 (SRP) + 최초 로그인 비번 챌린지

`authenticateUser`가 내부적으로 SRP를 수행한다. 관리자 초대 계정(임시 비번)은 첫 로그인 시
`newPasswordRequired` 콜백이 뜨므로, **같은 CognitoUser 인스턴스**에서 `completeNewPasswordChallenge`로
이어줘야 dead-end가 안 난다.

```ts
export type SignInResult =
  | ({ status: "ok" } & Tokens)
  | { status: "new_password_required"; complete: (newPassword: string) => Promise<Tokens> };

export async function signIn(email: string, password: string): Promise<SignInResult> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  const auth = new AuthenticationDetails({ Username: email, Password: password });
  return new Promise((resolve, reject) => {
    user.authenticateUser(auth, {
      onSuccess: (session) => {
        const idToken = session.getIdToken().getJwtToken();
        const accessToken = session.getAccessToken().getJwtToken();
        setTokens(idToken, accessToken);
        resolve({ status: "ok", id_token: idToken, access_token: accessToken });
      },
      onFailure: (err) => reject(err),
      newPasswordRequired: () => {
        // 같은 user 인스턴스에 바인딩한 continuation을 돌려준다.
        // 두번째 인자 {}: pool 필수속성(email)은 admin 생성 시 이미 세팅됐고
        // email/email_verified는 immutable이라 되돌려주면 에러난다.
        resolve({
          status: "new_password_required",
          complete: (newPassword) => new Promise<Tokens>((res, rej) => {
            user.completeNewPasswordChallenge(newPassword, {}, {
              onSuccess: (session) => {
                const idToken = session.getIdToken().getJwtToken();
                const accessToken = session.getAccessToken().getJwtToken();
                setTokens(idToken, accessToken);
                res({ id_token: idToken, access_token: accessToken });
              },
              onFailure: (err) => rej(err),
            });
          }),
        });
      },
    });
  });
}
```

### 5-3. 토큰 저장/조회

```ts
export function setTokens(idToken: string, accessToken: string) {
  localStorage.setItem("app_id_token", idToken);
  localStorage.setItem("app_access_token", accessToken);
}
export function getToken()       { return localStorage.getItem("app_id_token"); }
export function getAccessToken() { return localStorage.getItem("app_access_token"); }
export function clearTokens() {
  localStorage.removeItem("app_id_token");
  localStorage.removeItem("app_access_token");
}
```
> 주의: `amazon-cognito-identity-js`는 **refresh_token을 자체 localStorage 키
> (`CognitoIdentityServiceProvider.*`)에 따로 저장**한다. 그래서 silent refresh가 가능하다.
> 위 커스텀 키(app_id_token 등)는 앱이 "빠르게 읽으려고" 복제해둔 캐시일 뿐.

### 5-4. Silent refresh (토큰 만료 대응)

```ts
// exp가 이 창(초) 안이면 미리 갱신 → 긴 요청이 중간에 401 안 나게
const REFRESH_WINDOW_SECONDS = 120;

function secondsUntilExpiry(token: string | null): number | null {
  if (!token) return null;
  try {
    const { exp } = JSON.parse(atob(token.split(".")[1]));
    return exp ? exp - Math.floor(Date.now() / 1000) : null;
  } catch { return null; }
}

export async function refreshSession(): Promise<boolean> {
  const pool = await getPool();
  const user = pool.getCurrentUser();          // 라이브러리가 보관한 세션 사용
  if (!user) return false;
  return new Promise((resolve) => {
    user.getSession((err: any, session: any) => {  // refresh_token으로 자동 회전
      if (err || !session?.isValid()) return resolve(false);
      setTokens(session.getIdToken().getJwtToken(), session.getAccessToken().getJwtToken());
      resolve(true);
    });
  });
}

// API 호출 직전에 쓰는 헬퍼: 유효한 access token 보장(없으면 null → /login)
export async function getValidAccessToken(): Promise<string | null> {
  const cached = getAccessToken();
  const left = secondsUntilExpiry(cached);
  if (left !== null && left > REFRESH_WINDOW_SECONDS) return cached;
  return (await refreshSession()) ? getAccessToken() : null;
}
```

### 5-5. 비밀번호 재설정 (이메일 코드)

```ts
export async function requestPasswordReset(email: string): Promise<void> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  return new Promise((resolve, reject) => {
    user.forgotPassword({
      onSuccess: () => resolve(),
      onFailure: (err) => reject(err),
      inputVerificationCode: () => resolve(),  // 코드 발송됨 → 입력 화면으로
    });
  });
}
export async function confirmPasswordReset(email: string, code: string, newPassword: string) {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  return new Promise<void>((resolve, reject) => {
    user.confirmPassword(code, newPassword, { onSuccess: () => resolve(), onFailure: reject });
  });
}
```

### 5-6. RBAC (선택) — id_token의 `cognito:groups`

```ts
function decodeJwt(token: string | null): any {
  if (!token) return null;
  try { return JSON.parse(atob(token.split(".")[1])); } catch { return null; }
}
export function getUserGroups(): string[] {
  return decodeJwt(getToken())?.["cognito:groups"] ?? [];
}
// 예: viewer 그룹이면 제한. 단 이건 화면용 cosmetic 게이트일 뿐,
// 실제 권한은 반드시 서버가 클레임으로 재검증해야 한다.
```

### 5-7. AuthGuard — 보호 라우트

```tsx
"use client";
const PUBLIC_PATHS = ["/login", "/forgot", "/reset", "/callback"];
const REFRESH_INTERVAL_MS = 45 * 60 * 1000;  // 45분마다 백그라운드 refresh

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const [checked, setChecked] = useState(false);
  const [authed, setAuthed] = useState(false);
  const pathname = usePathname() || "/";
  const router = useRouter();

  useEffect(() => {
    if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) { setChecked(true); return; }
    let cancelled = false;
    (async () => {
      if (isLoggedIn()) { setAuthed(true); setChecked(true); return; }
      const ok = await refreshSession();               // >1h 유휴 탭 복구
      if (cancelled) return;
      if (ok) { setAuthed(true); setChecked(true); }
      else router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    })();
    return () => { cancelled = true; };
  }, [pathname, router]);

  // 주기적 + 포커스 시 refresh
  useEffect(() => {
    if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) return;
    const tick = async () => {
      if (!(await refreshSession())) {
        clearTokens();
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      }
    };
    const id = window.setInterval(tick, REFRESH_INTERVAL_MS);
    window.addEventListener("focus", tick);
    return () => { window.clearInterval(id); window.removeEventListener("focus", tick); };
  }, [pathname, router]);

  if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) return <>{children}</>;
  if (!checked) return <div>Loading…</div>;
  if (!authed) return null;
  return <>{children}</>;
}
```
루트 레이아웃에서 `<AuthGuard>{children}</AuthGuard>`로 앱 전체를 감싼다.

### 5-8. API 클라이언트에 토큰 실어보내기

```ts
export async function authedFetch(path: string, init: RequestInit = {}) {
  const token = await getValidAccessToken();          // 만료 임박 시 자동 refresh
  if (!token) { window.location.href = "/login"; throw new Error("not authenticated"); }
  const cfg = await fetch("/config.json").then(r => r.json());
  return fetch(`${cfg.apiUrl}${path}`, {
    ...init,
    headers: { ...(init.headers || {}), Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  });
}
```

## 6. 사용하는 Cognito / 라이브러리 API 총정리

**`amazon-cognito-identity-js` (브라우저, SRP)**
- `new CognitoUserPool({ UserPoolId, ClientId })` — pool 핸들
- `new CognitoUser({ Username, Pool })` / `new AuthenticationDetails({ Username, Password })`
- `user.authenticateUser(details, { onSuccess, onFailure, newPasswordRequired })` — **SRP 로그인**
- `user.completeNewPasswordChallenge(newPw, {}, callbacks)` — 최초 로그인 비번 설정
- `user.getSession(cb)` — refresh_token으로 세션 갱신(silent refresh)
- `user.forgotPassword({...})` / `user.confirmPassword(code, newPw, {...})` — 비번 재설정
- `pool.getCurrentUser()` — 라이브러리가 보관한 현재 사용자
- `session.getIdToken()/getAccessToken().getJwtToken()` — JWT 추출

**Cognito 내부 인증 플로우(App Client에 켜야 함)**
- `USER_SRP_AUTH` — 위 authenticateUser가 쓰는 흐름 (**필수**)
- `USER_PASSWORD_AUTH` — CLI/스모크테스트에서 비번으로 토큰 받을 때(선택)

**관리자용 (백엔드/CLI, boto3 `cognito-idp`)** — 계정 프로비저닝
- `admin_create_user(--message-action SUPPRESS)` — 초대 계정 생성
- `admin_set_user_password(--permanent)` — 영구 비번 설정(챌린지 스킵)
- `admin_get_user` — 상태 확인(CONFIRMED / FORCE_CHANGE_PASSWORD)

## 7. IaC (CDK) — User Pool + App Client

```python
self.user_pool = cognito.UserPool(
    self, "UserPool",
    user_pool_name=f"myapp-{env}",
    self_sign_up_enabled=False,                    # 공개 가입 없음(초대제)
    sign_in_aliases=cognito.SignInAliases(email=True),   # username = email
    password_policy=cognito.PasswordPolicy(
        min_length=8, require_lowercase=True, require_uppercase=True,
        require_digits=True, require_symbols=True,
    ),
    removal_policy=cdk.RemovalPolicy.DESTROY,
)

self.user_pool_client = self.user_pool.add_client(
    "WebClient",
    auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),  # SRP 필수
    prevent_user_existence_errors=True,   # 사용자 열거(enumeration) 방지 — 반드시 켜기
    generate_secret=False,                # SPA는 secret 없음(client id가 공개 번들에 노출)
    access_token_validity=cdk.Duration.hours(12),   # 세션 길이 취향껏
    id_token_validity=cdk.Duration.hours(12),
    o_auth=cognito.OAuthSettings(         # Hosted UI 안 써도 무해. 순수 인앱이면 생략 가능
        flows=cognito.OAuthFlows(authorization_code_grant=True),
        scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
        callback_urls=[...],
    ),
)

# (선택) RBAC 그룹
cognito.CfnUserPoolGroup(self, "AdminGroup", user_pool_id=self.user_pool.user_pool_id, group_name="myapp-admin")

# 프론트가 읽을 값 출력 → 배포 후 /config.json에 심는다
cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
cdk.CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
```

**핵심 설정 이유**
- `user_srp=True`: 인앱 SRP 로그인의 전제. 이게 없으면 `authenticateUser`가 실패한다.
- `generate_secret=False`: SPA는 client secret을 안전히 보관할 수 없다(번들에 노출). 반드시 secretless.
- `prevent_user_existence_errors=True`: 없으면 에러 코드만으로 "이 이메일 가입됨?"이 새어나가
  익명 사용자 열거가 가능. **보안상 필수.**
- `self_sign_up_enabled=False` + admin_create_user: 초대제 운영이면 공개 가입을 끈다.

## 8. 백엔드 JWT 검증

각 API가 `Authorization: Bearer <jwt>`를 받아 검증한다. 두 가지 방식:

1. **API Gateway Cognito/JWT Authorizer** (권장, 관리형): API GW가 서명·exp·audience를 자동 검증.
   핸들러는 통과된 요청의 클레임만 읽으면 됨.
2. **핸들러 내 직접 검증**: JWKS(`https://cognito-idp.<region>.amazonaws.com/<poolId>/.well-known/jwks.json`)로
   서명 검증 후 클레임 사용. DBOps는 테넌시/그룹 판별에 클레임을 직접 읽는다:
   - `cognito:username` (또는 `sub`) → 사용자 식별
   - `cognito:groups` → RBAC 인가 (프론트 게이트는 cosmetic, **서버가 authoritative**)

```python
# 예: 최소 검증 개념 (실서비스는 JWKS 서명검증까지 반드시)
import base64, json
def decode_claims(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))
# username = claims.get("cognito:username") or claims.get("sub")
# groups   = claims.get("cognito:groups") or []
```

## 9. 계정 프로비저닝 (초대제 운영)

공개 가입이 없으므로 관리자가 만든다:
```bash
POOL=us-east-1_xxxx
aws cognito-idp admin-create-user --user-pool-id $POOL \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
# 임시 비번으로 두면 첫 로그인 시 newPasswordRequired 챌린지가 뜬다(§5-2가 처리).
# 바로 쓸 영구 비번을 줄 거면:
aws cognito-idp admin-set-user-password --user-pool-id $POOL \
  --username user@example.com --password 'Passw0rd!' --permanent
```
비번 정책(대·소·숫자·기호·8자+)을 반드시 만족해야 한다.

## 10. 구현 체크리스트 (에이전트용)

- [ ] `amazon-cognito-identity-js` 설치
- [ ] CDK: UserPool + App Client(`user_srp=True`, `generate_secret=False`, `prevent_user_existence_errors=True`) + CfnOutput
- [ ] 배포 후 UserPoolId/ClientId/region을 프론트 `/config.json`에 주입(런타임 fetch)
- [ ] `lib/auth.ts`: getPool / signIn(+비번챌린지) / setTokens·getToken / refreshSession / getValidAccessToken / forgot·confirm / RBAC 헬퍼
- [ ] `app/login/page.tsx`: 로그인 폼 + "새 비밀번호 설정" 챌린지 폼 + (선택) forgot/reset
- [ ] `components/auth-guard.tsx`: 보호 라우트 + 주기/포커스 silent refresh, 루트 레이아웃에서 앱 감싸기
- [ ] API 클라이언트: `getValidAccessToken()` → `Authorization: Bearer` 부착
- [ ] 백엔드: API GW JWT Authorizer(또는 JWKS 서명검증) + 클레임 기반 인가(서버가 authoritative)
- [ ] 관리자 계정 프로비저닝(admin_create_user)로 첫 사용자 생성 → 브라우저 로그인 검증

## 11. 흔한 함정

- **`user_srp` 미설정** → `authenticateUser`가 `NotAuthorizedException`/flow 에러. SRP 플로우 켜기.
- **`generate_secret=True`** → SPA에서 `SECRET_HASH` 요구로 로그인 실패. secretless여야 함.
- **비번 챌린지에서 새 user 인스턴스 생성** → 챌린지 세션이 끊김. **동일 인스턴스**에서 complete.
- **`completeNewPasswordChallenge`에 email 되돌려줌** → immutable 속성 에러. 필수 attr는 `{}`로.
- **빌드타임 env로 pool id 고정** → 환경마다 재빌드 필요. `/config.json` 런타임 주입으로 회피.
- **프론트 RBAC만 믿음** → 우회 가능. 서버가 클레임으로 반드시 재검증.
- **refresh_token 수동 관리 시도** → 불필요. 라이브러리가 자체 키로 보관, `getSession()`이 회전.
```

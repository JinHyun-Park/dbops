interface AuthConfig {
  cognitoDomain: string;
  cognitoClientId: string;
  region: string;
}

let authConfigPromise: Promise<AuthConfig> | null = null;

function loadAuthConfig(): Promise<AuthConfig> {
  if (authConfigPromise) return authConfigPromise;
  authConfigPromise = (async () => {
    const fallback: AuthConfig = {
      cognitoDomain:
        process.env.NEXT_PUBLIC_COGNITO_DOMAIN_URL ||
        `https://${process.env.NEXT_PUBLIC_COGNITO_DOMAIN || ""}.auth.${
          process.env.NEXT_PUBLIC_COGNITO_REGION || ""
        }.amazoncognito.com`,
      cognitoClientId: process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "",
      region: process.env.NEXT_PUBLIC_COGNITO_REGION || "",
    };
    if (typeof window === "undefined") return fallback;
    try {
      const res = await fetch("/config.json", { cache: "no-store" });
      if (res.ok) {
        const cfg = await res.json();
        return {
          cognitoDomain: cfg.cognitoDomain
            ? cfg.cognitoDomain.startsWith("http")
              ? cfg.cognitoDomain
              : `https://${cfg.cognitoDomain}`
            : fallback.cognitoDomain,
          cognitoClientId: cfg.cognitoClientId || fallback.cognitoClientId,
          region: cfg.region || fallback.region,
        };
      }
    } catch {
      // fall through
    }
    return fallback;
  })();
  return authConfigPromise;
}

function redirectUri(): string {
  if (typeof window !== "undefined")
    return `${window.location.origin}/callback`;
  return "http://localhost:3000/callback";
}

export async function getLoginUrl(): Promise<string> {
  const cfg = await loadAuthConfig();
  return `${cfg.cognitoDomain}/login?client_id=${
    cfg.cognitoClientId
  }&response_type=token&scope=openid+profile&redirect_uri=${encodeURIComponent(
    redirectUri(),
  )}`;
}

export async function getLogoutUrl(): Promise<string> {
  const cfg = await loadAuthConfig();
  const origin = typeof window !== "undefined" ? window.location.origin : "/";
  return `${cfg.cognitoDomain}/logout?client_id=${
    cfg.cognitoClientId
  }&logout_uri=${encodeURIComponent(origin)}`;
}

export function parseTokensFromHash(): {
  id_token: string;
  access_token: string;
} | null {
  if (typeof window === "undefined") return null;
  const hash = window.location.hash.substring(1);
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const idToken = params.get("id_token");
  const accessToken = params.get("access_token");
  if (idToken && accessToken)
    return { id_token: idToken, access_token: accessToken };
  return null;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("dbops_id_token");
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("dbops_access_token");
}

export function setTokens(idToken: string, accessToken: string): void {
  localStorage.setItem("dbops_id_token", idToken);
  localStorage.setItem("dbops_access_token", accessToken);
  // Notify auth-aware singletons (e.g. the alert-stream WS client) that a fresh
  // session is available so they can (re)connect. Decoupled via a DOM event to
  // avoid an import cycle (alert-stream.ts already imports from this module).
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("dbops:auth-login"));
  }
}

export function clearTokens(): void {
  localStorage.removeItem("dbops_id_token");
  localStorage.removeItem("dbops_access_token");
  // On logout/revocation, tell auth-aware singletons to tear down. Without this,
  // the alert-stream WebSocket (authorized only at $connect) would survive up to
  // its 2h TTL, so a logged-out user keeps receiving pushed alerts.
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("dbops:auth-logout"));
  }
}

// Decode a JWT (no signature verification — exp/iat only).
// Returns null on malformed input.
function decodeJwt(
  token: string | null,
): { exp?: number; iat?: number; email?: string } | null {
  if (!token) return null;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload;
  } catch {
    return null;
  }
}

// Seconds until the token expires. Negative if already expired. null if undecodable.
function secondsUntilExpiry(token: string | null): number | null {
  const claims = decodeJwt(token);
  if (!claims?.exp) return null;
  return claims.exp - Math.floor(Date.now() / 1000);
}

// We refresh proactively if the token expires within this window so a long
// request can complete without 401-ing mid-flight.
const REFRESH_WINDOW_SECONDS = 120;

export function isLoggedIn(): boolean {
  // Treat already-expired tokens as not logged in. The background refresher
  // in AuthGuard will swap them out if it can; otherwise the user is bounced
  // to /login by the same effect.
  const left = secondsUntilExpiry(getToken());
  if (left === null) return false;
  return left > 0;
}

// --- SRP-based in-app auth (no Hosted UI redirect) ---

import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserAttribute,
} from "amazon-cognito-identity-js";

interface PoolConfig {
  userPoolId: string;
  clientId: string;
}

let poolPromise: Promise<CognitoUserPool> | null = null;

function _poolConfigFromConfig(cfg: {
  cognitoClientId?: string;
  userPoolId?: string;
  cognitoDomain?: string;
  region?: string;
}): PoolConfig | null {
  const clientId = cfg.cognitoClientId;
  let userPoolId = cfg.userPoolId;
  // Hosted-domain URLs don't carry pool id; fall back to env var if config.json
  // hasn't been extended yet. Stack outputs the pool id; user can pin via NEXT_PUBLIC.
  if (!userPoolId && typeof process !== "undefined") {
    userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
  }
  if (!clientId || !userPoolId) return null;
  return { userPoolId, clientId };
}

async function getPool(): Promise<CognitoUserPool> {
  if (poolPromise) return poolPromise;
  poolPromise = (async () => {
    const cfg = await loadAuthConfig();
    // We pass userPoolId via the existing auth config; if missing, throw early.
    const extended = await (async () => {
      try {
        if (typeof window === "undefined") return null;
        const res = await fetch("/config.json", { cache: "no-store" });
        if (!res.ok) return null;
        return await res.json();
      } catch {
        return null;
      }
    })();
    const pc = _poolConfigFromConfig({
      cognitoClientId: cfg.cognitoClientId,
      userPoolId: extended?.cognitoUserPoolId,
      cognitoDomain: cfg.cognitoDomain,
      region: cfg.region,
    });
    if (!pc)
      throw new Error(
        "Cognito client id / user pool id missing — check /config.json",
      );
    return new CognitoUserPool({
      UserPoolId: pc.userPoolId,
      ClientId: pc.clientId,
    });
  })();
  return poolPromise;
}

export interface Tokens {
  id_token: string;
  access_token: string;
}

// signIn resolves to one of two outcomes (it only rejects on a real failure):
//   - status "ok": authenticated, tokens stored.
//   - status "new_password_required": the account is in FORCE_CHANGE_PASSWORD
//     (admin-created user with a temporary password). The caller must collect a
//     new password and call complete() to finish the challenge on the SAME
//     CognitoUser instance — otherwise the invited user's first login dead-ends.
export type SignInResult =
  | ({ status: "ok" } & Tokens)
  | {
      status: "new_password_required";
      complete: (newPassword: string) => Promise<Tokens>;
    };

export async function signIn(
  email: string,
  password: string,
): Promise<SignInResult> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  const auth = new AuthenticationDetails({
    Username: email,
    Password: password,
  });
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
        // Resolve with a continuation bound to THIS user instance. Pass {} as
        // required attributes: the pool's required attr (email) is already set
        // at admin-creation time, and email/email_verified are immutable —
        // passing them back errors. Extend only if the pool genuinely requires
        // a NEW attribute at first login.
        resolve({
          status: "new_password_required",
          complete: (newPassword: string) =>
            new Promise<Tokens>((res, rej) => {
              user.completeNewPasswordChallenge(
                newPassword,
                {},
                {
                  onSuccess: (session) => {
                    const idToken = session.getIdToken().getJwtToken();
                    const accessToken = session.getAccessToken().getJwtToken();
                    setTokens(idToken, accessToken);
                    res({ id_token: idToken, access_token: accessToken });
                  },
                  onFailure: (err) => rej(err),
                },
              );
            }),
        });
      },
    });
  });
}

export async function requestPasswordReset(email: string): Promise<void> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  return new Promise((resolve, reject) => {
    user.forgotPassword({
      onSuccess: () => resolve(),
      onFailure: (err) => reject(err),
      inputVerificationCode: () => resolve(),
    });
  });
}

export async function confirmPasswordReset(
  email: string,
  code: string,
  newPassword: string,
): Promise<void> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  return new Promise((resolve, reject) => {
    user.confirmPassword(code, newPassword, {
      onSuccess: () => resolve(),
      onFailure: (err) => reject(err),
    });
  });
}

export function getUserFromToken(): { email?: string; sub?: string } | null {
  const claims = decodeJwt(getToken()) as {
    email?: string;
    sub?: string;
  } | null;
  return claims ? { email: claims.email, sub: claims.sub } : null;
}

// --- RBAC ---
// `cognito:groups` is present on the ID token. Default model: a user is
// admin if they have no group claim (single-admin deploys) or are in
// dbops-admin; any other group set (dbops-viewer or otherwise) is denied.
// This keeps existing single-admin deployments working without a migration.
// Cosmetic gate only — the server's _is_admin is authoritative.
export function getUserGroups(): string[] {
  const claims = decodeJwt(getToken()) as {
    "cognito:groups"?: string[];
  } | null;
  const g = claims?.["cognito:groups"];
  return Array.isArray(g) ? g : [];
}

// The pool's Cognito username is a UUID (== sub); email is a display
// attribute. Used to disable the acting admin's own role control.
export function getUsername(): string | null {
  const claims = decodeJwt(getToken()) as {
    "cognito:username"?: string;
    sub?: string;
  } | null;
  return claims?.["cognito:username"] || claims?.sub || null;
}

export function isAdmin(): boolean {
  const groups = getUserGroups();
  // Deny if a group set is present but lacks dbops-admin; empty groups (no
  // claim) stays admin (single-admin default). Cosmetic gate only — the
  // server enforces.
  if (groups.length > 0 && !groups.includes("dbops-admin")) return false;
  return true;
}

export function isViewer(): boolean {
  return !isAdmin();
}

// --- Silent refresh ---
//
// Cognito access tokens expire after ~1 hour. Without refresh, the user is
// kicked back to /login mid-session. amazon-cognito-identity-js keeps the
// refresh token under its own localStorage keys (CognitoIdentityServiceProvider.*),
// so calling `cognitoUser.getSession()` reads it and silently rotates tokens
// when the cached session has expired.

let refreshPromise: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const pool = await getPool();
      const user = pool.getCurrentUser();
      if (!user) return false;
      return await new Promise<boolean>((resolve) => {
        user.getSession(
          (
            err: Error | null,
            session: {
              getIdToken(): { getJwtToken(): string };
              getAccessToken(): { getJwtToken(): string };
              isValid(): boolean;
            } | null,
          ) => {
            if (err || !session || !session.isValid()) {
              resolve(false);
              return;
            }
            setTokens(
              session.getIdToken().getJwtToken(),
              session.getAccessToken().getJwtToken(),
            );
            resolve(true);
          },
        );
      });
    } catch {
      return false;
    } finally {
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

// Returns a valid AccessToken. Refreshes silently if the cached one is within
// REFRESH_WINDOW_SECONDS of expiry or already expired. Returns null if no
// refresh is possible (no current user, refresh token also expired). Callers
// MUST handle null by bouncing the user to /login.
export async function getValidAccessToken(): Promise<string | null> {
  const cached = getAccessToken();
  const left = secondsUntilExpiry(cached);
  if (left !== null && left > REFRESH_WINDOW_SECONDS) return cached;
  const ok = await refreshSession();
  if (!ok) return null;
  return getAccessToken();
}

// Same as getValidAccessToken but returns the ID token (some APIs prefer it
// for the email claim).
export async function getValidIdToken(): Promise<string | null> {
  const cached = getToken();
  const left = secondsUntilExpiry(cached);
  if (left !== null && left > REFRESH_WINDOW_SECONDS) return cached;
  const ok = await refreshSession();
  if (!ok) return null;
  return getToken();
}

// Re-export for AuthGuard's background timer.
export { refreshSession };

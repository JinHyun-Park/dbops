const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "dbops-dev";
const COGNITO_REGION = process.env.NEXT_PUBLIC_COGNITO_REGION || "ap-northeast-2";
const CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "h587q0bq8vtmd6fdpg76trapb";
const REDIRECT_URI = process.env.NEXT_PUBLIC_REDIRECT_URI || (
  typeof window !== "undefined" ? `${window.location.origin}/callback` : "http://localhost:3000/callback"
);

const AUTH_BASE = `https://${COGNITO_DOMAIN}.auth.${COGNITO_REGION}.amazoncognito.com`;

export function getLoginUrl(): string {
  return `${AUTH_BASE}/login?client_id=${CLIENT_ID}&response_type=code&scope=openid+profile&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`;
}

export function getLogoutUrl(): string {
  return `${AUTH_BASE}/logout?client_id=${CLIENT_ID}&logout_uri=${encodeURIComponent(typeof window !== "undefined" ? window.location.origin : "/")}`;
}

export async function exchangeCodeForTokens(code: string): Promise<{ id_token: string; access_token: string; refresh_token?: string }> {
  const res = await fetch(`${AUTH_BASE}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      code,
      redirect_uri: REDIRECT_URI,
    }),
  });
  if (!res.ok) throw new Error(`Token exchange failed: ${res.status}`);
  return res.json();
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("dbops_id_token");
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("dbops_access_token");
}

export function setTokens(idToken: string, accessToken: string, refreshToken?: string): void {
  localStorage.setItem("dbops_id_token", idToken);
  localStorage.setItem("dbops_access_token", accessToken);
  if (refreshToken) localStorage.setItem("dbops_refresh_token", refreshToken);
}

export function clearTokens(): void {
  localStorage.removeItem("dbops_id_token");
  localStorage.removeItem("dbops_access_token");
  localStorage.removeItem("dbops_refresh_token");
}

export function isLoggedIn(): boolean {
  return !!getToken();
}

export function getUserFromToken(): { email?: string; sub?: string } | null {
  const token = getToken();
  if (!token) return null;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return { email: payload.email, sub: payload.sub };
  } catch {
    return null;
  }
}

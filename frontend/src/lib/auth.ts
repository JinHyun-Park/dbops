const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "dbops-dev";
const COGNITO_REGION = process.env.NEXT_PUBLIC_COGNITO_REGION || "ap-northeast-2";
const CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "h587q0bq8vtmd6fdpg76trapb";

function getRedirectUri(): string {
  if (typeof window !== "undefined") return `${window.location.origin}/callback`;
  return "http://localhost:3000/callback";
}

const AUTH_BASE = `https://${COGNITO_DOMAIN}.auth.${COGNITO_REGION}.amazoncognito.com`;

export function getLoginUrl(): string {
  return `${AUTH_BASE}/login?client_id=${CLIENT_ID}&response_type=token&scope=openid+profile&redirect_uri=${encodeURIComponent(getRedirectUri())}`;
}

export function getLogoutUrl(): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "/";
  return `${AUTH_BASE}/logout?client_id=${CLIENT_ID}&logout_uri=${encodeURIComponent(origin)}`;
}

export function parseTokensFromHash(): { id_token: string; access_token: string } | null {
  if (typeof window === "undefined") return null;
  const hash = window.location.hash.substring(1);
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const idToken = params.get("id_token");
  const accessToken = params.get("access_token");
  if (idToken && accessToken) return { id_token: idToken, access_token: accessToken };
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
}

export function clearTokens(): void {
  localStorage.removeItem("dbops_id_token");
  localStorage.removeItem("dbops_access_token");
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

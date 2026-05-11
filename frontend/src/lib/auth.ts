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
  if (typeof window !== "undefined") return `${window.location.origin}/callback`;
  return "http://localhost:3000/callback";
}

export async function getLoginUrl(): Promise<string> {
  const cfg = await loadAuthConfig();
  return `${cfg.cognitoDomain}/login?client_id=${cfg.cognitoClientId}&response_type=token&scope=openid+profile&redirect_uri=${encodeURIComponent(redirectUri())}`;
}

export async function getLogoutUrl(): Promise<string> {
  const cfg = await loadAuthConfig();
  const origin = typeof window !== "undefined" ? window.location.origin : "/";
  return `${cfg.cognitoDomain}/logout?client_id=${cfg.cognitoClientId}&logout_uri=${encodeURIComponent(origin)}`;
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

const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "";
const CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "";
const REDIRECT_URI = process.env.NEXT_PUBLIC_REDIRECT_URI || "http://localhost:3000/callback";

export function getLoginUrl(): string {
  return `https://${COGNITO_DOMAIN}.auth.ap-northeast-2.amazoncognito.com/login?client_id=${CLIENT_ID}&response_type=code&scope=openid+profile&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("dbops_token");
}

export function setToken(token: string): void {
  localStorage.setItem("dbops_token", token);
}

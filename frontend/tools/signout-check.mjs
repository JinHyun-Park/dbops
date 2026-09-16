#!/usr/bin/env node
/**
 * clearTokens() must clear the REFRESH token, not just the two dbops_* keys.
 *
 * Why this file exists rather than only an E2E spec: the E2E can only reproduce
 * the bypass when the cached storage state happens to carry a Cognito cache, so
 * it skips otherwise, and a gate that skips is not a gate. This one always runs
 * and is deterministic.
 *
 * The bypass it pins shut: amazon-cognito-identity-js keeps its own cache under
 * CognitoIdentityServiceProvider.<clientId>.<username>.*, refreshToken
 * included. Leaving it behind let refreshSession() mint a fresh session from
 * LastAuthUser with no credentials, for the refresh token's whole lifetime.
 *
 * Run: node tools/signout-check.mjs   (or: npm run signout:check)
 */
import { clearTokens } from "../src/lib/auth.ts";

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
}

/** Minimal localStorage: Object.keys() over it must work, which is what the
 *  implementation iterates. */
function fakeStorage(seed) {
  const store = new Map(Object.entries(seed));
  return new Proxy(
    {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      get length() {
        return store.size;
      },
    },
    {
      ownKeys: () => [...store.keys()],
      getOwnPropertyDescriptor: () => ({
        enumerable: true,
        configurable: true,
      }),
      has: (t, k) => k in t || store.has(k),
      get: (t, k) => (k in t ? t[k] : store.get(k)),
    },
  );
}

const CLIENT = "4abcdef0123456789";
const SEED = {
  dbops_id_token: "id.jwt",
  dbops_access_token: "access.jwt",
  [`CognitoIdentityServiceProvider.${CLIENT}.LastAuthUser`]: "e2e@dbops.dev",
  [`CognitoIdentityServiceProvider.${CLIENT}.e2e@dbops.dev.refreshToken`]:
    "REFRESH-SECRET",
  [`CognitoIdentityServiceProvider.${CLIENT}.e2e@dbops.dev.idToken`]: "id.jwt",
  [`CognitoIdentityServiceProvider.${CLIENT}.e2e@dbops.dev.accessToken`]:
    "access.jwt",
  [`CognitoIdentityServiceProvider.${CLIENT}.e2e@dbops.dev.clockDrift`]: "0",
  // Unrelated per-viewer state. A sign-out that wipes these is too broad: the
  // locale and the saved views are not credentials.
  "dbops.locale": "ko",
  "dbops.tasks.publishedMark": "1757000000000",
  "dbops.selectedCluster": "pgtsd-demo-aurora-pg",
};

const storage = fakeStorage(SEED);
let loggedOut = 0;
globalThis.localStorage = storage;
globalThis.window = {
  dispatchEvent: (e) => {
    if (e?.type === "dbops:auth-logout") loggedOut++;
  },
};
globalThis.Event = class {
  constructor(type) {
    this.type = type;
  }
};

clearTokens();

const left = Object.keys(storage);
check(
  "dbops tokens cleared",
  !left.some((k) => k.startsWith("dbops_")),
  left.filter((k) => k.startsWith("dbops_")).join(", "),
);
check(
  "refresh token cleared",
  !left.some((k) => k.includes("refreshToken")),
  "a surviving refreshToken re-mints a session with no credentials",
);
check(
  "whole Cognito cache cleared",
  !left.some((k) => k.startsWith("CognitoIdentityServiceProvider.")),
  left
    .filter((k) => k.startsWith("CognitoIdentityServiceProvider."))
    .join(", "),
);
check(
  "LastAuthUser cleared",
  !left.some((k) => k.endsWith("LastAuthUser")),
  "pool.getCurrentUser() resolves from this key",
);
// The other direction, so the fix cannot be "clear everything".
check("locale preserved", storage.getItem("dbops.locale") === "ko");
check(
  "publication watermark preserved",
  storage.getItem("dbops.tasks.publishedMark") === "1757000000000",
);
check(
  "selected cluster preserved",
  storage.getItem("dbops.selectedCluster") === "pgtsd-demo-aurora-pg",
);
check("logout event dispatched once", loggedOut === 1, `got ${loggedOut}`);

if (failures) {
  console.error(`signout-check: ${failures} failure(s)`);
  process.exit(1);
}
console.log("signout-check: ok");

import type { APIRequestContext, Page } from "@playwright/test";

/** Where the frontend keeps the refresh token every tab shares. */
export const REFRESH_KEY = "musizerr.refresh_token";

export interface Account {
  username: string;
  password: string;
}

export function account(): Account {
  const username = process.env.E2E_USERNAME ?? "e2e";
  const password = process.env.E2E_PASSWORD;
  if (!password) throw new Error("E2E_PASSWORD is not set: run the suite with e2e/run.sh");
  return { username, password };
}

/** The throwaway administrator (e2e/run.sh makes it); null when the suite was started without one. */
export function adminAccount(): Account | null {
  const password = process.env.E2E_ADMIN_PASSWORD;
  return password ? { username: process.env.E2E_ADMIN_USERNAME ?? "e2e-admin", password } : null;
}

/** Signs `who` (the e2e user by default) in through the API; both tokens of the new session. */
export async function login(request: APIRequestContext, who: Account = account()): Promise<{ access_token: string; refresh_token: string }> {
  const response = await request.post("/api/auth/account/login", { data: who });
  if (!response.ok()) throw new Error(`sign-in failed: ${response.status()} ${await response.text()}`);
  return response.json();
}

/** Signs `who` (the e2e user by default) in through the API; its refresh token. */
export async function signIn(request: APIRequestContext, who: Account = account()): Promise<string> {
  return (await login(request, who)).refresh_token;
}

/** What the session did on a page: every refresh answer, and sign-outs. */
export function watchSession(page: Page) {
  const refreshes: number[] = [];
  const signOuts: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/auth/account/refresh")) refreshes.push(response.status());
  });
  page.on("request", (request) => {
    if (request.method() === "DELETE" && request.url().includes("/api/auth/account/stream-session")) {
      signOuts.push(request.url());
    }
  });
  return { refreshes, signOuts };
}

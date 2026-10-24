import { providerLoginStartUrl } from "./api-client";
import { classifyError } from "./api-errors";
import { safeRedirect } from "./safe-redirect";

/**
 * Signing in through a provider leaves the app (the authorization is on the
 * provider's site) and comes back to a page that has lost where the viewer was
 * going: the target waits in sessionStorage for the way back. Storage may be
 * blocked or full; the sign-in then simply lands on Home.
 */
const REDIRECT_KEY = "musizeer.login-redirect";

export function rememberLoginRedirect(target: string | undefined): void {
  try {
    if (target) sessionStorage.setItem(REDIRECT_KEY, target);
    else sessionStorage.removeItem(REDIRECT_KEY);
  } catch {
    // blocked storage: nothing to remember
  }
}

/**
 * Sends the browser to the server, which sends it on to the provider's
 * authorization; `redirect` is where the viewer was going.
 */
export function startProviderLogin(provider: string, redirect: string | undefined): void {
  rememberLoginRedirect(redirect);
  window.location.assign(providerLoginStartUrl(provider));
}

/** The page to go to after a sign-in through a provider (only a path inside the app), and forgets it. */
export function takeLoginRedirect(): string {
  try {
    const target = sessionStorage.getItem(REDIRECT_KEY);
    sessionStorage.removeItem(REDIRECT_KEY);
    return safeRedirect(target);
  } catch {
    return "/";
  }
}

/** The stable code (`detail.error_code`) a failed provider sign-in carries, or the one of its kind. */
export function providerLoginErrorCode(error: unknown): string {
  const { code, kind } = classifyError(error);
  if (code) return code;
  return kind === "unreachable" || kind === "upstream" ? "provider_login_unavailable" : "provider_login_refused";
}

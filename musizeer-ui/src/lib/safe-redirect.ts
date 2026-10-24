/**
 * Where to go after signing in: only a path inside this app. Anything else
 * (another origin, a protocol-relative "//host", a "javascript:" URL, the
 * login page itself) falls back to Home — an open redirect is a phishing aid.
 */
export function safeRedirect(target: unknown): string {
  if (typeof target !== "string" || !target.startsWith("/") || target.startsWith("//") || target.startsWith("/\\")) {
    return "/";
  }
  if (/[\u0000-\u001f]/.test(target)) return "/";
  if (isAuthPage(target)) return "/";
  return target;
}

const isAuthPage = (target: string) => /^\/(login|register)(?:[/?#]|$)/.test(target);

/**
 * The search of a link to the sign-in (or register) page from `href`, the
 * page the viewer is on: brings them back there afterwards. Nothing for
 * Home, or when they are already on an auth page (a link from there keeps
 * the `redirect` it carries instead).
 */
export function signInSearch(href: string, carried?: string): { redirect?: string } {
  const target = safeRedirect(isAuthPage(href) ? carried : href);
  return target === "/" ? {} : { redirect: target };
}

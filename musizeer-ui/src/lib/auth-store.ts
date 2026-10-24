// In-memory access token + (optional) localStorage refresh token fallback.
// Refresh tokens are preferred via httpOnly cookie (server-side); the
// localStorage path is a graceful degradation when the backend cannot set
// cross-origin cookies. Subscribers are notified on token changes so that
// React state (AuthContext) stays in sync without prop drilling.

const REFRESH_KEY = "musizeer.refresh_token";
// Web Lock held by the tab refreshing the session: every tab of the origin
// shares the refresh token above, and the server rotates it on each use.
const REFRESH_LOCK = "musizeer.refresh";

let accessToken: string | null = null;
// Bumped by clear() (sign-out) and beginSession() (sign-in). A refresh that
// started under an older generation must not write its result once it
// resolves — the session it belongs to has ended (or been replaced), and
// writing anyway would resurrect a session the user ended, or clobber the
// tokens of a newer one. See refreshAccessToken() in api-client.ts.
let generation = 0;
const listeners = new Set<() => void>();

function notify() {
  for (const fn of listeners) fn();
}

function parseJwtExp(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

export const authStore = {
  getAccessToken(): string | null {
    return accessToken;
  },
  getAccessTokenExpiresAt(): number | null {
    return accessToken ? parseJwtExp(accessToken) : null;
  },
  setAccessToken(token: string | null) {
    accessToken = token;
    notify();
  },
  getRefreshToken(): string | null {
    try {
      return localStorage.getItem(REFRESH_KEY);
    } catch {
      return null;
    }
  },
  setRefreshToken(token: string | null) {
    try {
      if (token) localStorage.setItem(REFRESH_KEY, token);
      else localStorage.removeItem(REFRESH_KEY);
    } catch {
      // ignore (private mode, etc.)
    }
  },
  clear() {
    accessToken = null;
    generation += 1;
    try {
      localStorage.removeItem(REFRESH_KEY);
    } catch {
      // ignore
    }
    notify();
  },
  // Call when a new session starts (sign-in), so any refresh still in flight
  // from a previous session is treated as stale even if that session was
  // never explicitly cleared.
  beginSession(): number {
    generation += 1;
    return generation;
  },
  getGeneration(): number {
    return generation;
  },
  /**
   * Runs `fn` while no other tab of this origin is refreshing the session.
   * Without Web Locks (old browsers) it runs at once: the server's grace
   * window and refreshAccessToken's "still our token?" check cover the race.
   */
  withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
    const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
    if (!locks?.request) return fn();
    return locks.request(REFRESH_LOCK, fn) as Promise<T>;
  },
  subscribe(fn: () => void): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};

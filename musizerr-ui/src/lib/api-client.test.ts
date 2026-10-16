import { describe, it, expect, vi, beforeEach } from "vitest";
import axios, { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { authStore } from "./auth-store";
import { refreshAccessToken } from "./api-client";

// Task 1 (Phase 5b, second half): signing out did not sign you out. A
// refresh already in flight when the user signed out captured the old
// refresh token before authStore.clear() ran, and when it resolved it wrote
// a fresh access+refresh token pair straight back into the store —
// resurrecting the session the user had just ended. authStore now tracks a
// "generation" counter, bumped by clear() (sign-out) and beginSession()
// (sign-in); refreshAccessToken captures it before making the request and
// discards the result if it no longer matches once the request resolves.
//
// This test interleaves the two operations deliberately — start the
// request, sign out while it is still pending, THEN resolve it — because
// that interleaving is the entire bug. A test that runs them sequentially
// would pass against the old code too and prove nothing.

describe("refreshAccessToken — sign-out race", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    authStore.clear();
  });

  it("discards a refresh that resolves after sign-out instead of resurrecting the session", async () => {
    authStore.setAccessToken("old-access-token");
    authStore.setRefreshToken("old-refresh-token");

    let resolvePost!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolvePost = resolve;
    });
    vi.spyOn(axios, "post").mockReturnValue(pending as never);

    // 1. Refresh starts — captures the pre-sign-out generation and reads
    //    the (still valid) old refresh token, then awaits the network call.
    const refreshPromise = refreshAccessToken();

    // 2. The user signs out WHILE that request is still in flight.
    authStore.clear();
    expect(authStore.getAccessToken()).toBeNull();
    expect(authStore.getRefreshToken()).toBeNull();

    // 3. The in-flight request now resolves with a brand new token pair, as
    //    if it had raced the sign-out and won the network round trip.
    resolvePost({
      data: { access_token: "new-access-token", refresh_token: "new-refresh-token" },
    });
    const result = await refreshPromise;

    // The result must be discarded, not written: the user asked to be
    // signed out and nothing should undo that.
    expect(result).toBeNull();
    expect(authStore.getAccessToken()).toBeNull();
    expect(authStore.getRefreshToken()).toBeNull();
  });

  it("does not tear down a session that signed back in while a stale refresh was failing", async () => {
    // A refresh starts under the OLD session's generation...
    authStore.setAccessToken("old-access-token");
    authStore.setRefreshToken("old-refresh-token");

    let rejectPost!: (reason: unknown) => void;
    const pending = new Promise((_resolve, reject) => {
      rejectPost = reject;
    });
    vi.spyOn(axios, "post").mockReturnValue(pending as never);

    const refreshPromise = refreshAccessToken();

    // ...the user signs out, then immediately signs back in (a new session,
    // with its own tokens)...
    authStore.clear();
    authStore.beginSession();
    authStore.setAccessToken("new-session-access-token");
    authStore.setRefreshToken("new-session-refresh-token");

    // ...and only now does the stale request fail (e.g. the old refresh
    // token was rejected server-side because it had just been revoked).
    rejectPost(new Error("401"));
    const result = await refreshPromise;

    expect(result).toBeNull();
    // The new session's tokens must survive — the failure belonged to a
    // session that no longer exists.
    expect(authStore.getAccessToken()).toBe("new-session-access-token");
    expect(authStore.getRefreshToken()).toBe("new-session-refresh-token");
  });

  it("ordinary case: a refresh started after sign-out finds no token and does nothing", async () => {
    authStore.clear();
    expect(authStore.getRefreshToken()).toBeNull();

    const postSpy = vi.spyOn(axios, "post");
    const result = await refreshAccessToken();

    expect(result).toBeNull();
    expect(postSpy).not.toHaveBeenCalled();
    expect(authStore.getAccessToken()).toBeNull();
    expect(authStore.getRefreshToken()).toBeNull();
  });
});

describe("refreshAccessToken — an unreachable server is not a sign-out", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    authStore.clear();
  });

  it("keeps the refresh token when the server cannot be reached", async () => {
    authStore.setRefreshToken("still-valid");
    vi.spyOn(axios, "post").mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));

    const result = await refreshAccessToken();

    expect(result).toBeNull();
    expect(authStore.getRefreshToken()).toBe("still-valid");
  });

  it("keeps it on a 502 from the proxy while the backend restarts", async () => {
    authStore.setRefreshToken("still-valid");
    const response = { status: 502, data: "Bad Gateway", statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
    vi.spyOn(axios, "post").mockRejectedValue(new AxiosError("bad gateway", "502", undefined, undefined, response));

    await refreshAccessToken();

    expect(authStore.getRefreshToken()).toBe("still-valid");
  });

  it("signs out when the server refuses the refresh token", async () => {
    authStore.setRefreshToken("revoked");
    const response = { status: 401, data: { detail: "Invalid or revoked refresh token" }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
    vi.spyOn(axios, "post").mockRejectedValue(new AxiosError("unauthorized", "401", undefined, undefined, response));
    vi.spyOn(axios, "delete").mockResolvedValue({});

    const result = await refreshAccessToken();

    expect(result).toBeNull();
    expect(authStore.getRefreshToken()).toBeNull();
  });
});

describe("refreshAccessToken — one attempt at a time", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    authStore.clear();
  });

  it("a refresh without a token does not block the next one", async () => {
    // Regression: the attempt that returned before its first await (no
    // refresh token) stayed cached as "in flight" forever, so every later
    // refresh — the scheduled renewal after signing in — answered null.
    expect(await refreshAccessToken()).toBeNull();

    authStore.setRefreshToken("rt");
    vi.spyOn(axios, "post").mockResolvedValue({ data: { access_token: "at", refresh_token: "rt2" } });

    expect(await refreshAccessToken()).toBe("at");
    expect(authStore.getAccessToken()).toBe("at");
  });

  it("concurrent callers share one request", async () => {
    authStore.setRefreshToken("rt");
    const post = vi.spyOn(axios, "post").mockResolvedValue({ data: { access_token: "at", refresh_token: "rt2" } });

    const [a, b] = await Promise.all([refreshAccessToken(), refreshAccessToken()]);

    expect(a).toBe("at");
    expect(b).toBe("at");
    // One refresh call plus the stream-session sync.
    expect(post.mock.calls.filter(([url]) => String(url).endsWith("/auth/account/refresh"))).toHaveLength(1);
  });
});

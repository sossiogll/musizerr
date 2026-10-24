import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import axios, { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";

// Every tab of the browser shares ONE refresh token (localStorage) and the
// server rotates it on each refresh. Two tabs refreshing at once (a dev
// server restart reloads them all) used to send the same token: the second
// got a 401 and signed the user out. Each "tab" here is a fresh instance of
// api-client/auth-store over the same localStorage, and the fake server
// rotates strictly (no grace window), so only the frontend is under test.

interface Tab {
  refreshAccessToken: () => Promise<string | null>;
  authStore: { getAccessToken(): string | null; getRefreshToken(): string | null; setRefreshToken(t: string | null): void };
}

async function openTab(): Promise<Tab> {
  vi.resetModules();
  const client = await import("./api-client");
  const store = await import("./auth-store");
  return { refreshAccessToken: client.refreshAccessToken, authStore: store.authStore };
}

/** navigator.locks shared by every tab of the origin: one holder per name. */
function installLocks() {
  const queues = new Map<string, Promise<unknown>>();
  const locks = {
    request: (name: string, callback: () => Promise<unknown>) => {
      const previous = queues.get(name) ?? Promise.resolve();
      const run = previous.then(callback, callback);
      queues.set(name, run.catch(() => undefined));
      return run;
    },
  };
  Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
}

function removeLocks() {
  Object.defineProperty(navigator, "locks", { value: undefined, configurable: true });
}

const refused = () => {
  const response = { status: 401, data: { detail: "Invalid or revoked refresh token" }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("unauthorized", "401", undefined, undefined, response);
};

/** A server that rotates strictly: a token works once. */
function strictServer() {
  let current = "T0";
  let issued = 0;
  const refreshCalls: string[] = [];
  vi.spyOn(axios, "post").mockImplementation(async (url: string, body?: unknown) => {
    if (!url.endsWith("/auth/account/refresh")) return { data: null };
    const sent = (body as { refresh_token: string }).refresh_token;
    refreshCalls.push(sent);
    // The network round trip: lets the other tab's request interleave.
    await new Promise((r) => setTimeout(r, 5));
    if (sent !== current) throw refused();
    issued += 1;
    current = `T${issued}`;
    return { data: { access_token: `A${issued}`, refresh_token: current } };
  });
  const signOut = vi.spyOn(axios, "delete").mockResolvedValue({});
  return { refreshCalls, signOut, current: () => current };
}

describe("refreshAccessToken — two tabs refreshing at the same time", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });
  afterEach(() => removeLocks());

  it("serializes the refresh across tabs: both stay signed in", async () => {
    installLocks();
    const server = strictServer();
    const tabA = await openTab();
    const tabB = await openTab();
    tabA.authStore.setRefreshToken("T0");

    const [a, b] = await Promise.all([tabA.refreshAccessToken(), tabB.refreshAccessToken()]);

    expect(a).not.toBeNull();
    expect(b).not.toBeNull();
    // The second tab read the token AFTER the first rotated it.
    expect(server.refreshCalls).toEqual(["T0", "T1"]);
    expect(tabA.authStore.getRefreshToken()).toBe(server.current());
    expect(server.signOut).not.toHaveBeenCalled();
  });

  it("without cross-tab locks, a 401 for a token another tab already replaced does not sign out", async () => {
    removeLocks();
    const server = strictServer();
    const tabA = await openTab();
    const tabB = await openTab();
    tabA.authStore.setRefreshToken("T0");

    const [a, b] = await Promise.all([tabA.refreshAccessToken(), tabB.refreshAccessToken()]);

    expect(a).not.toBeNull();
    // B's T0 was refused because A had rotated it: B retries with A's token.
    expect(b).not.toBeNull();
    expect(tabB.authStore.getRefreshToken()).toBe(server.current());
    expect(server.signOut).not.toHaveBeenCalled();
  });

  it("still signs out when the token refused is the one the session holds", async () => {
    installLocks();
    const server = strictServer();
    const tab = await openTab();
    tab.authStore.setRefreshToken("stolen-and-revoked");

    expect(await tab.refreshAccessToken()).toBeNull();
    expect(tab.authStore.getRefreshToken()).toBeNull();
    expect(server.signOut).toHaveBeenCalled();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import {
  MAX_AGE_MS,
  STORAGE_KEY,
  clearPersistedCache,
  newestSavedAt,
  persistQueryCache,
  resetPersistenceForTests,
  restoreQueryCache,
  setCacheOwner,
} from "./query-persistence";

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
}

function saved(): { owner: string | null; state: { queries: { queryKey: unknown[] }[] } } | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw ? JSON.parse(raw) : null;
}

beforeEach(() => {
  vi.useFakeTimers();
  resetPersistenceForTests();
  clearPersistedCache();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("persistQueryCache", () => {
  it("saves only the whitelisted roots", async () => {
    const qc = client();
    const stop = persistQueryCache(qc);
    qc.setQueryData(["favorites", { page: 1 }], { items: [1] });
    qc.setQueryData(["downloads"], { items: [] });
    qc.setQueryData(["settings-values"], { secret: "no" });
    qc.setQueryData(["admin-users"], { items: ["root"] });
    qc.setQueryData(["search", "q"], { items: [] });

    await vi.advanceTimersByTimeAsync(1_100);
    stop();

    const roots = saved()!.state.queries.map((q) => q.queryKey[0]).sort();
    expect(roots).toEqual(["downloads", "favorites"]);
  });

  it("never saves a media URL outside the API: it may be signed, and expire", async () => {
    const qc = client();
    const stop = persistQueryCache(qc);
    qc.setQueryData(["discover", "trending-tracks"], {
      items: [
        { title: "Signed", preview_url: "https://cdn.example/p.mp3?hdnea=exp=1", stream_url: null, cover_url: "https://cdn.example/c.jpg" },
        { title: "Host", preview_url: "/api/preview/p/1", stream_url: "/api/stream/abc", cover_url: "/api/cover/provider/p/1" },
      ],
    });

    await vi.advanceTimersByTimeAsync(1_100);
    stop();

    const raw = localStorage.getItem(STORAGE_KEY)!;
    expect(raw).not.toContain("hdnea");
    const items = (saved()!.state.queries[0] as unknown as { state: { data: { items: Record<string, unknown>[] } } }).state.data.items;
    expect(items[0]).toMatchObject({ title: "Signed", preview_url: null, cover_url: "https://cdn.example/c.jpg" });
    expect(items[1]).toMatchObject({ preview_url: "/api/preview/p/1", stream_url: "/api/stream/abc" });
  });

  it("throttles writes", async () => {
    const qc = client();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const stop = persistQueryCache(qc);
    for (let i = 0; i < 20; i++) qc.setQueryData(["downloads"], { n: i });
    await vi.advanceTimersByTimeAsync(1_100);
    stop();

    expect(setItem.mock.calls.filter(([key]) => key === STORAGE_KEY)).toHaveLength(1);
    setItem.mockRestore();
  });
});

describe("restoreQueryCache", () => {
  it("puts the saved copy back, with the time it was fetched", async () => {
    const first = client();
    const stop = persistQueryCache(first);
    setCacheOwner(first, "u1");
    first.setQueryData(["discover", "new-releases"], { items: ["a"] });
    await vi.advanceTimersByTimeAsync(1_100);
    stop();

    const second = client();
    expect(restoreQueryCache(second)).toBe("u1");
    expect(second.getQueryData(["discover", "new-releases"])).toEqual({ items: ["a"] });
    expect(newestSavedAt(second)).toBe(first.getQueryState(["discover", "new-releases"])!.dataUpdatedAt);
  });

  it("ignores a copy older than the maximum age", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      v: 1, savedAt: Date.now() - MAX_AGE_MS - 1, owner: "u1",
      state: { mutations: [], queries: [{ queryKey: ["downloads"], queryHash: '["downloads"]', state: { data: 1, status: "success", dataUpdatedAt: 1 } }] },
    }));
    const qc = client();

    expect(restoreQueryCache(qc)).toBeNull();
    expect(qc.getQueryData(["downloads"])).toBeUndefined();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("ignores garbage", () => {
    localStorage.setItem(STORAGE_KEY, "{not json");
    expect(restoreQueryCache(client())).toBeNull();
  });
});

describe("setCacheOwner", () => {
  it("drops another viewer's copy, in memory and on disk", async () => {
    const qc = client();
    const stop = persistQueryCache(qc);
    setCacheOwner(qc, "alice");
    qc.setQueryData(["favorites"], { items: ["alice's"] });
    qc.setQueryData(["search", "x"], { items: [] });
    await vi.advanceTimersByTimeAsync(1_100);

    setCacheOwner(qc, "bob");

    expect(qc.getQueryData(["favorites"])).toBeUndefined();
    expect(qc.getQueryData(["search", "x"])).toEqual({ items: [] });
    await vi.advanceTimersByTimeAsync(1_100);
    stop();
    expect(saved()?.state.queries.map((q) => q.queryKey[0]) ?? []).not.toContain("favorites");
  });

  it("keeps who the viewer is across the change, and asks again", async () => {
    const qc = client();
    const fetchStatus = vi.fn(async () => ({ mode: "user" }));
    qc.setQueryData(["auth-config"], { mode: "guest" });
    qc.setQueryData(["me"], { id: "bob" });
    const observer = new QueryObserver(qc, { queryKey: ["auth-config"], queryFn: fetchStatus, staleTime: Infinity });
    const stop = observer.subscribe(() => {});
    setCacheOwner(qc, "guest");
    qc.setQueryData(["downloads"], { items: ["guest's"] });

    setCacheOwner(qc, "bob");

    expect(qc.getQueryData(["downloads"])).toBeUndefined();
    expect(qc.getQueryData(["me"])).toEqual({ id: "bob" });
    await vi.waitFor(() => expect(qc.getQueryData(["auth-config"])).toEqual({ mode: "user" }));
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    stop();
  });

  it("keeps the copy for the same viewer", () => {
    const qc = client();
    setCacheOwner(qc, "alice");
    qc.setQueryData(["downloads"], { items: [1] });
    setCacheOwner(qc, "alice");
    expect(qc.getQueryData(["downloads"])).toEqual({ items: [1] });
  });
});

import { describe, expect, it } from "vitest";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { refetchTrack } from "./fresh-track";

function track(id: string, preview: string) {
  return { provider: "p", provider_id: id, title: id, preview_url: preview, playback: { type: "preview", source: "provider" } };
}

describe("refetchTrack", () => {
  it("asks the lists on screen again and finds the track in their new answer", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let calls = 0;
    const observer = new QueryObserver(client, {
      queryKey: ["discover", "trending-tracks"],
      queryFn: async () => {
        calls += 1;
        return { items: [track("1", `/api/preview/p/1?v=${calls}`), track("2", "x")] };
      },
    });
    const unsubscribe = observer.subscribe(() => {});
    await observer.refetch();

    const fresh = await refetchTrack(client, { provider: "p", provider_id: "1" });

    expect(calls).toBe(2);
    expect(fresh?.preview_url).toBe("/api/preview/p/1?v=2");
    expect(await refetchTrack(client, { provider: "p", provider_id: "missing" })).toBeNull();
    unsubscribe();
  });
});

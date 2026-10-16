import { describe, expect, it } from "vitest";
import { DEFAULT_POLICY, NOT_GOVERNED } from "@/test/viewer";
import { approvalVerdict, isPendingApproval, missingTracks, targetKey } from "./download-requests";
import { pendingApproval } from "@/test/download-requests";
import type { ViewerDownloadPolicy } from "@/types";

describe("isPendingApproval", () => {
  it("tells the 202 answer of a request that waits from a download that ran", () => {
    expect(isPendingApproval(pendingApproval)).toBe(true);
    expect(isPendingApproval({ status: "queued", download_id: "d", library_id: "l" })).toBe(false);
    expect(isPendingApproval({ download_ids: ["a"], total_tracks: 1 })).toBe(false);
  });
});

describe("targetKey", () => {
  it("names a request by its kind and what it asks for; the likes download has no target", () => {
    expect(targetKey("playlist", "catalog", "p1")).toBe("playlist:catalog:p1");
    expect(targetKey("liked_elsewhere")).toBe("liked_elsewhere::");
    expect(targetKey("album", "catalog", "p1")).not.toBe(targetKey("playlist", "catalog", "p1"));
  });
});

describe("approvalVerdict — reads the limit the server computed, never decides", () => {
  it("is never held for an admin, open mode or a guest (the policy does not apply)", () => {
    expect(approvalVerdict(NOT_GOVERNED, "playlist", 500)).toEqual({ waits: false, limit: null });
    expect(approvalVerdict(null, "playlist", 500)).toEqual({ waits: false, limit: null });
    expect(approvalVerdict(undefined, "playlist", 500)).toEqual({ waits: false, limit: null });
  });

  it("a kind that always waits (auto_up_to 0) waits whatever the page knows of its size", () => {
    expect(approvalVerdict(DEFAULT_POLICY, "playlist", 259)).toEqual({ waits: true, limit: 0 });
    expect(approvalVerdict(DEFAULT_POLICY, "artist", null)).toEqual({ waits: true, limit: 0 });
  });

  it("a request that needs nothing is not held, even where the kind always waits", () => {
    expect(approvalVerdict(DEFAULT_POLICY, "playlist", 0).waits).toBe(false);
  });

  it("holds the size against the limit: at it goes through, one more waits", () => {
    expect(approvalVerdict(DEFAULT_POLICY, "album", 30)).toEqual({ waits: false, limit: 30 });
    expect(approvalVerdict(DEFAULT_POLICY, "album", 31)).toEqual({ waits: true, limit: 30 });
  });

  it("does not guess when the size is unknown and there is room: it goes through", () => {
    expect(approvalVerdict(DEFAULT_POLICY, "album", null)).toEqual({ waits: false, limit: 30 });
  });

  it("no limit at all (every kind automatic, no quota) never waits", () => {
    const open: ViewerDownloadPolicy = {
      applies: true,
      rules: { ...DEFAULT_POLICY.rules!, album: { mode: "auto", threshold: 30, auto_up_to: null } },
      quota: { tracks: 0, window_days: 7, used: 0, remaining: null },
    };
    expect(approvalVerdict(open, "album", 10_000)).toEqual({ waits: false, limit: null });
  });
});

describe("missingTracks", () => {
  const library = (over: Partial<{ total: number | null; downloaded: number; in_progress: number; failed: number }>) => ({
    total: 10,
    downloaded: 0,
    in_progress: 0,
    failed: 0,
    ...over,
  });

  it("is what is neither in the library nor on its way, as the server counted it", () => {
    expect(missingTracks(library({ downloaded: 4, in_progress: 1 }), null)).toBe(5);
    // A failed track needs a download again: it is not subtracted.
    expect(missingTracks(library({ downloaded: 4, failed: 2 }), null)).toBe(6);
  });

  it("falls back to the tracks a source declares when the library says nothing", () => {
    expect(missingTracks(undefined, 20)).toBe(20);
    expect(missingTracks(library({ total: null }), 20)).toBe(20);
  });

  it("is unknown when no source says how many tracks there are", () => {
    expect(missingTracks(undefined, null)).toBeNull();
    expect(missingTracks(library({ total: null }), undefined)).toBeNull();
  });

  it("never goes below zero", () => {
    expect(missingTracks(library({ downloaded: 12 }), null)).toBe(0);
  });
});

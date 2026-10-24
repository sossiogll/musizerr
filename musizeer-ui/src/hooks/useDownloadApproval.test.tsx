import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { I18nProvider, LOCALE_STORAGE_KEY } from "@/i18n";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { pendingApproval } from "@/test/download-requests";
import { useAnnouncePending, useApproval } from "./useDownloadApproval";
import type { AuthConfig, RequestKind } from "@/types";

let status: AuthConfig;
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status }) }));
const mockToast = vi.hoisted(() => ({ info: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <I18nProvider>{children}</I18nProvider>
  </QueryClientProvider>
);

const hint = (kind: RequestKind, needed: number | null) => renderHook(() => useApproval(kind, needed), { wrapper }).result.current;

beforeEach(() => {
  localStorage.clear();
  status = viewerStatus("user", DEFAULT_POLICY);
  mockToast.info.mockReset();
});

describe("useApproval — what a download button tells before it is pressed (English)", () => {
  it("a kind that always waits: Request, with the tracks it needs when the page knows them", () => {
    expect(hint("playlist", null)).toEqual({ waits: true, hint: "Needs approval" });
    expect(hint("playlist", 259)).toEqual({ waits: true, hint: "Needs approval: 259 tracks" });
    expect(hint("playlist", 1)).toEqual({ waits: true, hint: "Needs approval: 1 track" });
  });

  it("over the limit that goes through at once: says how many do", () => {
    expect(hint("album", 31)).toEqual({ waits: true, hint: "Needs approval: 31 tracks (up to 30 go through at once)" });
  });

  it("within the limit, or of an unknown size: goes through (the limit is a hint for the latter)", () => {
    expect(hint("album", 30)).toEqual({ waits: false, hint: undefined });
    expect(hint("album", null)).toEqual({ waits: false, hint: "Goes through at once up to 30 tracks; more needs approval" });
    expect(hint("liked_elsewhere", null).hint).toBe("Goes through at once up to 50 tracks; more needs approval");
  });

  it("a limit of one track reads in the singular", () => {
    status = viewerStatus("user", { ...DEFAULT_POLICY, rules: { ...DEFAULT_POLICY.rules!, album: { mode: "auto_up_to", threshold: 30, auto_up_to: 1 } } });
    expect(hint("album", null).hint).toBe("Goes through at once up to 1 track; more needs approval");
  });

  it("an admin, or open mode, sees no change and no hint", () => {
    for (const mode of ["admin", "open"] as const) {
      status = viewerStatus(mode);
      expect(hint("playlist", 259)).toEqual({ waits: false, hint: undefined });
    }
  });

  it("is the same object while nothing changes (memoized rows do not re-render for it)", () => {
    const { result, rerender } = renderHook(() => useApproval("playlist", 259), { wrapper });
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});

describe("useApproval (Italian, plural forms)", () => {
  beforeEach(() => localStorage.setItem(LOCALE_STORAGE_KEY, "it"));

  it.each([
    [1, "Serve approvazione: 1 brano"],
    [259, "Serve approvazione: 259 brani"],
  ])("%i tracks: %s", (needed, text) => {
    expect(hint("playlist", needed).hint).toBe(text);
  });

  it("the limit, singular and plural", () => {
    expect(hint("album", 31).hint).toBe("Serve approvazione: 31 brani (fino a 30 partono subito)");
    expect(hint("album", null).hint).toBe("Parte subito fino a 30 brani; oltre serve approvazione");
  });
});

describe("useAnnouncePending — the one wording of a request that was sent", () => {
  it("says how many tracks and why it waits (English)", () => {
    const { result } = renderHook(() => useAnnouncePending(), { wrapper });
    act(() => result.current(pendingApproval));
    expect(mockToast.info).toHaveBeenCalledWith("Sent for approval: 259 tracks to download", { description: "This kind always needs approval" });
    act(() => result.current({ needed_tracks: 1, reason: "over_quota" }));
    expect(mockToast.info).toHaveBeenLastCalledWith("Sent for approval: 1 track to download", { description: "Over the quota" });
  });

  it("in Italian, with its plurals", () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    const { result } = renderHook(() => useAnnouncePending(), { wrapper });
    act(() => result.current(pendingApproval));
    expect(mockToast.info).toHaveBeenCalledWith("Inviata per approvazione: 259 brani da scaricare", { description: "Questo tipo richiede sempre approvazione" });
    act(() => result.current({ needed_tracks: 1, reason: "over_threshold" }));
    expect(mockToast.info).toHaveBeenLastCalledWith("Inviata per approvazione: 1 brano da scaricare", { description: "Più grande del limite automatico" });
  });
});

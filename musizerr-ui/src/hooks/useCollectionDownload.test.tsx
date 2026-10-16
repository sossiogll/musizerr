import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { AxiosError, type AxiosResponse } from "axios";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { I18nProvider } from "@/i18n";
import { queryKeys } from "@/lib/query-keys";
import { pendingApproval } from "@/test/download-requests";
import { useCollectionDownload } from "./useCollectionDownload";

const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), warning: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const addItems = vi.fn();
vi.mock("@/contexts/QueueContext", () => ({ useQueueActions: () => addItems }));

const mockDownloadPlaylist = vi.fn();
const mockDownloadArtist = vi.fn();
vi.mock("@/lib/api-client", () => ({
  downloadAlbum: vi.fn(),
  downloadArtist: (...a: unknown[]) => mockDownloadArtist(...a),
  downloadPlaylist: (...a: unknown[]) => mockDownloadPlaylist(...a),
}));

let queryClient: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>
    <I18nProvider>{children}</I18nProvider>
  </QueryClientProvider>
);

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  addItems.mockReset();
  mockDownloadPlaylist.mockReset();
  mockDownloadArtist.mockReset();
  Object.values(mockToast).forEach((fn) => fn.mockReset());
});

describe("a collection download", () => {
  it("queues the tracks and says how many", async () => {
    mockDownloadPlaylist.mockResolvedValue({ download_ids: ["a", "b"], total_tracks: 2, skipped: 0 });
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("playlist", "p", "1", "favorite"));

    expect(addItems).toHaveBeenCalledWith(["a", "b"]);
    expect(mockToast.success).toHaveBeenCalledWith("Queued 2 tracks");
  });

  it("waiting for an admin (approval policy): nothing is queued and the user is told it was sent, and why it waits", async () => {
    mockDownloadPlaylist.mockResolvedValue(pendingApproval);
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("playlist", "p", "1", "favorite"));

    expect(addItems).not.toHaveBeenCalled();
    expect(mockToast.info).toHaveBeenCalledWith("Sent for approval: 259 tracks to download", { description: "This kind always needs approval" });
    expect(mockToast.success).not.toHaveBeenCalled();
    expect(mockToast.error).not.toHaveBeenCalled();
  });

  it("a request that waits makes the lists of the viewer's requests read again", async () => {
    mockDownloadPlaylist.mockResolvedValue(pendingApproval);
    queryClient.setQueryData(queryKeys.myRequests({ status: ["pending"], limit: 200 }), { items: [], total: 0, page: 1, limit: 200, pages: 0 });
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("playlist", "p", "1", "favorite"));

    expect(queryClient.getQueryState(queryKeys.myRequests({ status: ["pending"], limit: 200 }))?.isInvalidated).toBe(true);
  });

  it.each([
    ["over_threshold", "Larger than the automatic limit"],
    ["over_quota", "Over the quota"],
  ] as const)("says why it waits: %s", async (reason, description) => {
    mockDownloadPlaylist.mockResolvedValue({ ...pendingApproval, needed_tracks: 1, reason });
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("playlist", "p", "1", "favorite"));

    expect(mockToast.info).toHaveBeenCalledWith("Sent for approval: 1 track to download", { description });
  });

  it("a refusal with an error code of the server is said in the viewer's words", async () => {
    const detail = { error_code: "queue_unavailable", error_message: "The download could not be queued" };
    mockDownloadPlaylist.mockRejectedValue(new AxiosError("503", "ERR_BAD_RESPONSE", undefined, undefined, { status: 503, data: { detail } } as AxiosResponse));
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("playlist", "p", "1", "favorite"));

    expect(mockToast.error).toHaveBeenCalledWith("The queue is unavailable, so this could not start. A download is tried again automatically; otherwise try again in a moment.");
  });

  it("a discography with releases that could not be read is queued and says what is missing", async () => {
    mockDownloadArtist.mockResolvedValue({ download_ids: ["a"], total_tracks: 1, releases_failed: 2 });
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("artist", "p", "1", "favorite"));

    expect(addItems).toHaveBeenCalledWith(["a"]);
    expect(mockToast.success).toHaveBeenCalledWith("Queued 1 track");
    expect(mockToast.warning).toHaveBeenCalledWith("2 releases could not be read, so their tracks are missing");
  });

  it("a discography that waits for approval says the same", async () => {
    mockDownloadArtist.mockResolvedValue({ ...pendingApproval, releases_failed: 1 });
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("artist", "p", "1", "favorite"));

    expect(addItems).not.toHaveBeenCalled();
    expect(mockToast.info).toHaveBeenCalled();
    expect(mockToast.warning).toHaveBeenCalledWith("1 release could not be read, so its tracks are missing");
  });

  it.each([
    ["discography_not_supported", "This provider cannot give an artist's whole discography to download."],
    ["discography_empty", "There is no release of this artist to download."],
    ["discography_too_large", "This artist has too many releases to download in one request."],
  ])("the refusal %s is said in the viewer's words", async (error_code, text) => {
    const detail = { error_code, error_message: "the server's English message" };
    mockDownloadArtist.mockRejectedValue(new AxiosError("422", "ERR_BAD_REQUEST", undefined, undefined, { status: 422, data: { detail } } as AxiosResponse));
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("artist", "p", "1", "favorite"));

    expect(mockToast.error).toHaveBeenCalledWith(text);
  });

  it("a failure with nothing to say is 'Download failed'", async () => {
    mockDownloadPlaylist.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useCollectionDownload(), { wrapper });

    await act(() => result.current.download("playlist", "p", "1", "favorite"));

    expect(mockToast.error).toHaveBeenCalledWith("Download failed");
  });
});

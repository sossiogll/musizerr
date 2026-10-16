import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { I18nProvider } from "@/i18n";
import { AxiosError, type AxiosResponse } from "axios";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { waitingPlaylist } from "@/test/download-requests";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import {
  activeItem,
  alreadyInLibrary,
  attached,
  dispatchedOnOwned,
  doneItemWithNewerDispatch,
  downloadingOnMissing,
  failedItem,
  failedOnOwned,
} from "@/test/live-downloads";
import { QueueProvider, useQueue } from "@/contexts/QueueContext";
import { TrackRow } from "@/components/tracks/TrackRow";
import { slotStatus } from "./useTrackDownload";
import type { AuthConfig, DownloadRequest, ProgressEvent, Track, TrackDownload } from "@/types";

// Status precedence, on responses and events captured from a running server
// (src/test/live-downloads.ts). Real case (29 Sep): a track in the library
// whose newest attempt was "dispatched" showed "Delegated" inside the
// "Completed" tab, and a live "failed" offered Retry on a track in the
// library, because the row preferred the live attempt status over the list.

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => ({ track: null, playing: false, play: vi.fn() }),
}));

const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const mockDownloadTrack = vi.fn();
/** What the server keeps of the viewer's waiting requests (GET /download-requests?status=pending). */
let waiting: DownloadRequest[] = [];
const mockMyRequests = vi.fn(async () => ({ items: waiting, total: waiting.length, page: 1, limit: 200, pages: 1 }));
let progressSource: { onmessage: ((e: MessageEvent) => void) | null } & Record<string, unknown> = { onmessage: null };
vi.mock("@/lib/api-client", () => ({
  downloadTrack: (...a: unknown[]) => mockDownloadTrack(...a),
  getMyRequests: () => mockMyRequests(),
  addFavorite: vi.fn(),
  removeFavorite: vi.fn(),
  getFavorite: vi.fn(),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => {
    progressSource = { close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 };
    return progressSource;
  },
  refreshAccessToken: vi.fn(),
}));

let active = 0;
function ActiveCount() {
  active = useQueue().totalActive;
  return null;
}

function renderRow(ui: ReactNode) {
  const root = createRootRoute({ component: () => <>{ui}</> });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <QueueProvider>
          <ActiveCount />
          <RouterProvider router={router} />
        </QueueProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/** A row of the downloads page, as routes/downloads.tsx renders an item. */
const downloadsRow = (item: TrackDownload) => renderRow(<TrackRow track={item.track as Track} statusOverride={item.status} />);

const send = (event: ProgressEvent) =>
  act(() => {
    progressSource.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
  });

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
  mockDownloadTrack.mockReset();
  mockMyRequests.mockClear();
  waiting = [];
  Object.values(mockToast).forEach((fn) => fn.mockReset());
  active = 0;
});

describe("slotStatus — which state a row shows", () => {
  it("the list says in the library: a newer attempt's live event never takes it out", () => {
    expect(doneItemWithNewerDispatch.status).toBe("downloaded");
    expect(slotStatus(doneItemWithNewerDispatch.status, dispatchedOnOwned, false)).toBe("downloaded");
    expect(slotStatus(doneItemWithNewerDispatch.status, failedOnOwned, false)).toBe("downloaded");
    // An event from a server that does not send file_state yet: still in the library.
    expect(slotStatus("downloaded", { status: "failed" }, false)).toBe("downloaded");
  });

  it("the live event carries the server's file state, not only the attempt's status", () => {
    expect(dispatchedOnOwned.status).toBe("dispatched");
    expect(slotStatus(null, dispatchedOnOwned, false)).toBe("downloaded");
  });

  it("otherwise the live event, newer than the list, wins", () => {
    expect(slotStatus(failedItem.status, downloadingOnMissing, false)).toBe(downloadingOnMissing.file_state);
    expect(slotStatus(activeItem.status, { status: "failed", file_state: "failed" }, false)).toBe("failed");
  });

  it("then the optimistic queued state, then the list", () => {
    expect(slotStatus(failedItem.status, undefined, true)).toBe("pending");
    expect(slotStatus(activeItem.status, undefined, false)).toBe("dispatched");
    expect(slotStatus(null, undefined, false)).toBeNull();
  });
});

describe("a row of the Downloads page", () => {
  it("in 'Completed' stays In library when its newer attempt is dispatched, then fails", async () => {
    downloadsRow(doneItemWithNewerDispatch);
    expect(await screen.findByTitle("In library")).toBeInTheDocument();

    send({ ...dispatchedOnOwned, track_id: doneItemWithNewerDispatch.track_id });
    expect(screen.getByTitle("In library")).toBeInTheDocument();
    expect(screen.queryByTitle("Delegated")).not.toBeInTheDocument();

    send({ ...failedOnOwned, track_id: doneItemWithNewerDispatch.track_id });
    expect(screen.getByTitle("In library")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
    // A track in the library is never counted as an active download.
    expect(active).toBe(0);
  });

  it("a failed track shows the live state of its new attempt", async () => {
    downloadsRow(failedItem);
    expect(await screen.findByRole("button", { name: /Retry/ })).toBeInTheDocument();

    send({ ...downloadingOnMissing, track_id: failedItem.track_id });
    expect(await screen.findByTitle("Delegated")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
    expect(active).toBe(1);
  });
});

describe("starting a download", () => {
  it("already in the library: nothing is queued, the row says In library", async () => {
    mockDownloadTrack.mockResolvedValue(alreadyInLibrary);
    renderRow(<TrackRow track={makeTrack({ title: alreadyInLibrary.track.title })} />);

    fireEvent.click(await screen.findByRole("button", { name: /Download .* and add it to favorites/ }));

    expect(await screen.findByTitle("In library")).toBeInTheDocument();
    expect(screen.queryByTitle("Queued")).not.toBeInTheDocument();
    expect(mockToast.info).toHaveBeenCalledWith(expect.stringContaining("Already in the library"));
    expect(mockToast.success).not.toHaveBeenCalled();
    expect(active).toBe(0);
  });

  it("attached to an open request: queued, following that request", async () => {
    mockDownloadTrack.mockResolvedValue(attached);
    renderRow(<TrackRow track={makeTrack()} />);

    fireEvent.click(await screen.findByRole("button", { name: /Download .* and add it to favorites/ }));

    expect(await screen.findByTitle("Queued")).toBeInTheDocument();
    await waitFor(() => expect(mockToast.success).toHaveBeenCalled());
    expect(active).toBe(1);
  });

  it("waiting for an admin (approval policy): nothing is queued, the toast says it was sent and the row stays 'Awaiting approval'", async () => {
    const asked: DownloadRequest = {
      ...waitingPlaylist,
      id: "r1",
      kind: "track",
      reason: "over_quota",
      target: { provider: "prov", id: "p1" },
      title: "Needs approval",
      subtitle: "Artist A",
      needed_tracks: 1,
    };
    mockDownloadTrack.mockResolvedValue({ status: "pending_approval", request_id: "r1", kind: "track", needed_tracks: 1, reason: "over_quota" });
    renderRow(<TrackRow track={makeTrack({ title: "Needs approval" })} />);

    // The list of waiting requests the server keeps: the request shows up in it once it was sent.
    waiting = [];
    fireEvent.click(await screen.findByRole("button", { name: /Download .* and add it to favorites/ }));
    waiting = [asked];

    await waitFor(() => expect(mockToast.info).toHaveBeenCalledWith("Sent for approval: 1 track to download", { description: "Over the quota" }));
    expect(await screen.findByText("Awaiting approval")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download|Request/ })).not.toBeInTheDocument();
    expect(screen.queryByTitle("Queued")).not.toBeInTheDocument();
    expect(screen.queryByTitle("In library")).not.toBeInTheDocument();
    expect(mockToast.success).not.toHaveBeenCalled();
    expect(mockToast.error).not.toHaveBeenCalled();
    expect(active).toBe(0);
  });

  it("a retry the server refuses (not approved) says why, in words", async () => {
    mockDownloadTrack.mockRejectedValue(
      new AxiosError("403", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 403,
        data: { detail: { error_code: "retry_not_approved", error_message: "Only a download that was requested and approved can be retried." } },
      } as AxiosResponse),
    );
    renderRow(<TrackRow track={makeTrack()} />);

    fireEvent.click(await screen.findByRole("button", { name: /Download .* and add it to favorites/ }));

    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith("Only a download that was requested and approved can be retried."));
  });
});

describe("the download button says honestly what will happen", () => {
  it("a viewer the policy lets download at once sees Download", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    renderRow(<TrackRow track={makeTrack({ title: "Song A" })} />);

    const button = await screen.findByRole("button", { name: "Download Song A and add it to favorites" });
    expect(button).toHaveTextContent("Download");
    expect(button).toHaveAttribute("title", "Download Song A and add it to favorites");
  });

  it("when the request would wait for an admin it says Request, and why", async () => {
    status = viewerStatus("user", { ...DEFAULT_POLICY, rules: { ...DEFAULT_POLICY.rules!, track: { mode: "approval", threshold: 1, auto_up_to: 0 } } });
    renderRow(<TrackRow track={makeTrack({ title: "Song A" })} />);

    const button = await screen.findByRole("button", { name: "Request Song A (needs approval) and add it to favorites" });
    expect(button).toHaveTextContent("Request");
    expect(button).toHaveAttribute("title", "Needs approval: 1 track");
    expect(screen.queryByRole("button", { name: /^Download/ })).not.toBeInTheDocument();
  });

  it("a quota that is used up makes it wait too, whatever the mode", async () => {
    // `auto` with 0 tracks of quota left: the server reports auto_up_to 0.
    status = viewerStatus("user", {
      applies: true,
      rules: { ...DEFAULT_POLICY.rules!, track: { mode: "auto", threshold: 1, auto_up_to: 0 } },
      quota: { tracks: 200, window_days: 7, used: 200, remaining: 0 },
    });
    renderRow(<TrackRow track={makeTrack({ title: "Song A" })} />);

    expect(await screen.findByRole("button", { name: "Request Song A (needs approval) and add it to favorites" })).toBeInTheDocument();
  });

  it("an admin sees no change: the policy does not apply", async () => {
    status = viewerStatus("admin");
    renderRow(<TrackRow track={makeTrack({ title: "Song A" })} />);

    expect(await screen.findByRole("button", { name: "Download Song A" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request/ })).not.toBeInTheDocument();
    // Nothing is asked for an admin: they have no requests.
    expect(mockMyRequests).not.toHaveBeenCalled();
  });

  it("after a reload the request that still waits shows as 'Awaiting approval', not as a download to start", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    waiting = [{ ...waitingPlaylist, kind: "track", target: { provider: "prov", id: "p1" }, title: "Song A", needed_tracks: 1 }];
    renderRow(<TrackRow track={makeTrack({ title: "Song A" })} />);

    expect(await screen.findByText("Awaiting approval")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download|Request/ })).not.toBeInTheDocument();
  });

  it("another track's waiting request does not mark this one", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    waiting = [{ ...waitingPlaylist, kind: "track", target: { provider: "prov", id: "other" }, title: "Other", needed_tracks: 1 }];
    renderRow(<TrackRow track={makeTrack({ title: "Song A" })} />);

    expect(await screen.findByRole("button", { name: /^Download Song A/ })).toBeInTheDocument();
    await waitFor(() => expect(mockMyRequests).toHaveBeenCalled());
    expect(screen.queryByText("Awaiting approval")).not.toBeInTheDocument();
  });
});

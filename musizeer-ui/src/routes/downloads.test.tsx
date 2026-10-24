import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { approvedAlbum, waitingPlaylist } from "@/test/download-requests";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { DownloadsPage } from "./downloads";
import { parseDownloadsSearch } from "./downloads-search";
import type { DownloadPhase, DownloadsSummary } from "@/lib/api-client";
import type { AuthConfig, TrackDownload } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ authConfig: status, user: { username: "me" } }),
}));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const mockGetDownloads = vi.fn();
const mockRetryFailed = vi.fn();
const mockRetryOne = vi.fn();
let summary: DownloadsSummary;
let status: AuthConfig = viewerStatus("user");
/** What GET /download-requests answers (the viewer's own requests). */
const mockMyRequests = vi.fn();
vi.mock("@/lib/api-client", () => ({
  getDownloads: (...a: unknown[]) => mockGetDownloads(...a),
  getDownloadsSummary: async () => summary,
  getMyRequests: (...a: unknown[]) => mockMyRequests(...a),
  retryFailedDownloads: () => mockRetryFailed(),
  retryDownload: (id: string) => mockRetryOne(id),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
  refreshAccessToken: vi.fn(),
}));

const phases = (p: Partial<Record<DownloadPhase, number>>) => ({ queued: 0, active: 0, done: 0, failed: 0, review: 0, ...p });

function item(id: string, status: TrackDownload["status"], extra: Partial<TrackDownload> = {}): TrackDownload {
  return {
    track_id: id,
    status,
    provider_used: "prov",
    file_path: null,
    requested_by: "me",
    track: makeTrack({ internal_id: id, provider_id: id, title: `Song ${id}` }),
    attempts: [],
    ...extra,
  };
}

// The server filters by phase: each group gets what matches its phases.
const byPhase: Record<DownloadPhase, TrackDownload[]> = { queued: [], active: [], done: [], failed: [], review: [] };
mockGetDownloads.mockImplementation(async ({ phase }: { phase: DownloadPhase[] }) => {
  const items = phase.flatMap((p) => byPhase[p]);
  return { items, total: items.length, page: 1, limit: 25, pages: 1 };
});

const renderDownloads = (url = "/downloads") =>
  renderPage(DownloadsPage, { path: "/downloads", url, validateSearch: parseDownloadsSearch, queue: true });

beforeEach(() => {
  connectivityMock.reset();
  mockGetDownloads.mockClear();
  mockRetryFailed.mockReset();
  mockRetryOne.mockReset();
  status = viewerStatus("user");
  mockMyRequests.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, limit: 25, pages: 0 });
  summary = { statuses: {} as DownloadsSummary["statuses"], phases: phases({ active: 1, failed: 1, done: 1 }), total: 3, downloaded_since: 1 };
  byPhase.active = [item("a", "downloading")];
  byPhase.failed = [item("f", "failed")];
  byPhase.done = [item("d", "downloaded")];
  byPhase.queued = [];
  byPhase.review = [];
});

describe("Downloads page", () => {
  it("All: one group per need, most urgent first, with the counts from the summary", async () => {
    renderDownloads();

    await waitFor(() =>
      expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["In progress · 1", "Needs review · 1", "Completed · 1"]),
    );
    expect(screen.getByRole("button", { name: /Needs review\s*1/ })).toBeInTheDocument();
    expect(mockGetDownloads).toHaveBeenCalledWith(expect.objectContaining({ phase: ["active", "queued"], sort: "recent" }));
    expect(mockGetDownloads).toHaveBeenCalledWith(expect.objectContaining({ phase: ["failed", "review"] }));
    expect(mockGetDownloads).toHaveBeenCalledWith(expect.objectContaining({ phase: ["done"] }));
  });

  it("hides a group with nothing in it", async () => {
    byPhase.done = [];
    renderDownloads();
    await screen.findByText("Song a");
    expect(screen.queryByRole("heading", { name: /Completed/ })).not.toBeInTheDocument();
  });

  it("?tab=review asks the server for failed and review only, and says so when empty", async () => {
    byPhase.failed = [];
    renderDownloads("/downloads?tab=review");
    expect(await screen.findByText("Nothing here.")).toBeInTheDocument();
    expect(mockGetDownloads).toHaveBeenCalledTimes(1);
    expect(mockGetDownloads).toHaveBeenCalledWith(expect.objectContaining({ phase: ["failed", "review"] }));
  });

  it("switching tab writes it to the URL", async () => {
    const { router } = renderDownloads();
    fireEvent.click(await screen.findByRole("button", { name: /Completed\s*1/ }));
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: "done" }));
  });

  it("Retry all failed appears only with failures, and asks the server to retry them", async () => {
    mockRetryFailed.mockResolvedValue({ download_ids: ["x"] });
    renderDownloads();
    fireEvent.click(await screen.findByRole("button", { name: "Retry all failed" }));
    await waitFor(() => expect(mockRetryFailed).toHaveBeenCalledTimes(1));
  });

  it("no Retry all without failures", async () => {
    summary = { ...summary, phases: phases({ done: 1 }) };
    renderDownloads();
    await screen.findByText("Song d");
    expect(screen.queryByRole("button", { name: "Retry all failed" })).not.toBeInTheDocument();
  });

  it("the filter is sent to the server", async () => {
    renderDownloads("/downloads?tab=done");
    await screen.findByText("Song d");
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter by title, artist…" }), { target: { value: " love " } });
    await waitFor(() => expect(mockGetDownloads).toHaveBeenCalledWith(expect.objectContaining({ q: "love" })));
  });

  it("the attempts show the error in words and who asked, only when it was someone else", async () => {
    byPhase.failed = [
      item("f", "failed", {
        requested_by: "other",
        attempts: [
          { download_id: "1", status: "failed", retry_count: 0, provider_used: "prov", file_path: null, error_code: "not_found", error_message: "raw", started_at: null, completed_at: null },
        ],
      }),
    ];
    byPhase.done = [item("d", "downloaded", { attempts: [{ download_id: "2", status: "downloaded", retry_count: 0, provider_used: "prov", file_path: "x.flac", error_code: null, error_message: null, started_at: null, completed_at: null }] })];
    renderDownloads();

    fireEvent.click(await screen.findByRole("button", { name: "Show attempts for Song f" }));
    const failedPanel = screen.getByRole("button", { name: "Hide attempts for Song f" }).closest("div.rounded-md") as HTMLElement;
    expect(within(failedPanel).getByText("Requested by other")).toBeInTheDocument();
    expect(within(failedPanel).getByText(/via Provider/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show attempts for Song d" }));
    const donePanel = screen.getByRole("button", { name: "Hide attempts for Song d" }).closest("div.rounded-md") as HTMLElement;
    expect(within(donePanel).queryByText(/Requested by/)).not.toBeInTheDocument();
  });
});

describe("a download that never reached a worker", () => {
  it("shows the queue_unavailable code as an attempt's error, in words (the server says it is tried again automatically)", async () => {
    byPhase.failed = [
      item("f", "failed", {
        attempts: [
          {
            download_id: "1",
            status: "failed",
            retry_count: 0,
            provider_used: null,
            file_path: null,
            error_code: "queue_unavailable",
            error_message: "The download could not be queued: it will be tried again automatically.",
            started_at: null,
            completed_at: null,
          },
        ],
      }),
    ];
    renderDownloads();

    fireEvent.click(await screen.findByRole("button", { name: "Show attempts for Song f" }));

    expect(
      screen.getByText("The queue is unavailable, so this could not start. A download is tried again automatically; otherwise try again in a moment."),
    ).toBeInTheDocument();
  });
});

describe("Downloads › Requests (the approval policy)", () => {
  const twoWaiting = { items: [waitingPlaylist, approvedAlbum], total: 2, page: 1, limit: 25, pages: 1 };

  it("a viewer the policy governs has a Requests tab that counts what waits, and it lists their requests", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    // The waiting list (all pages) says 1 waits; the tab's own page lists everything.
    mockMyRequests.mockImplementation(async ({ status: states }: { status?: string[] }) =>
      states ? { items: [waitingPlaylist], total: 1, page: 1, limit: 200, pages: 1 } : twoWaiting,
    );
    const { router } = renderDownloads();

    fireEvent.click(await screen.findByRole("button", { name: /Requests\s*1/ }));

    await waitFor(() => expect(router.state.location.search).toEqual({ tab: "requests" }));
    expect(await screen.findByRole("link", { name: "Rap 2010" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Box Set" })).toBeInTheDocument();
    // The track filter and Retry all belong to the tracks, not to the requests.
    expect(screen.queryByRole("searchbox", { name: "Filter by title, artist…" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry all failed" })).not.toBeInTheDocument();
  });

  it("?tab=requests opens it directly", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    mockMyRequests.mockResolvedValue(twoWaiting);
    renderDownloads("/downloads?tab=requests");

    expect(await screen.findByRole("link", { name: "Rap 2010" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Requests/ })).toHaveAttribute("aria-pressed", "true");
  });

  it.each(["admin", "open"] as const)("%s has no requests and no tab: the URL falls back to All", async (mode) => {
    status = viewerStatus(mode);
    renderDownloads("/downloads?tab=requests");

    await waitFor(() => expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: /Requests/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^All/ })).toHaveAttribute("aria-pressed", "true");
    // Nothing is asked of the server: they have none.
    expect(mockMyRequests).not.toHaveBeenCalled();
  });
});

describe("parseDownloadsSearch", () => {
  it("keeps a known tab and falls back to All otherwise", () => {
    expect(parseDownloadsSearch({ tab: "review" })).toEqual({ tab: "review" });
    expect(parseDownloadsSearch({ tab: "requests" })).toEqual({ tab: "requests" });
    expect(parseDownloadsSearch({ tab: "nope" })).toEqual({ tab: "all" });
    expect(parseDownloadsSearch({ tab: "toString" })).toEqual({ tab: "all" });
    expect(parseDownloadsSearch({})).toEqual({ tab: "all" });
  });
});

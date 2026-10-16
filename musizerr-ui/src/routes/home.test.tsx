import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { HomePage } from "./home";
import type { DownloadsSummary } from "@/lib/api-client";
import type { Album, AuthConfig } from "@/types";

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const album = (i: number): Album =>
  ({
    kind: "album", title: `Album ${i}`, artist: "Artist", artists: [], year: 2026, cover_url: null,
    provider: "prov", provider_id: `al${i}`, external_ids: [], providers: ["prov"], tracks: [], track_count: 10,
  }) as unknown as Album;

const mockSummary = vi.fn();
const mockRetryFailed = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getDownloadsSummary: () => mockSummary(),
  retryFailedDownloads: () => mockRetryFailed(),
  getNewReleases: async () => ({ items: Array.from({ length: 14 }, (_, i) => album(i)), provider_errors: [] }),
  getTrendingTracks: async () => ({ items: Array.from({ length: 12 }, (_, i) => makeTrack({ provider_id: `t${i}`, title: `Hit ${i}` })), provider_errors: [] }),
  getTrendingAlbums: async () => ({ items: [], provider_errors: [{ provider: "prov", error_code: "unreachable", error_message: "down" }] }),
  getTrendingArtists: async () => ({ items: [], provider_errors: [] }),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
  refreshAccessToken: vi.fn(),
}));

const summary = (p: Partial<DownloadsSummary["phases"]>, since = 0): DownloadsSummary => ({
  statuses: {} as DownloadsSummary["statuses"],
  phases: { queued: 0, active: 0, done: 0, failed: 0, review: 0, ...p },
  total: 0,
  downloaded_since: since,
});

const renderHome = () => renderPage(HomePage, { path: "/", queue: true, otherPaths: ["/downloads"] });

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
  mockSummary.mockReset();
  mockRetryFailed.mockReset();
});

describe("Home — download status cards", () => {
  it("counts what is on its way, what needs a look and what arrived this week", async () => {
    mockSummary.mockResolvedValue(summary({ active: 1, queued: 2, failed: 1, review: 3 }, 5));
    renderHome();

    const cards = await screen.findByRole("region", { name: "Your downloads" });
    const link = (name: RegExp) => within(cards).getByRole("link", { name });
    expect(link(/3\s*Downloading/)).toHaveAttribute("href", "/downloads?tab=active");
    expect(link(/4\s*Need review/)).toHaveAttribute("href", "/downloads?tab=review");
    expect(link(/5\s*Added this week/)).toHaveAttribute("href", "/downloads?tab=done");
  });

  it("Retry all is offered only with failures, and asks the server", async () => {
    mockSummary.mockResolvedValue(summary({ failed: 2 }));
    mockRetryFailed.mockResolvedValue({ download_ids: ["a", "b"] });
    renderHome();
    fireEvent.click(await screen.findByRole("button", { name: "Retry all" }));
    await waitFor(() => expect(mockRetryFailed).toHaveBeenCalledTimes(1));
  });

  it("nothing to review, nothing to retry", async () => {
    mockSummary.mockResolvedValue(summary({ review: 1 }));
    renderHome();
    await screen.findByRole("region", { name: "Your downloads" });
    expect(screen.queryByRole("button", { name: "Retry all" })).not.toBeInTheDocument();
  });

  it("a guest gets no cards and never asks for the counters", async () => {
    status = viewerStatus("guest");
    renderHome();
    await screen.findByRole("list", { name: "New Releases" });
    expect(screen.queryByRole("region", { name: "Your downloads" })).not.toBeInTheDocument();
    expect(mockSummary).not.toHaveBeenCalled();
  });
});

describe("Home — discover", () => {
  it("new releases as tiles, the top 10 trending tracks, and which provider did not answer", async () => {
    mockSummary.mockResolvedValue(summary({}));
    renderHome();

    const releases = await screen.findByRole("list", { name: "New Releases" });
    expect(within(releases).getAllByRole("listitem")).toHaveLength(14);
    expect(screen.getByRole("button", { name: "Show all (14)" })).toBeInTheDocument();

    const trending = await screen.findByRole("list", { name: "Trending Tracks" });
    expect(within(trending).getAllByRole("listitem")).toHaveLength(10);
    fireEvent.click(screen.getByRole("button", { name: /Load more/ }));
    expect(within(trending).getAllByRole("listitem")).toHaveLength(12);

    // An empty section is not shown; the provider that failed is named.
    expect(screen.queryByRole("heading", { name: "Trending Albums" })).not.toBeInTheDocument();
    expect(await screen.findByText(/Provider/)).toBeInTheDocument();
  });
});

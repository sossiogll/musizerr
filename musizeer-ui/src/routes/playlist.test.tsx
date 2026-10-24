import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { PlaylistPage, plainText } from "./playlist";
import type { Playlist } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("user"), user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

let playlist: Playlist;
const mockGetPlaylist = vi.fn();
const mockAddFavorites = vi.fn();
const mockDownloadPlaylist = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getPlaylist: () => mockGetPlaylist(),
  addFavorites: (...a: unknown[]) => mockAddFavorites(...a),
  downloadPlaylist: (...a: unknown[]) => mockDownloadPlaylist(...a),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const base = (extra: Partial<Playlist> = {}): Playlist => ({
  kind: "playlist", internal_id: "p", name: "Road Trip", provider: "prov", provider_id: "pl1",
  description: 'Songs for the <a href="https://example.com">road</a> &amp; more', cover_url: null,
  tracks: [makeTrack({ provider_id: "t1", title: "Drive" }), makeTrack({ provider_id: "t2", title: "Cruise" })],
  track_count: 2, tracks_unavailable: null, external_ids: [],
  library: { total: 2, downloaded: 0, in_progress: 0, failed: 0 },
  ...extra,
});

const renderPlaylist = () => renderPage(PlaylistPage, { path: "/playlist/$provider/$id", url: "/playlist/prov/pl1", queue: true });

beforeEach(() => {
  connectivityMock.reset();
  playlist = base();
  mockGetPlaylist.mockReset().mockImplementation(async () => playlist);
  mockAddFavorites.mockReset().mockResolvedValue({ added: ["lib-a"], skipped: [] });
  mockDownloadPlaylist.mockReset().mockResolvedValue({ download_ids: ["d"], total_tracks: 1, skipped: 1 });
});

describe("Playlist header actions", () => {
  const partly = () =>
    base({
      tracks: [
        makeTrack({ provider_id: "t1", title: "Drive", download_status: "downloaded", in_library: true, library_id: "lib-a" }),
        makeTrack({ provider_id: "t2", title: "Cruise" }),
      ],
      library: { total: 2, downloaded: 1, in_progress: 0, failed: 0 },
      favorites: { favorite: 0, addable: 1 },
    });

  it("downloads only the missing ones, and adds the one in library to favorites", async () => {
    playlist = partly();
    renderPlaylist();

    fireEvent.click(await screen.findByRole("button", { name: "Download the missing track and add it to favorites" }));
    await waitFor(() => expect(mockDownloadPlaylist).toHaveBeenCalledWith("prov", "pl1", { onlyMissing: true, addToFavorites: true }));

    fireEvent.click(screen.getByRole("button", { name: "Add the track in library to favorites" }));
    await waitFor(() => expect(mockAddFavorites).toHaveBeenCalledWith(["lib-a"]));
    // The page asks the server again for the new state.
    await waitFor(() => expect(mockGetPlaylist).toHaveBeenCalledTimes(2));
  });

  it("the coverage counts what the rows show as in library", async () => {
    playlist = partly();
    renderPlaylist();
    const list = await screen.findByRole("list", { name: "Road Trip" });
    expect(list.querySelectorAll('[title="In library"]')).toHaveLength(1);
    expect(screen.getByRole("img", { name: "1 of 2 in library" })).toBeInTheDocument();
  });
});

describe("Playlist page", () => {
  it("shows the tracks, the description as plain text and the provider by its label", async () => {
    renderPlaylist();
    expect(await screen.findByRole("heading", { level: 1, name: "Road Trip" })).toBeInTheDocument();
    expect(screen.getByText("Songs for the road & more")).toBeInTheDocument();
    expect(await screen.findByText("Playlist · Provider")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Road Trip" }).querySelectorAll("li")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Download all and add them to favorites" })).toBeEnabled();
  });

  it("tracks that cannot be read: says why, Download all disabled, Retry asks again", async () => {
    playlist = base({ tracks: [], track_count: 30, tracks_unavailable: { code: "access_restricted", message: "Forbidden by upstream" } });
    renderPlaylist();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This playlist is private");
    expect(alert).not.toHaveTextContent("Forbidden by upstream");
    expect(screen.getByRole("button", { name: "Download all and add them to favorites" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockGetPlaylist).toHaveBeenCalledTimes(2));
  });
});

describe("plainText", () => {
  it("keeps the text of markup, never the markup", () => {
    expect(plainText('<img src=x onerror="alert(1)">Hi <b>there</b>')).toBe("Hi there");
    expect(plainText("Tom &amp; Jerry")).toBe("Tom & Jerry");
    expect(plainText("plain")).toBe("plain");
    expect(plainText(null)).toBe("");
  });
});

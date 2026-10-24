import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { TrackRow } from "@/components/tracks/TrackRow";
import type { AuthConfig, Track, ViewerDownloadPolicy } from "@/types";

// The track menu through the real row and the REAL menu (Radix): what each
// kind of viewer is offered, what each entry does, and that the "⋯" button
// and a right click are the same menu.

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const player = { play: vi.fn(), playNext: vi.fn(), addToQueue: vi.fn() };
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => ({ track: null, playing: false, ...player }),
}));

const api = { download: vi.fn(), removeFavorite: vi.fn(), addFavorite: vi.fn() };
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  downloadTrack: (...a: unknown[]) => api.download(...a),
  addFavorite: (...a: unknown[]) => api.addFavorite(...a),
  removeFavorite: (...a: unknown[]) => api.removeFavorite(...a),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getUserPlaylists: async () => ({ items: [], max_tracks: 500 }),
  getMyRequests: async () => ({ items: [], total: 0, page: 1, limit: 200, pages: 1 }),
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const onRemove = vi.fn();
const renderRow = (track: Track = makeTrack(), remove?: () => void) =>
  renderPage(() => <TrackRow track={track} onRemove={remove} />, { path: "/", otherPaths: ["/album/prov/al1", "/artist/prov/ar1"], queue: true });

const button = () => screen.findByRole("button", { name: "More actions for Song A" });
const openMenu = async () => {
  fireEvent.click(await button());
  await screen.findByRole("menu");
};
const items = () => screen.getAllByRole("menuitem").map((i) => i.textContent);

const inLibrary = (over: Partial<Track> = {}) => makeTrack({ download_status: "downloaded", in_library: true, library_id: "lib-1", favorite: false, ...over });

const approvalOnTracks: ViewerDownloadPolicy = {
  ...DEFAULT_POLICY,
  rules: { ...DEFAULT_POLICY.rules!, track: { mode: "approval", threshold: 1, auto_up_to: 0 } },
};

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
  Object.values(player).forEach((f) => f.mockReset());
  Object.values(api).forEach((f) => f.mockReset());
  Object.values(toast).forEach((f) => f.mockReset());
  onRemove.mockReset();
});

describe("TrackMenu — what each viewer is offered", () => {
  it("a user, for a track that is not in the library", async () => {
    renderRow();
    await openMenu();
    expect(items()).toEqual(["Play next", "Add to queue", "Add to playlist", "Download and add to favorites", "Go to album", "Go to artist"]);
  });

  it("an admin has both downloads, the plain one first", async () => {
    status = viewerStatus("admin");
    renderRow();
    await openMenu();
    expect(items()).toEqual(["Play next", "Add to queue", "Add to playlist", "Download", "Download and add to favorites", "Go to album", "Go to artist"]);
  });

  it("a guest has what needs no account: the queue and the links", async () => {
    status = viewerStatus("guest");
    renderRow();
    await openMenu();
    expect(items()).toEqual(["Play next", "Add to queue", "Go to album", "Go to artist"]);
  });

  it("a track in the library has its favorite and no download", async () => {
    renderRow(inLibrary());
    await openMenu();
    expect(items()).toEqual(["Play next", "Add to queue", "Add to playlist", "Add to favorites", "Go to album", "Go to artist"]);
  });

  it("a track that cannot be played has no queue entries", async () => {
    renderRow(makeTrack({ playback: { type: "none", source: null }, preview_url: null }));
    await openMenu();
    expect(items()).toEqual(["Add to playlist", "Download and add to favorites", "Go to album", "Go to artist"]);
  });

  it("goes only where it knows the way: no album or artist id, no link", async () => {
    renderRow(makeTrack({ album_provider_id: "", artist_provider_id: "" }));
    await openMenu();
    expect(items()).toEqual(["Play next", "Add to queue", "Add to playlist", "Download and add to favorites"]);
  });

  it("a failed download offers Retry in place of Download", async () => {
    renderRow(makeTrack({ download_status: "failed" }));
    await openMenu();
    expect(items()).toContain("Retry");
    expect(items()).not.toContain("Download and add to favorites");
  });

  it("when the policy makes the download wait, the entry says Request, with the policy's own hint", async () => {
    status = viewerStatus("user", approvalOnTracks);
    renderRow();
    await openMenu();
    const request = screen.getByRole("menuitem", { name: "Request download (needs approval)" });
    expect(request).toHaveAttribute("title", "Needs approval: 1 track");
    expect(screen.queryByRole("menuitem", { name: "Download and add to favorites" })).not.toBeInTheDocument();
  });

  it("the playlists and the download are out of reach while the server is", async () => {
    connectivityMock.set({ online: false });
    renderRow();
    await openMenu();
    expect(screen.getByRole("menuitem", { name: "Add to playlist" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("menuitem", { name: "Download and add to favorites" })).toHaveAttribute("aria-disabled", "true");
  });
});

describe("TrackMenu — what the entries do", () => {
  it("Play next and Add to queue go to the player and say so", async () => {
    renderRow();
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Play next" }));
    expect(player.playNext).toHaveBeenCalledTimes(1);
    expect(player.playNext.mock.calls[0][0]).toMatchObject({ provider: "prov", provider_id: "p1", title: "Song A" });
    expect(toast.success).toHaveBeenCalledWith("Playing next: Song A");

    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Add to queue" }));
    expect(player.addToQueue).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith("Added to queue: Song A");
  });

  it("Download starts the download the slot would, with the favorite a user always gets", async () => {
    api.download.mockResolvedValue({ status: "queued", download_id: "d1", library_id: "lib-1", track: makeTrack({ download_status: "pending", library_id: "lib-1", favorite_pending: true }) });
    renderRow();
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Download and add to favorites" }));
    await waitFor(() => expect(api.download).toHaveBeenCalledWith("prov", "p1", [], { addToFavorites: true }));
    // The row's own slot shows it queued: ONE download state for the row and its menu.
    expect(await screen.findByTitle("Queued")).toBeInTheDocument();
  });

  it("the favorite entry toggles the heart", async () => {
    api.addFavorite.mockResolvedValue({ track_id: "lib-1", favorite: true });
    renderRow(inLibrary());
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Add to favorites" }));
    await waitFor(() => expect(api.addFavorite).toHaveBeenCalledWith("lib-1"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove Song A from favorites" })).toHaveAttribute("aria-pressed", "true"));
  });

  it("a favorite is taken off the same way", async () => {
    api.removeFavorite.mockResolvedValue({ track_id: "lib-1", favorite: false });
    renderRow(inLibrary({ favorite: true }));
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove from favorites" }));
    await waitFor(() => expect(api.removeFavorite).toHaveBeenCalledWith("lib-1"));
  });

  it("Go to album and Go to artist navigate to the provider's pages", async () => {
    const { router } = renderRow();
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Go to album" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/album/prov/al1"));
  });

  it("Go to artist", async () => {
    const { router } = renderRow();
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Go to artist" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/artist/prov/ar1"));
  });
});

describe("TrackMenu — inside a playlist", () => {
  it("offers Remove from this playlist, last and marked, only where the playlist can be edited", async () => {
    renderRow(makeTrack(), onRemove);
    await openMenu();
    expect(items()[items().length - 1]).toBe("Remove from this playlist");
    expect(screen.getByRole("menuitem", { name: "Remove from this playlist" }).className).toContain("text-destructive");
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove from this playlist" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("and not elsewhere", async () => {
    renderRow();
    await openMenu();
    expect(items()).not.toContain("Remove from this playlist");
  });
});

describe("TrackMenu — one menu, three ways in", () => {
  it("a right click on the row opens the very same menu as the button", async () => {
    renderRow();
    await openMenu();
    const fromButton = items();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());

    fireEvent.contextMenu(screen.getByText("Song A"), { clientX: 30, clientY: 40 });
    await screen.findByRole("menu", { name: "More actions for Song A" });
    expect(items()).toEqual(fromButton);
  });

  it("the menu is named after the track and carries its title as the heading", async () => {
    renderRow();
    await openMenu();
    expect(screen.getByRole("menu", { name: "More actions for Song A" })).toHaveTextContent("Song A");
  });
});

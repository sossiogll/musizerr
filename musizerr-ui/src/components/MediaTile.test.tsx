import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { I18nProvider } from "@/i18n";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { waitingPlaylist } from "@/test/download-requests";
import { makeTrack } from "@/test/fixtures";
import { MediaTile } from "./MediaTile";
import type { Album, Artist, AuthConfig, DownloadRequest, Playlist, UserPlaylist } from "@/types";

// A tile is a cover and a link; everything it can do is in ITS menu (the "⋯",
// a right click, a long press), built from what the viewer may do.

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/contexts/QueueContext", () => ({ useQueueActions: () => vi.fn() }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const player = { play: vi.fn(), addToQueue: vi.fn() };
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => ({ track: null, playing: false, ...player }),
}));

const api = {
  album: vi.fn(),
  artist: vi.fn(),
  playlist: vi.fn(),
  getAlbum: vi.fn(),
  getPlaylist: vi.fn(),
  getTop: vi.fn(),
  follow: vi.fn(),
};
let waiting: DownloadRequest[] = [];
let mine: UserPlaylist[] = [];
// The capabilities the artist's provider declares (GET /providers/public).
let declared: string[] = ["discography"];
vi.mock("@/lib/api-client", () => ({
  getPublicProviders: async () => ({
    providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: ["prov"], metadata_capabilities: { prov: declared },
  }),
  downloadAlbum: (...a: unknown[]) => api.album(...a),
  downloadArtist: (...a: unknown[]) => api.artist(...a),
  downloadPlaylist: (...a: unknown[]) => api.playlist(...a),
  getAlbum: (...a: unknown[]) => api.getAlbum(...a),
  getPlaylist: (...a: unknown[]) => api.getPlaylist(...a),
  getArtistTopTracks: (...a: unknown[]) => api.getTop(...a),
  followPlaylist: (...a: unknown[]) => api.follow(...a),
  getUserPlaylists: async () => ({ items: mine, max_tracks: 500 }),
  getMyRequests: async () => ({ items: waiting, total: waiting.length, page: 1, limit: 200, pages: 1 }),
}));

const base = { internal_id: "x", provider: "prov", provider_id: "id1", cover_url: null, external_ids: [], providers: [] };
const album: Album = { ...base, kind: "album", title: "An Album", artist: "Someone", year: 2020, track_count: 10, artist_provider_id: "a", artists: [], tracks: [], album_type: "album" };
const artist: Artist = { ...base, kind: "artist", name: "Someone" };
// A playlist has `name` and no `title`, like an artist: only `kind` tells them apart.
const playlist: Playlist = { ...base, kind: "playlist", name: "Rap 2010", description: "", tracks: [], track_count: 20, tracks_unavailable: null };

const followedCopy = (): UserPlaylist => ({
  id: "mine-1", name: "Rap 2010", description: "", kind: "followed", source_provider: "prov", source_id: "id1", cover_url: null, track_count: 20,
  auto_download: false, tracks_unavailable: null, refresh_error_code: null, checked_at: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", synced_to: [],
});

function renderTile(ui: ReactNode) {
  const root = createRootRoute({ component: () => <>{ui}</> });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/** Opens the menu of the tile named `name` and returns the names of its entries. */
async function openMenu(name: string) {
  fireEvent.click(await screen.findByRole("button", { name: `More actions for ${name}` }));
  await screen.findByRole("menu");
  return screen.getAllByRole("menuitem").map((i) => i.textContent);
}
const entry = (name: string) => screen.getByRole("menuitem", { name });

beforeEach(() => {
  status = viewerStatus("user");
  waiting = [];
  mine = [];
  declared = ["discography"];
  Object.values(api).forEach((m) => m.mockReset());
  Object.values(player).forEach((m) => m.mockReset());
  Object.values(toast).forEach((m) => m.mockReset());
  [api.album, api.artist, api.playlist].forEach((m) => m.mockResolvedValue({ download_ids: ["d"], total_tracks: 3 }));
});

describe("MediaTile — rendered by kind", () => {
  it("a playlist links to the playlist page and downloads as a playlist (not an artist)", async () => {
    renderTile(<MediaTile item={playlist} downloadable />);
    expect(await screen.findByRole("link", { name: /Rap 2010/ })).toHaveAttribute("href", "/playlist/prov/id1");
    await openMenu("Rap 2010");
    // A user's download adds to favorites, and its name says so.
    fireEvent.click(entry("Download playlist and add to favorites"));
    await waitFor(() => expect(api.playlist).toHaveBeenCalledWith("prov", "id1", { onlyMissing: false, addToFavorites: true }));
    expect(api.artist).not.toHaveBeenCalled();
  });

  it("an album links to the album page and downloads at once", async () => {
    renderTile(<MediaTile item={album} downloadable />);
    expect(await screen.findByRole("link", { name: /An Album/ })).toHaveAttribute("href", "/album/prov/id1");
    expect(screen.getByText("Someone · 2020")).toBeInTheDocument();
    await openMenu("An Album");
    fireEvent.click(entry("Download album and add to favorites"));
    await waitFor(() => expect(api.album).toHaveBeenCalledWith("prov", "id1", { onlyMissing: false, addToFavorites: true }));
  });

  it("downloading a whole artist asks for confirmation first", async () => {
    renderTile(<MediaTile item={artist} downloadable />);
    await openMenu("Someone");
    fireEvent.click(entry("Download discography and add to favorites"));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Download everything by Someone?");
    expect(api.artist).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Download everything" }));
    await waitFor(() => expect(api.artist).toHaveBeenCalledWith("prov", "id1", { onlyMissing: false, addToFavorites: true }));
  });

  it("an artist whose provider gives no discography has no download, while an album of it keeps its own", async () => {
    declared = [];
    renderTile(
      <>
        <MediaTile item={artist} downloadable />
        <MediaTile item={album} downloadable />
      </>,
    );

    expect(await openMenu("An Album")).toContain("Download album and add to favorites");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(await openMenu("Someone")).toEqual(["Play", "Add to queue", "Go to artist"]);
  });

  it("an admin's tile downloads without adding to favorites, or with", async () => {
    status = viewerStatus("admin");
    api.album.mockResolvedValue({ download_ids: ["d1"], total_tracks: 1 });
    renderTile(<MediaTile item={album} downloadable />);
    expect(await openMenu("An Album")).toEqual(["Play", "Add to queue", "Download album", "Download album and add to favorites", "Go to album"]);
    fireEvent.click(entry("Download album"));
    await waitFor(() => expect(api.album).toHaveBeenCalledWith("prov", "id1", { onlyMissing: false, addToFavorites: false }));
  });

  it("without `downloadable` it offers no download at all", async () => {
    renderTile(<MediaTile item={album} />);
    expect(await openMenu("An Album")).toEqual(["Play", "Add to queue", "Go to album"]);
  });

  describe("under an approval policy", () => {
    beforeEach(() => {
      status = viewerStatus("user", DEFAULT_POLICY);
    });

    it("a playlist, which always waits, offers a Request that says so", async () => {
      api.playlist.mockResolvedValue({ status: "pending_approval", request_id: "r", kind: "playlist", needed_tracks: 20, reason: "kind_requires_approval" });
      renderTile(<MediaTile item={playlist} downloadable />);

      await openMenu("Rap 2010");
      const request = entry("Request download (needs approval)");
      expect(request).toHaveAttribute("title", "Needs approval: 20 tracks");
      expect(screen.queryByRole("menuitem", { name: /^Download/ })).not.toBeInTheDocument();

      fireEvent.click(request);
      await waitFor(() => expect(api.playlist).toHaveBeenCalledWith("prov", "id1", { onlyMissing: false, addToFavorites: true }));
    });

    it("an album within what goes through at once keeps its Download", async () => {
      renderTile(<MediaTile item={album} downloadable />);
      expect(await openMenu("An Album")).toContain("Download album and add to favorites");
    });

    it("an album bigger than that offers a Request, by the library's own count of what is missing", async () => {
      renderTile(<MediaTile item={{ ...album, track_count: 45, library: { total: 45, downloaded: 10, in_progress: 0, failed: 0 } }} downloadable />);

      // 35 missing: over the 30 that go through at once.
      await openMenu("An Album");
      expect(entry("Request download (needs approval)")).toHaveAttribute("title", "Needs approval: 35 tracks (up to 30 go through at once)");
    });

    it("an artist's tile says Request, and its confirmation says an administrator decides", async () => {
      renderTile(<MediaTile item={artist} downloadable />);

      await openMenu("Someone");
      fireEvent.click(entry("Request download (needs approval)"));
      const dialog = await screen.findByRole("alertdialog");
      expect(dialog).toHaveTextContent("Send a request for Someone?");
      expect(api.artist).not.toHaveBeenCalled();
    });

    it("once asked, the tile shows 'Waiting for approval', and the menu has nothing to request", async () => {
      waiting = [{ ...waitingPlaylist, target: { provider: "prov", id: "id1" } }];
      renderTile(<MediaTile item={playlist} downloadable />);

      expect(await screen.findByRole("status", { name: "Rap 2010: waiting for approval" })).toHaveAttribute("title", "Waiting for approval");
      await openMenu("Rap 2010");
      expect(entry("Waiting for approval")).toHaveAttribute("aria-disabled", "true");
      expect(screen.queryByRole("menuitem", { name: /Request|^Download/ })).not.toBeInTheDocument();
    });
  });

  it("a guest has the menu of what needs no account", async () => {
    status = viewerStatus("guest");
    renderTile(<MediaTile item={album} downloadable />);
    expect(await openMenu("An Album")).toEqual(["Play", "Add to queue", "Go to album"]);
  });
});

describe("MediaTile — one menu, three ways in", () => {
  it("has ONE menu button, and a right click on the tile opens the very same menu", async () => {
    renderTile(<MediaTile item={album} downloadable />);
    expect(await screen.findAllByRole("button", { name: "More actions for An Album" })).toHaveLength(1);
    const fromButton = await openMenu("An Album");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());

    fireEvent.contextMenu(screen.getByRole("link", { name: /An Album/ }), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu", { name: "More actions for An Album" });
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual(fromButton);
  });
});

describe("MediaTile — play and queue", () => {
  const playable = (id: string) => makeTrack({ provider_id: id, title: `Song ${id}`, playback: { type: "full", source: "library" }, stream_url: `/s/${id}` });
  const mute = (id: string) => makeTrack({ provider_id: id, title: `Song ${id}`, playback: { type: "none", source: null }, preview_url: null });

  it("Play reads the album when asked and plays it as the queue", async () => {
    api.getAlbum.mockResolvedValue({ ...album, tracks: [playable("1"), playable("2")] });
    renderTile(<MediaTile item={album} />);
    await openMenu("An Album");
    expect(api.getAlbum).not.toHaveBeenCalled();
    fireEvent.click(entry("Play"));

    await waitFor(() => expect(player.play).toHaveBeenCalledTimes(1));
    expect(api.getAlbum).toHaveBeenCalledWith("prov", "id1");
    const [first, queue] = player.play.mock.calls[0];
    expect(first.provider_id).toBe("1");
    expect(queue.map((t: { provider_id: string }) => t.provider_id)).toEqual(["1", "2"]);
  });

  it("Add to queue adds the tracks that can play, in order, and says how many", async () => {
    api.getPlaylist.mockResolvedValue({ ...playlist, tracks: [playable("1"), mute("2"), playable("3")] });
    renderTile(<MediaTile item={playlist} />);
    await openMenu("Rap 2010");
    fireEvent.click(entry("Add to queue"));

    await waitFor(() => expect(player.addToQueue).toHaveBeenCalledTimes(2));
    expect(player.addToQueue.mock.calls.map(([t]) => t.provider_id)).toEqual(["1", "3"]);
    expect(toast.success).toHaveBeenCalledWith("Added 2 tracks to the queue: Rap 2010");
  });

  it("an artist plays its most popular tracks", async () => {
    api.getTop.mockResolvedValue({ items: [playable("9")], error_code: null, error_message: null, provider_errors: [] });
    renderTile(<MediaTile item={artist} />);
    await openMenu("Someone");
    expect(entry("Play")).toHaveAttribute("title", "Its most popular tracks");
    fireEvent.click(entry("Play"));
    await waitFor(() => expect(player.play).toHaveBeenCalledTimes(1));
    expect(api.getTop).toHaveBeenCalledWith("prov", "id1");
  });

  it("says so when nothing in it can play, and plays nothing", async () => {
    api.getAlbum.mockResolvedValue({ ...album, tracks: [mute("1")] });
    renderTile(<MediaTile item={album} />);
    await openMenu("An Album");
    fireEvent.click(entry("Play"));
    await waitFor(() => expect(toast.info).toHaveBeenCalledWith("Nothing to play in An Album"));
    expect(player.play).not.toHaveBeenCalled();
  });

  it("says so when its tracks cannot be read", async () => {
    api.getAlbum.mockRejectedValue(new Error("down"));
    renderTile(<MediaTile item={album} />);
    await openMenu("An Album");
    fireEvent.click(entry("Add to queue"));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Could not load the tracks of An Album"));
    expect(player.addToQueue).not.toHaveBeenCalled();
  });
});

describe("MediaTile — following a playlist", () => {
  it("a playlist of a provider can be followed from its menu", async () => {
    api.follow.mockResolvedValue({ created: true, playlist: followedCopy(), download: { status: "none" } });
    renderTile(<MediaTile item={playlist} />);
    await openMenu("Rap 2010");
    fireEvent.click(entry("Follow"));
    await waitFor(() => expect(api.follow).toHaveBeenCalledWith("prov", "id1"));
  });

  it("a followed one offers to open it among the viewer's own, and to unfollow it", async () => {
    mine = [followedCopy()];
    renderTile(<MediaTile item={playlist} />);
    await waitFor(async () => expect(await openMenu("Rap 2010")).toContain("Unfollow…"));
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["Play", "Add to queue", "Open in my playlists", "Unfollow…", "Open playlist"]);
    fireEvent.click(entry("Unfollow…"));
    const dialog = await screen.findByRole("alertdialog");
    // Unfollowing asks whether to delete the tracks too, and starts at no.
    expect(within(dialog).getByRole("switch")).not.toBeChecked();
  });

  it("an album or an artist has no Follow", async () => {
    renderTile(<MediaTile item={album} />);
    expect(await openMenu("An Album")).not.toContain("Follow");
  });

  it("a guest, who has no playlists, has no Follow either", async () => {
    status = viewerStatus("guest");
    renderTile(<MediaTile item={playlist} />);
    expect(await openMenu("Rap 2010")).toEqual(["Play", "Add to queue", "Open playlist"]);
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { connectivityMock } from "@/test/connectivity-mock";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { PlaylistsPage } from "./playlists";
import { makeTrack } from "@/test/fixtures";
import type { UserPlaylist } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("user"), user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const player = { play: vi.fn(), addToQueue: vi.fn() };
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => ({ track: null, playing: false, ...player }),
}));

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockGet = vi.fn();
const mockUpdate = vi.fn();
const mockDuplicate = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getUserPlaylists: () => mockList(),
  createUserPlaylist: (...a: unknown[]) => mockCreate(...a),
  getUserPlaylist: (...a: unknown[]) => mockGet(...a),
  updateUserPlaylist: (...a: unknown[]) => mockUpdate(...a),
  duplicateUserPlaylist: (...a: unknown[]) => mockDuplicate(...a),
  deleteUserPlaylist: (...a: unknown[]) => mockDelete(...a),
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
}));

const playlist = (over: Partial<UserPlaylist>): UserPlaylist => ({
  id: "a", name: "Road trip", description: "", kind: "own", source_provider: null, source_id: null, cover_url: null,
  track_count: 3, auto_download: false, tracks_unavailable: null, refresh_error_code: null, checked_at: null,
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", synced_to: [], ...over,
});

const renderList = () => renderPage(PlaylistsPage, { path: "/playlists", otherPaths: ["/playlists/$id"] });

beforeEach(() => {
  connectivityMock.reset();
  Object.values(toast).forEach((f) => f.mockReset());
  mockList.mockReset().mockResolvedValue({ items: [], max_tracks: 500 });
  mockCreate.mockReset().mockResolvedValue(playlist({ id: "new", name: "Fresh", track_count: 0 }));
  mockGet.mockReset();
  mockUpdate.mockReset().mockResolvedValue({});
  mockDuplicate.mockReset().mockResolvedValue({ id: "copy", name: "Road trip (copy)" });
  mockDelete.mockReset().mockResolvedValue({ deleted: true, tracks_scheduled: 0 });
  Object.values(player).forEach((f) => f.mockReset());
});

describe("Playlists page", () => {
  it("invites to create one, or to follow one, when there is none", async () => {
    renderList();
    expect(await screen.findByText("You have no playlists yet. Create one, or follow a playlist from a provider.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Playlists" })).not.toBeInTheDocument();
  });

  it("lists own and followed playlists, each a link, with its track count and where it comes from", async () => {
    mockList.mockResolvedValue({
      items: [
        playlist({ id: "a", name: "Road trip", track_count: 3 }),
        playlist({ id: "f", name: "Mix", kind: "followed", source_provider: "prov", source_id: "s1", track_count: 1 }),
      ],
      max_tracks: 500,
    });
    renderList();

    const list = await screen.findByRole("list", { name: "Playlists" });
    const cards = within(list).getAllByRole("link");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent("Road trip");
    expect(cards[0]).toHaveTextContent("3 tracks");
    expect(cards[0]).not.toHaveTextContent("Followed");
    expect(cards[1]).toHaveTextContent("Mix");
    // The provider's name arrives with its labels.
    await waitFor(() => expect(cards[1]).toHaveTextContent("1 track · Followed from Provider"));
    expect(within(cards[1]).getByText("Followed")).toBeInTheDocument();
  });

  it("says when the playlists could not be loaded", async () => {
    mockList.mockRejectedValue(new Error("boom"));
    renderList();
    expect(await screen.findByText("Could not load your playlists")).toBeInTheDocument();
  });

  it("creates a playlist from its name and opens it", async () => {
    const { router } = renderList();
    fireEvent.click(await screen.findByRole("button", { name: "New playlist" }));

    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "  Fresh " } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith("Fresh"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/playlists/new"));
    expect(toast.success).toHaveBeenCalledWith("Created Fresh");
  });

  it("does not offer to create while the server cannot be reached", async () => {
    connectivityMock.set({ online: false });
    renderList();
    expect(await screen.findByRole("button", { name: "New playlist" })).toBeDisabled();
  });
});

describe("Playlists page — the menu of each playlist", () => {
  const withBoth = () =>
    mockList.mockResolvedValue({
      items: [
        playlist({ id: "a", name: "Road trip", track_count: 3 }),
        playlist({ id: "f", name: "Mix", kind: "followed", source_provider: "prov", source_id: "s1", track_count: 1 }),
      ],
      max_tracks: 500,
    });
  const openMenu = async (name: string) => {
    fireEvent.click(await screen.findByRole("button", { name: `More actions for ${name}` }));
    await screen.findByRole("menu");
    return screen.getAllByRole("menuitem").map((i) => i.textContent);
  };

  it("has one menu button on each card, outside the link, and the entries follow the kind", async () => {
    withBoth();
    renderList();

    const list = await screen.findByRole("list", { name: "Playlists" });
    expect(within(list).getAllByRole("button", { name: /^More actions for/ })).toHaveLength(2);
    expect(within(list).getAllByRole("link")).toHaveLength(2);
    expect(await openMenu("Road trip")).toEqual(["Play", "Add to queue", "Rename", "Duplicate as mine", "Open playlist", "Delete playlist"]);
  });

  it("a followed one cannot be renamed and is unfollowed instead of deleted", async () => {
    withBoth();
    renderList();
    expect(await openMenu("Mix")).toEqual(["Play", "Add to queue", "Duplicate as mine", "Open playlist", "Unfollow…"]);
  });

  it("a right click on a card opens the same menu", async () => {
    withBoth();
    renderList();
    const card = (await screen.findAllByRole("link"))[0];
    fireEvent.contextMenu(card, { clientX: 20, clientY: 20 });
    await screen.findByRole("menu", { name: "More actions for Road trip" });
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toContain("Rename");
  });

  it("renames from a dialog that starts from the current name", async () => {
    withBoth();
    renderList();
    await openMenu("Road trip");
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const name = await screen.findByLabelText("Name");
    expect(name).toHaveValue("Road trip");
    fireEvent.change(name, { target: { value: "Night drive" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("a", { name: "Night drive" }));
  });

  it("duplicates under a name in the viewer's language and opens the copy", async () => {
    withBoth();
    const { router } = renderList();
    await openMenu("Mix");
    fireEvent.click(screen.getByRole("menuitem", { name: "Duplicate as mine" }));
    await waitFor(() => expect(mockDuplicate).toHaveBeenCalledWith("f", "Mix (copy)"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/playlists/copy"));
  });

  it("deletes an own one after a confirmation with no choice about tracks, and stays on the list", async () => {
    withBoth();
    const { router } = renderList();
    await openMenu("Road trip");
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete playlist" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).queryByRole("switch")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("a", { deleteTracks: false }));
    expect(toast.success).toHaveBeenCalledWith("Deleted Road trip");
    expect(router.state.location.pathname).toBe("/playlists");
  });

  it("unfollows keeping the downloaded tracks unless the viewer says otherwise", async () => {
    withBoth();
    mockDelete.mockResolvedValue({ deleted: true, tracks_scheduled: 1 });
    renderList();
    await openMenu("Mix");
    fireEvent.click(screen.getByRole("menuitem", { name: "Unfollow…" }));
    const dialog = await screen.findByRole("alertdialog");
    const choice = within(dialog).getByRole("switch", { name: "Also delete the tracks downloaded for this playlist" });
    expect(choice).not.toBeChecked();
    fireEvent.click(choice);
    fireEvent.click(within(dialog).getByRole("button", { name: "Unfollow" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("f", { deleteTracks: true }));
    expect(toast.success).toHaveBeenCalledWith("Unfollowed Mix");
  });

  it("plays the playlist from its menu, reading its tracks only then", async () => {
    withBoth();
    mockGet.mockResolvedValue({ ...playlist({ id: "a" }), tracks: [makeTrack({ provider_id: "1", playback: { type: "full", source: "library" }, stream_url: "/s/1" })] });
    renderList();
    await openMenu("Road trip");
    expect(mockGet).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem", { name: "Play" }));
    await waitFor(() => expect(player.play).toHaveBeenCalledTimes(1));
    expect(mockGet).toHaveBeenCalledWith("a");
  });

  it("an empty playlist has nothing to play or queue", async () => {
    mockList.mockResolvedValue({ items: [playlist({ id: "e", name: "Empty", track_count: 0 })], max_tracks: 500 });
    renderList();
    await openMenu("Empty");
    expect(screen.getByRole("menuitem", { name: "Play" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("menuitem", { name: "Add to queue" })).toHaveAttribute("aria-disabled", "true");
  });

  it("changes nothing while the server cannot be reached", async () => {
    withBoth();
    renderList();
    await screen.findByRole("list", { name: "Playlists" });
    connectivityMock.set({ online: false });
    await openMenu("Road trip");
    for (const name of ["Rename", "Duplicate as mine", "Delete playlist"]) {
      expect(screen.getByRole("menuitem", { name })).toHaveAttribute("aria-disabled", "true");
    }
  });
});

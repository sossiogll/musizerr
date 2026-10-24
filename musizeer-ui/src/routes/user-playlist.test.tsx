import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { UserPlaylistPage } from "./user-playlist";
import type { UserPlaylistDetail, UserPlaylistTrack } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("user"), user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

let detail: UserPlaylistDetail;
const mockGet = vi.fn();
const mockReorder = vi.fn();
const mockRemove = vi.fn();
const mockUpdate = vi.fn();
const mockDuplicate = vi.fn();
const mockRefresh = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getUserPlaylist: () => mockGet(),
  getUserPlaylists: async () => ({ items: [], max_tracks: 5 }),
  reorderPlaylistTracks: (...a: unknown[]) => mockReorder(...a),
  removePlaylistTrack: (...a: unknown[]) => mockRemove(...a),
  updateUserPlaylist: (...a: unknown[]) => mockUpdate(...a),
  duplicateUserPlaylist: (...a: unknown[]) => mockDuplicate(...a),
  refreshUserPlaylist: (...a: unknown[]) => mockRefresh(...a),
  deleteUserPlaylist: (...a: unknown[]) => mockDelete(...a),
  getMyRequests: async () => ({ items: [], total: 0, page: 1, limit: 200, pages: 1 }),
  getFavorite: async (id: string) => ({ track_id: id, favorite: false, in_library: true }),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }, { id: "music", label: "Music server" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const track = (n: number): UserPlaylistTrack => ({
  ...makeTrack({ provider_id: `t${n}`, internal_id: `t${n}`, title: `Song ${n}`, library_id: `lib-${n}` }),
  added_at: "2026-10-01T00:00:00Z",
});

const base = (over: Partial<UserPlaylistDetail> = {}): UserPlaylistDetail => ({
  id: "pl", name: "Road trip", description: "For the car", kind: "own", source_provider: null, source_id: null, cover_url: null,
  track_count: 3, auto_download: false, tracks_unavailable: null, refresh_error_code: null, checked_at: null,
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", synced_to: [],
  tracks: [track(1), track(2), track(3)], ...over,
});

const followed = (over: Partial<UserPlaylistDetail> = {}) =>
  base({ kind: "followed", source_provider: "prov", source_id: "s1", name: "Mix", auto_download: true, checked_at: "2026-10-02T10:00:00Z", ...over });

const renderPlaylist = () => renderPage(UserPlaylistPage, { path: "/playlists/$id", url: "/playlists/pl", otherPaths: ["/playlists"], queue: true });
/** The playlist's own menu, in the page header (the rows' buttons are named after their songs). */
const openHeaderMenu = async () => fireEvent.click(await screen.findByRole("button", { name: /^More actions for (Road trip|Mix)$/ }));
const rowTitles = () => screen.getAllByRole("link", { name: /^Song \d$/ }).map((a) => a.textContent);

beforeEach(() => {
  connectivityMock.reset();
  Object.values(toast).forEach((f) => f.mockReset());
  detail = base();
  mockGet.mockReset().mockImplementation(async () => detail);
  mockReorder.mockReset().mockResolvedValue({ track_ids: [] });
  mockRemove.mockReset().mockResolvedValue({ track_id: "", removed: true });
  mockUpdate.mockReset().mockResolvedValue({});
  mockDuplicate.mockReset().mockResolvedValue({ id: "copy", name: "Road trip (copy)" });
  mockRefresh.mockReset().mockResolvedValue({
    playlist: {}, changed: false, new_tracks: 0, removed_tracks: 0, error_code: null, download: { status: "none" },
  });
  mockDelete.mockReset().mockResolvedValue({ deleted: true, tracks_scheduled: 0 });
});

describe("An own playlist", () => {
  it("shows its tracks in order, how many of the cap it holds, and where it is copied", async () => {
    detail = base({ synced_to: [{ sync_provider: "music", synced_at: "2026-10-02T10:00:00Z" }] });
    renderPlaylist();

    expect(await screen.findByRole("heading", { name: "Road trip" })).toBeInTheDocument();
    expect(rowTitles()).toEqual(["Song 1", "Song 2", "Song 3"]);
    expect(await screen.findByText("3 of 5 tracks · Also on Music server")).toBeInTheDocument();
    expect(screen.getByText("For the car")).toBeInTheDocument();
  });

  it("says when it is not copied to a music server yet", async () => {
    renderPlaylist();
    expect(await screen.findByText(/Not copied to a music server yet/)).toBeInTheDocument();
  });

  it("moves a track down by sending the whole new order", async () => {
    renderPlaylist();
    fireEvent.click(await screen.findByRole("button", { name: "Move Song 1 down" }));
    await waitFor(() => expect(mockReorder).toHaveBeenCalledWith("pl", ["lib-2", "lib-1", "lib-3"]));
  });

  it("moves a track up, and the first cannot go up nor the last down", async () => {
    renderPlaylist();
    fireEvent.click(await screen.findByRole("button", { name: "Move Song 3 up" }));
    await waitFor(() => expect(mockReorder).toHaveBeenCalledWith("pl", ["lib-1", "lib-3", "lib-2"]));
    expect(screen.getByRole("button", { name: "Move Song 1 up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Song 3 down" })).toBeDisabled();
  });

  it("takes a track out from the menu of its row, the same menu every list has", async () => {
    renderPlaylist();
    fireEvent.click(await screen.findByRole("button", { name: "More actions for Song 2" }));
    // The usual entries, and the one only an editable playlist adds, last.
    const names = screen.getAllByRole("menuitem").map((i) => i.textContent);
    expect(names).toEqual(expect.arrayContaining(["Play next", "Add to queue", "Add to playlist", "Go to album"]));
    expect(names[names.length - 1]).toBe("Remove from this playlist");
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove from this playlist" }));
    await waitFor(() => expect(mockRemove).toHaveBeenCalledWith("pl", "lib-2"));
  });

  it("has the playlists' own menu in its header, without Open (it is open), and no separate Duplicate button", async () => {
    renderPlaylist();
    await openHeaderMenu();
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["Play", "Add to queue", "Rename", "Duplicate as mine", "Delete playlist"]);
    expect(screen.queryByRole("button", { name: "Duplicate as mine" })).not.toBeInTheDocument();
  });

  it("goes back to the list of playlists once deleted", async () => {
    const { router } = renderPlaylist();
    await openHeaderMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete playlist" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/playlists"));
  });

  it("has no remove button on its rows any more, only the order controls", async () => {
    renderPlaylist();
    await screen.findByRole("heading", { name: "Road trip" });
    expect(screen.queryByRole("button", { name: /from the playlist$/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Move Song \d (up|down)$/ })).toHaveLength(6);
  });

  it("is renamed from a dialog", async () => {
    renderPlaylist();
    await screen.findByRole("heading", { name: "Road trip" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for Road trip" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));

    const name = await screen.findByLabelText("Name");
    expect(name).toHaveValue("Road trip");
    fireEvent.change(name, { target: { value: "Night drive" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("pl", { name: "Night drive" }));
  });

  it("is deleted after a confirmation that does not offer to delete tracks", async () => {
    renderPlaylist();
    await screen.findByRole("heading", { name: "Road trip" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for Road trip" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete playlist" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Delete Road trip?")).toBeInTheDocument();
    expect(within(dialog).queryByRole("switch")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("pl", { deleteTracks: false }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Deleted Road trip"));
  });

  it("has no switch for downloading and no source to check", async () => {
    renderPlaylist();
    await screen.findByRole("heading", { name: "Road trip" });
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check now" })).not.toBeInTheDocument();
  });

  it("is empty with a way to fill it", async () => {
    detail = base({ tracks: [], track_count: 0 });
    renderPlaylist();
    expect(await screen.findByText("This playlist has no tracks yet. Add some from the menu of any track.")).toBeInTheDocument();
  });

  it("duplicates itself under a name in the viewer's language and opens the copy", async () => {
    const { router } = renderPlaylist();
    await openHeaderMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Duplicate as mine" }));

    await waitFor(() => expect(mockDuplicate).toHaveBeenCalledWith("pl", "Road trip (copy)"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/playlists/copy"));
  });

  it("is not found when it is somebody else's", async () => {
    mockGet.mockRejectedValue(Object.assign(new Error("404"), { isAxiosError: true, response: { status: 404, data: {} } }));
    renderPlaylist();
    expect(await screen.findByText("This playlist does not exist, or it is not yours.")).toBeInTheDocument();
  });
});

describe("A followed playlist", () => {
  beforeEach(() => {
    detail = followed();
  });

  it("is read-only: its tracks have no controls and it says so", async () => {
    renderPlaylist();
    expect(await screen.findByRole("heading", { name: "Mix" })).toBeInTheDocument();
    expect(rowTitles()).toEqual(["Song 1", "Song 2", "Song 3"]);
    expect(screen.queryByRole("button", { name: /^Move /u })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove / })).not.toBeInTheDocument();
    expect(screen.getByText(/A followed playlist follows its source and cannot be edited/)).toBeInTheDocument();
    expect(await screen.findByText("Followed from Provider")).toBeInTheDocument();
    expect(await screen.findByText("3 tracks · Not copied to a music server yet")).toBeInTheDocument();
  });

  it("cannot be renamed from its menu", async () => {
    renderPlaylist();
    await screen.findByRole("heading", { name: "Mix" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for Mix" }));
    expect(screen.queryByRole("menuitem", { name: "Rename" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Unfollow…" })).toBeInTheDocument();
  });

  it("is duplicated as the viewer's own", async () => {
    renderPlaylist();
    await openHeaderMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Duplicate as mine" }));
    await waitFor(() => expect(mockDuplicate).toHaveBeenCalledWith("pl", "Mix (copy)"));
  });

  it("chooses whether the tracks its source gains are downloaded", async () => {
    renderPlaylist();
    const toggle = await screen.findByRole("switch", { name: "Download new tracks" });
    expect(toggle).toBeChecked();

    fireEvent.click(toggle);

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("pl", { auto_download: false }));
  });

  it("looks at its source now and says what changed", async () => {
    mockRefresh.mockResolvedValue({
      playlist: {}, changed: true, new_tracks: 2, removed_tracks: 1, error_code: null, download: { status: "queued", queued: 2 },
    });
    renderPlaylist();
    fireEvent.click(await screen.findByRole("button", { name: "Check now" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Updated: 2 new, 1 removed"));
    expect(toast.success).toHaveBeenCalledWith("Queued 2 tracks");
  });

  it("says when its source has nothing new", async () => {
    renderPlaylist();
    fireEvent.click(await screen.findByRole("button", { name: "Check now" }));
    await waitFor(() => expect(toast.info).toHaveBeenCalledWith("Already up to date"));
  });

  it("says a new track waits for approval", async () => {
    mockRefresh.mockResolvedValue({
      playlist: {}, changed: true, new_tracks: 4, removed_tracks: 0, error_code: null,
      download: { status: "pending_approval", needed_tracks: 4, reason: "kind_requires_approval", request_id: "r1", kind: "playlist" },
    });
    renderPlaylist();
    fireEvent.click(await screen.findByRole("button", { name: "Check now" }));

    await waitFor(() => expect(toast.info).toHaveBeenCalledTimes(1));
    expect(toast.info.mock.calls[0][0]).toMatch(/4/);
  });

  it("unfollows keeping the tracks unless the viewer asks otherwise", async () => {
    renderPlaylist();
    await screen.findByRole("heading", { name: "Mix" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for Mix" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Unfollow…" }));

    const dialog = await screen.findByRole("alertdialog");
    const choice = within(dialog).getByRole("switch", { name: "Also delete the tracks downloaded for this playlist" });
    expect(choice).not.toBeChecked();
    fireEvent.click(within(dialog).getByRole("button", { name: "Unfollow" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("pl", { deleteTracks: false }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Unfollowed Mix"));
  });

  it("unfollows and deletes the downloaded tracks when the viewer says so, and says it happens later", async () => {
    mockDelete.mockResolvedValue({ deleted: true, tracks_scheduled: 3 });
    renderPlaylist();
    await screen.findByRole("heading", { name: "Mix" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for Mix" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Unfollow…" }));

    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("switch", { name: "Also delete the tracks downloaded for this playlist" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Unfollow" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("pl", { deleteTracks: true }));
    await waitFor(() => expect(toast.info).toHaveBeenCalledWith("Deleting 3 downloaded tracks in the background"));
  });

  it("explains why its tracks are missing when the provider does not share them", async () => {
    detail = followed({
      tracks: [], track_count: 0, tracks_unavailable: { code: "access_restricted", message: "The provider does not allow this app to read this playlist's tracks." },
    });
    renderPlaylist();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The provider does not let this account read the playlist's tracks.");
    expect(screen.queryByText("This followed playlist has no tracks yet.")).not.toBeInTheDocument();
  });

  it("shows why the last look at its source failed, and that its tracks are kept", async () => {
    detail = followed({ refresh_error_code: "playlist_source_not_found" });
    renderPlaylist();
    expect(await screen.findByText("The source of this playlist is no longer available. Its tracks are kept.")).toBeInTheDocument();
    expect(rowTitles()).toHaveLength(3);
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { connectivityMock } from "@/test/connectivity-mock";
import { renderPage } from "@/test/render";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { FollowPlaylistButton } from "./FollowPlaylistButton";
import type { AuthConfig, FollowResult, UserPlaylist } from "@/types";

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

let mine: UserPlaylist[] = [];
let capabilities: Record<string, string[]> = {};
const mockFollow = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getUserPlaylists: async () => ({ items: mine, max_tracks: 500 }),
  followPlaylist: (...a: unknown[]) => mockFollow(...a),
  deleteUserPlaylist: (...a: unknown[]) => mockDelete(...a),
  getMyRequests: async () => ({ items: [], total: 0, page: 1, limit: 200, pages: 1 }),
  getPublicProviders: async () => ({
    providers: [{ id: "prov", label: "Provider" }], metadata_priority: ["prov"], metadata: ["prov"], metadata_capabilities: capabilities,
  }),
}));

const playlist = (over: Partial<UserPlaylist> = {}): UserPlaylist => ({
  id: "f1", name: "Mix", description: "", kind: "followed", source_provider: "prov", source_id: "s1", cover_url: null,
  track_count: 3, auto_download: true, tracks_unavailable: null, refresh_error_code: null, checked_at: null,
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", synced_to: [], ...over,
});

const result = (download: FollowResult["download"], created = true): FollowResult => ({ playlist: playlist(), created, download });

const renderButton = () =>
  renderPage(() => <FollowPlaylistButton provider="prov" id="s1" name="Mix" tracks={3} />, { path: "/p", otherPaths: ["/playlists/$id"] });

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
  Object.values(toast).forEach((f) => f.mockReset());
  mine = [];
  capabilities = {};
  mockFollow.mockReset().mockResolvedValue(result({ status: "queued", queued: 3, skipped: 0 }));
  mockDelete.mockReset().mockResolvedValue({ deleted: true, tracks_scheduled: 0 });
});

describe("FollowPlaylistButton", () => {
  it("follows the playlist and says how many tracks were queued", async () => {
    renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Follow" }));

    await waitFor(() => expect(mockFollow).toHaveBeenCalledWith("prov", "s1"));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Following Mix: queued 3 tracks"));
  });

  it("tells that the tracks do not become favorites", async () => {
    renderButton();
    expect((await screen.findByRole("button", { name: "Follow" })).getAttribute("title")).toContain("They do not become favorites.");
  });

  it("says the request waits for an administrator, when the policy says so", async () => {
    mockFollow.mockResolvedValue(
      result({ status: "pending_approval", request_id: "r", kind: "playlist", needed_tracks: 3, reason: "kind_requires_approval" }),
    );
    renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Follow" }));

    await waitFor(() => expect(toast.info).toHaveBeenCalledTimes(1));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("says nothing was left to download", async () => {
    mockFollow.mockResolvedValue(result({ status: "none", queued: 0, skipped: 3 }));
    renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Follow" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Following Mix: nothing to download"));
  });

  it("says the download could not be requested, but that the playlist is followed", async () => {
    mockFollow.mockResolvedValue(result({ status: "error" }));
    renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Follow" }));
    await waitFor(() => expect(toast.warning).toHaveBeenCalledTimes(1));
  });

  it("warns, from what the provider declares, that only some playlists give their tracks", async () => {
    capabilities = { prov: ["playlist_tracks_restricted"] };
    renderButton();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Follow" }).getAttribute("title")).toContain("only shares the tracks of the playlists you own or collaborate on"),
    );
  });

  it("does not warn about a provider that declares nothing", async () => {
    renderButton();
    const title = (await screen.findByRole("button", { name: "Follow" })).getAttribute("title");
    expect(title).not.toContain("only shares");
  });

  it("states the approval the policy would ask for", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    renderButton();
    expect((await screen.findByRole("button", { name: "Follow" })).getAttribute("title")).toContain("approval");
  });

  it("is not there for a viewer without playlists", () => {
    status = viewerStatus("guest");
    renderButton();
    expect(screen.queryByRole("button", { name: "Follow" })).not.toBeInTheDocument();
  });

  it("is a menu once followed: open it among the viewer's playlists, or unfollow", async () => {
    mine = [playlist()];
    const { router } = renderButton();

    fireEvent.click(await screen.findByRole("button", { name: "Following: Mix" }));
    expect(screen.queryByRole("button", { name: "Follow" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open in my playlists" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/playlists/f1"));
  });

  it("unfollows by default keeping the tracks, and deletes them when asked", async () => {
    mine = [playlist()];
    renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Following: Mix" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Unfollow…" }));

    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("switch"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Unfollow" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("f1", { deleteTracks: true }));
  });
});

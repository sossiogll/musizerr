import { beforeEach, describe, expect, it, vi } from "vitest";
import http, {
  addPlaylistTracks,
  createUserPlaylist,
  deleteUserPlaylist,
  duplicateUserPlaylist,
  followPlaylist,
  getUserPlaylist,
  getUserPlaylists,
  refreshUserPlaylist,
  removePlaylistTrack,
  reorderPlaylistTracks,
  updateUserPlaylist,
} from "./api-client";

// What each playlist call sends: the path with its ids as one encoded segment, the body, the options.

const answer = (data: unknown) => Promise.resolve({ data }) as never;

beforeEach(() => vi.restoreAllMocks());

describe("playlist API calls", () => {
  it("reads the list and one playlist", async () => {
    const get = vi.spyOn(http, "get").mockReturnValue(answer({}));
    await getUserPlaylists();
    await getUserPlaylist("a/b");
    expect(get.mock.calls).toEqual([["/playlists"], ["/playlists/a%2Fb"]]);
  });

  it("creates with a name and an empty description by default", async () => {
    const post = vi.spyOn(http, "post").mockReturnValue(answer({}));
    await createUserPlaylist("Road");
    await createUserPlaylist("Road", "For the car");
    expect(post.mock.calls).toEqual([
      ["/playlists", { name: "Road", description: "" }],
      ["/playlists", { name: "Road", description: "For the car" }],
    ]);
  });

  it("changes only what it is given", async () => {
    const patch = vi.spyOn(http, "patch").mockReturnValue(answer({}));
    await updateUserPlaylist("p", { name: "New" });
    await updateUserPlaylist("p", { auto_download: false });
    expect(patch.mock.calls).toEqual([["/playlists/p", { name: "New" }], ["/playlists/p", { auto_download: false }]]);
  });

  it("deletes keeping the tracks unless it is told to delete them", async () => {
    const del = vi.spyOn(http, "delete").mockReturnValue(answer({}));
    await deleteUserPlaylist("p");
    await deleteUserPlaylist("p", { deleteTracks: true });
    expect(del.mock.calls).toEqual([
      ["/playlists/p", { params: { delete_tracks: false } }],
      ["/playlists/p", { params: { delete_tracks: true } }],
    ]);
  });

  it("adds, removes and reorders tracks", async () => {
    const post = vi.spyOn(http, "post").mockReturnValue(answer({}));
    const del = vi.spyOn(http, "delete").mockReturnValue(answer({}));
    const put = vi.spyOn(http, "put").mockReturnValue(answer({}));
    await addPlaylistTracks("p", [{ track_id: "t" }, { provider: "x", provider_id: "1" }]);
    await removePlaylistTrack("p", "t/1");
    await reorderPlaylistTracks("p", ["b", "a"]);
    expect(post.mock.calls).toEqual([["/playlists/p/tracks", { items: [{ track_id: "t" }, { provider: "x", provider_id: "1" }] }]]);
    expect(del.mock.calls).toEqual([["/playlists/p/tracks/t%2F1"]]);
    expect(put.mock.calls).toEqual([["/playlists/p/tracks/order", { track_ids: ["b", "a"] }]]);
  });

  it("duplicates with or without a name, follows and looks at the source", async () => {
    const post = vi.spyOn(http, "post").mockReturnValue(answer({}));
    await duplicateUserPlaylist("p");
    await duplicateUserPlaylist("p", "Copy");
    await followPlaylist("prov", "s1");
    await refreshUserPlaylist("p");
    expect(post.mock.calls).toEqual([
      ["/playlists/p/duplicate", {}],
      ["/playlists/p/duplicate", { name: "Copy" }],
      ["/playlists/follow", { provider: "prov", id: "s1" }],
      ["/playlists/p/refresh"],
    ]);
  });
});

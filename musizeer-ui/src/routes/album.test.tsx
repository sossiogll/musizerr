import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { AlbumPage } from "./album";
import type { Album, AlbumEdition, LibraryCoverage } from "@/types";

let status = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());

let album: Album;
let editions: AlbumEdition[];
const mockDownloadAlbum = vi.fn();
const mockAddFavorites = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getAlbum: async () => album,
  getAlbumEditions: async () => ({ items: editions }),
  downloadAlbum: (...a: unknown[]) => mockDownloadAlbum(...a),
  addFavorites: (...a: unknown[]) => mockAddFavorites(...a),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const edition = (id: string, title: string, year: number): AlbumEdition => ({
  provider: "prov", provider_id: id, title, year, album_type: "album", track_count: 3, cover_url: null, external_ids: [],
});

function makeAlbum(library: LibraryCoverage, extra: Partial<Album> = {}): Album {
  return {
    kind: "album", internal_id: "a", title: "The Album", artist: "Artist", year: 2020, track_count: 3, cover_url: null,
    provider: "prov", provider_id: "al1", artist_provider_id: "ar1", artists: [{ name: "Artist", provider: "prov", provider_id: "ar1" }],
    external_ids: [{ type: "upc", value: "123" }], providers: ["prov"], album_type: "album", library,
    tracks: [
      makeTrack({ provider_id: "t1", title: "One", track_number: 1, download_status: "downloaded", in_library: true, library_id: "lib-1" }),
      makeTrack({ provider_id: "t2", title: "Two", track_number: 2, download_status: "failed" }),
      makeTrack({ provider_id: "t3", title: "Three", track_number: 3, download_status: null }),
    ],
    ...extra,
  };
}

const renderAlbum = () => renderPage(AlbumPage, { path: "/album/$provider/$id", url: "/album/prov/al1", queue: true });

beforeEach(() => {
  status = viewerStatus("user");
  mockAddFavorites.mockReset().mockResolvedValue({ added: ["lib-1"], skipped: [] });
  connectivityMock.reset();
  mockDownloadAlbum.mockReset().mockResolvedValue({ download_ids: ["d"], total_tracks: 2, skipped: 1 });
  album = makeAlbum({ total: 3, downloaded: 1, in_progress: 0, failed: 1 });
  editions = [];
});

describe("Album page", () => {
  it("shows the coverage and offers to download only what is missing", async () => {
    renderAlbum();
    expect(await screen.findByRole("heading", { level: 1, name: "The Album" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "1 of 3 in library · 1 to review" })).toBeInTheDocument();

    // A user's download: the name says the tracks arrive as favorites.
    const button = screen.getByRole("button", { name: "Download the 2 missing and add them to favorites" });
    expect(button).toHaveTextContent("Download the 2 missing");
    fireEvent.click(button);
    await waitFor(() => expect(mockDownloadAlbum).toHaveBeenCalledWith("prov", "al1", { onlyMissing: true, addToFavorites: true }));
  });

  it("nothing in the library yet: download it all", async () => {
    album = makeAlbum({ total: 3, downloaded: 0, in_progress: 0, failed: 0 });
    renderAlbum();
    fireEvent.click(await screen.findByRole("button", { name: "Download all and add them to favorites" }));
    await waitFor(() => expect(mockDownloadAlbum).toHaveBeenCalledWith("prov", "al1", { onlyMissing: true, addToFavorites: true }));
  });

  it("an admin: the split button downloads the missing ones plain, the menu adds them to favorites", async () => {
    status = viewerStatus("admin");
    renderAlbum();
    fireEvent.click(await screen.findByRole("button", { name: "Download the 2 missing" }));
    await waitFor(() => expect(mockDownloadAlbum).toHaveBeenCalledWith("prov", "al1", { onlyMissing: true, addToFavorites: false }));

    fireEvent.click(screen.getByRole("button", { name: "Download options for The Album" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Download the 2 missing and add them to favorites" }));
    await waitFor(() => expect(mockDownloadAlbum).toHaveBeenLastCalledWith("prov", "al1", { onlyMissing: true, addToFavorites: true }));
  });

  it("the row badges and the coverage agree", async () => {
    renderAlbum();
    const list = await screen.findByRole("list", { name: "Tracks" });
    expect(within(list).getAllByTitle("In library")).toHaveLength(album.library!.downloaded);
    expect(screen.getByRole("img", { name: /^1 of 3 in library/ })).toBeInTheDocument();
  });

  it("adds the tracks in the library to favorites, with the server's count", async () => {
    album = makeAlbum({ total: 3, downloaded: 1, in_progress: 0, failed: 1 }, { favorites: { favorite: 0, addable: 1 } });
    renderAlbum();
    fireEvent.click(await screen.findByRole("button", { name: "Add the track in library to favorites" }));
    // Only the tracks in the library, never the others.
    await waitFor(() => expect(mockAddFavorites).toHaveBeenCalledWith(["lib-1"]));
  });

  it("all the tracks in library already favorites: says so, no button", async () => {
    album = makeAlbum({ total: 3, downloaded: 1, in_progress: 0, failed: 1 }, { favorites: { favorite: 1, addable: 0 } });
    renderAlbum();
    expect(await screen.findByText("The track in library is in your favorites")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /in library to favorites/ })).not.toBeInTheDocument();
  });

  it("nothing in the library: no favorites action", async () => {
    album = makeAlbum({ total: 3, downloaded: 0, in_progress: 0, failed: 0 }, { favorites: { favorite: 0, addable: 0 } });
    renderAlbum();
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: /in library to favorites/ })).not.toBeInTheDocument();
  });

  it("a guest gets neither the download nor the favorites action", async () => {
    status = viewerStatus("guest");
    album = makeAlbum({ total: 3, downloaded: 1, in_progress: 0, failed: 1 }, { favorites: { favorite: 0, addable: 1 } });
    renderAlbum();
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: /Download|favorites/ })).not.toBeInTheDocument();
  });

  it("everything in the library: no download, says so", async () => {
    album = makeAlbum({ total: 3, downloaded: 3, in_progress: 0, failed: 0 });
    renderAlbum();
    expect(await screen.findByText("All in library")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Download (all|the)/ })).not.toBeInTheDocument();
  });

  it("filters the tracks by library state", async () => {
    renderAlbum();
    const list = await screen.findByRole("list", { name: "Tracks" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);

    fireEvent.click(screen.getByRole("button", { name: "All states" }));
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "Missing" }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Tracks" })).getAllByRole("listitem")).toHaveLength(1));
    expect(screen.getByText("Three")).toBeInTheDocument();
  });

  it("an album on two discs gets a heading per disc", async () => {
    album = makeAlbum(
      { total: 2, downloaded: 0, in_progress: 0, failed: 0 },
      { tracks: [makeTrack({ provider_id: "x", title: "A", disc_number: 1 }), makeTrack({ provider_id: "y", title: "B", disc_number: 2 })] },
    );
    renderAlbum();
    expect(await screen.findByRole("heading", { name: "Disc 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Disc 2" })).toBeInTheDocument();
  });

  it("switches to another edition of the release", async () => {
    editions = [edition("al1", "The Album", 2020), edition("al2", "The Album (Deluxe)", 2021)];
    const { router } = renderAlbum();
    const picker = await screen.findByRole("combobox", { name: /Edition \(2\)/ });
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "The Album · 2020 · 3 tracks",
      "The Album (Deluxe) · 2021 · 3 tracks",
    ]);
    fireEvent.change(picker, { target: { value: "prov\u0000al2" } });
    await waitFor(() => expect(router.state.location.pathname).toBe("/album/prov/al2"));
  });

  it("no picker for an album with one edition", async () => {
    editions = [edition("al1", "The Album", 2020)];
    renderAlbum();
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { ArtistPage } from "./artist";
import type { Album, AlbumType, TopTracksResponse } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("user"), user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const release = (id: string, title: string, type: AlbumType | null, extra: Partial<Album> = {}): Album =>
  ({
    kind: "album", internal_id: id, title, artist: "Band", year: 2020, track_count: 10, cover_url: null,
    provider: "prov", provider_id: id, artist_provider_id: "ar1", artists: [], external_ids: [], tracks: [],
    providers: ["prov"], album_type: type, ...extra,
  }) as Album;

let top: TopTracksResponse;
// What the server says about the artist's provider: it can give the whole discography.
let downloadable: boolean | undefined;
// The provider's listing reached the bound the server reads.
let mayBeLonger = false;
const mockDownloadArtist = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getArtist: async () => ({
    kind: "artist", internal_id: "ar", name: "Band", provider: "prov", provider_id: "ar1", cover_url: null, external_ids: [],
    providers: ["prov"], discography_downloadable: downloadable,
  }),
  getArtistAlbums: async () => ({
    items: [
      release("a1", "First LP", "album", {
        editions: [{ provider: "prov", provider_id: "a1", title: "First LP", year: 2020, album_type: "album", track_count: 10, cover_url: null, external_ids: [] },
                   { provider: "prov", provider_id: "a1d", title: "First LP (Deluxe)", year: 2021, album_type: "album", track_count: 14, cover_url: null, external_ids: [] }],
        library: { total: 10, downloaded: 4, in_progress: 0, failed: 1 },
      }),
      release("s1", "Hit", "single"),
      release("x1", "Mystery", null),
    ],
    total: 3, page: 1, limit: 0, pages: 1,
    library: { total: 12, downloaded: 4, in_progress: 0, failed: 1 },
    may_be_longer: mayBeLonger,
    provider_errors: [{ provider: "prov", error_code: "metadata_provider_unreachable", error_message: "down" }],
  }),
  getArtistTopTracks: async () => top,
  downloadArtist: (...a: unknown[]) => mockDownloadArtist(...a),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const renderArtist = () => renderPage(ArtistPage, { path: "/artist/$provider/$id", url: "/artist/prov/ar1", queue: true });

beforeEach(() => {
  connectivityMock.reset();
  mockDownloadArtist.mockReset().mockResolvedValue({ download_ids: [], total_tracks: 0 });
  downloadable = true;
  mayBeLonger = false;
  top = { items: [makeTrack({ provider_id: "t1", title: "Popular One" })], error_code: null, error_message: null, provider_errors: [] };
});

describe("Artist page", () => {
  it("header with the release count and the artist's library coverage", async () => {
    renderArtist();
    expect(await screen.findByRole("heading", { level: 1, name: "Band" })).toBeInTheDocument();
    expect(await screen.findByText("3 releases")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "4 of 12 in library · 1 to review" })).toBeInTheDocument();
    // The discography's missing provider is named in the discography, not in the header.
    const notice = screen.getByText(/Results may be incomplete/);
    expect(notice.closest("section")).toHaveAttribute("aria-labelledby", "discography");
  });

  it("the discography is split by release type, each tile with its editions and coverage", async () => {
    renderArtist();
    const albums = await screen.findByRole("heading", { level: 3, name: /Albums/ });
    expect(screen.getByRole("heading", { level: 3, name: /Singles/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: /Other releases/ })).toBeInTheDocument();
    const grid = albums.nextElementSibling as HTMLElement;
    expect(within(grid).getByText("2 editions")).toBeInTheDocument();
    expect(within(grid).getByText(/4\/10 in library/)).toBeInTheDocument();
  });

  it("says when the discography may be longer than what the provider's listing gave", async () => {
    mayBeLonger = true;
    renderArtist();

    const heading = await screen.findByRole("heading", { level: 2, name: "Discography" });
    expect(await within(heading.closest("section") as HTMLElement).findByText(/this artist may have more/)).toBeInTheDocument();
  });

  it("says nothing of the sort when the listing is whole", async () => {
    renderArtist();

    await screen.findByRole("heading", { level: 3, name: /Albums/ });
    expect(screen.queryByText(/this artist may have more/)).not.toBeInTheDocument();
  });

  it("popular tracks as rows", async () => {
    renderArtist();
    expect(await screen.findByText("Popular One")).toBeInTheDocument();
  });

  it("top tracks unavailable: explains instead of an empty list", async () => {
    top = { items: [], error_code: "metadata_provider_unreachable", error_message: "raw", provider_errors: [] };
    renderArtist();
    const heading = await screen.findByRole("heading", { name: "Popular" });
    expect(heading.closest("section")).not.toHaveTextContent("raw");
  });

  it("downloading everything asks first", async () => {
    renderArtist();
    fireEvent.click(await screen.findByRole("button", { name: "Download everything by Band and add it to favorites" }));
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    expect(mockDownloadArtist).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Download everything" }));
    await waitFor(() => expect(mockDownloadArtist).toHaveBeenCalledWith("prov", "ar1", { onlyMissing: false, addToFavorites: true }));
  });

  // The server says whether the provider can give the whole discography: only then is there a button.
  it.each([
    ["the provider cannot give a discography", false],
    ["the server does not say (an old copy of the page)", undefined],
  ])("offers no download of the whole discography when %s", async (_why, value) => {
    downloadable = value;
    renderArtist();

    expect(await screen.findByRole("heading", { level: 1, name: "Band" })).toBeInTheDocument();
    await screen.findByText("Popular One");
    expect(screen.queryByRole("button", { name: /Download everything/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request/ })).not.toBeInTheDocument();
  });
});

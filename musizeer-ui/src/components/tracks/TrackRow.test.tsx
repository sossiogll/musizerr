import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import type { ReactNode } from "react";
import { I18nProvider, LOCALE_STORAGE_KEY } from "@/i18n";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { QueueProvider } from "@/contexts/QueueContext";
import { TrackList } from "./TrackList";
import { TrackRow } from "./TrackRow";
import type { AuthConfig, Track } from "@/types";

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const mockPlay = vi.fn();
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => ({ track: null, playing: false, play: mockPlay }),
}));

vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());

const mockDownloadTrack = vi.fn();
const mockAddFavorite = vi.fn();
const mockRemoveFavorite = vi.fn();
const mockGetFavorite = vi.fn();
let progressSource: { onmessage: ((e: MessageEvent) => void) | null } & Record<string, unknown> = { onmessage: null };
vi.mock("@/lib/api-client", () => ({
  downloadTrack: (...a: unknown[]) => mockDownloadTrack(...a),
  addFavorite: (...a: unknown[]) => mockAddFavorite(...a),
  removeFavorite: (...a: unknown[]) => mockRemoveFavorite(...a),
  getFavorite: (...a: unknown[]) => mockGetFavorite(...a),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => {
    progressSource = { close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 };
    return progressSource;
  },
  refreshAccessToken: vi.fn(),
}));

function renderInApp(ui: ReactNode) {
  const root = createRootRoute({ component: () => <>{ui}</> });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <QueueProvider>
          <RouterProvider router={router} />
        </QueueProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const row = (track: Track) => renderInApp(<TrackRow track={track} />);

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
  mockDownloadTrack.mockReset();
  mockAddFavorite.mockReset();
  mockRemoveFavorite.mockReset();
  mockGetFavorite.mockReset();
  mockPlay.mockReset();
});

const USER_DOWNLOAD = "Download Song A and add it to favorites";
const PENDING_HEART = "Will be added to favorites when it arrives";

/** POST /download/track answering "queued": its track is annotated like every list. */
const queued = (favoritePending: boolean) => ({
  status: "queued",
  download_id: "d1",
  library_id: "lib-1",
  track: makeTrack({ download_status: "pending", library_id: "lib-1", favorite_pending: favoritePending }),
});

describe("TrackRow status slot — five states, labels from the backend", () => {
  it("available: a user's Download says it adds to favorites, and does", async () => {
    mockDownloadTrack.mockResolvedValue(queued(true));
    row(makeTrack());

    const button = await screen.findByRole("button", { name: USER_DOWNLOAD });
    expect(button).toHaveAttribute("title", USER_DOWNLOAD);
    expect(button).toHaveTextContent("Download");
    fireEvent.click(button);

    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalledWith("prov", "p1", [], { addToFavorites: true }));
    // Optimistically queued: the button is replaced by the declared "pending" state.
    expect(await screen.findByTitle("Queued")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: USER_DOWNLOAD })).not.toBeInTheDocument();
  });

  it("a user has no plain download and no menu", async () => {
    row(makeTrack());
    await screen.findByRole("button", { name: USER_DOWNLOAD });
    expect(screen.queryByRole("button", { name: "Download Song A" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download options/ })).not.toBeInTheDocument();
  });

  it("an admin's slot has the plain Download, and Download and add to favorites is in the row's menu", async () => {
    status = viewerStatus("admin");
    mockDownloadTrack.mockResolvedValue(queued(false));
    row(makeTrack());

    const plain = await screen.findByRole("button", { name: "Download Song A" });
    fireEvent.click(plain);
    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalledWith("prov", "p1", [], { addToFavorites: false }));
  });

  it("the admin's menu downloads and adds to favorites", async () => {
    status = viewerStatus("admin");
    mockDownloadTrack.mockResolvedValue(queued(true));
    row(makeTrack());

    fireEvent.click(await screen.findByRole("button", { name: "More actions for Song A" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Download and add to favorites" }));
    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalledWith("prov", "p1", [], { addToFavorites: true }));
  });

  // Its ARIA and keyboard, with the real menu: components/menu/ActionMenu.test.tsx.
  it("the row has ONE menu button, a 44px target, and the slot has no menu of its own", async () => {
    status = viewerStatus("admin");
    row(makeTrack());

    const triggers = await screen.findAllByRole("button", { name: "More actions for Song A" });
    expect(triggers).toHaveLength(1);
    expect(triggers[0].className.split(/\s+/)).toEqual(expect.arrayContaining(["size-11", "rounded-md"]));
    expect(screen.queryByRole("button", { name: /Download options/ })).not.toBeInTheDocument();
  });

  it.each([
    ["pending", "Queued"],
    ["retrying", "Retrying"],
    ["downloaded", "In library"],
    ["conflict", "Conflict"],
  ] as const)("%s shows its declared label and no action", async (downloadStatus, label) => {
    row(makeTrack({ download_status: downloadStatus }));
    expect(await screen.findByTitle(label)).toHaveTextContent(label);
    expect(screen.queryByRole("button", { name: /Download|Retry/ })).not.toBeInTheDocument();
  });

  it("downloading shows progress as a live status", async () => {
    row(makeTrack({ download_status: "downloading" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Downloading");
  });

  it("failed: one Retry that downloads again", async () => {
    mockDownloadTrack.mockResolvedValue({ download_id: "d2", track: makeTrack() });
    row(makeTrack({ download_status: "failed" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry Song A" }));
    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalled());
  });

  it("a stream URL says nothing about the library: only the server's state does", async () => {
    row(makeTrack({ stream_url: "https://cdn.example/full.mp3", playback: { type: "full", source: "provider" } }));
    expect(await screen.findByRole("button", { name: USER_DOWNLOAD })).toBeInTheDocument();
    expect(screen.queryByTitle("In library")).not.toBeInTheDocument();
  });

  it("labels follow the viewer's language via label_key", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    row(makeTrack({ download_status: "pending" }));
    expect(await screen.findByTitle("In coda")).toBeInTheDocument();
  });

  it("a guest gets no download action", async () => {
    status = viewerStatus("guest");
    row(makeTrack());
    await screen.findByText("Song A");
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
  });

  it("offline: the action is disabled with the reason", async () => {
    connectivityMock.set({ online: false });
    row(makeTrack());
    const button = await screen.findByRole("button", { name: USER_DOWNLOAD });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Needs the server: not available while it is unreachable");
  });

  it("the slot has a fixed width and is icon-only below M", async () => {
    row(makeTrack({ download_status: "pending" }));
    const slot = await screen.findByTitle("Queued");
    expect(slot).toHaveClass("w-11", "md:w-32");
    expect(screen.getByText("Queued")).toHaveClass("max-md:sr-only");
  });
});

describe("TrackRow content", () => {
  it("shows 'artist · album', or 'Single' when the album repeats the title", async () => {
    const { unmount } = row(makeTrack());
    expect(await screen.findByText(/Artist A/)).toHaveTextContent("Artist A · Album A");
    unmount();
    row(makeTrack({ title: "Solo", album: "Solo" }));
    expect(await screen.findByText(/Artist A/)).toHaveTextContent("Artist A · Single");
  });

  it("no provider chips: the sources are in the title's tooltip", async () => {
    row(makeTrack());
    const title = await screen.findByRole("link", { name: "Song A" });
    await waitFor(() => expect(title).toHaveAttribute("title", "Sources: Provider"));
    expect(screen.queryByText("Provider")).not.toBeInTheDocument();
  });

  it("the cover is the play button, and plays the list as a queue", async () => {
    const tracks = [makeTrack(), makeTrack({ provider_id: "p2", title: "Song B" })];
    renderInApp(<TrackList tracks={tracks} />);
    fireEvent.click(await screen.findByRole("button", { name: "Play a 30 second preview of Song B" }));
    const [played, queue] = mockPlay.mock.calls[0];
    expect(played.title).toBe("Song B");
    expect(queue.map((t: { title: string }) => t.title)).toEqual(["Song A", "Song B"]);
  });

  it("playback full: a normal play button, no preview label", async () => {
    row(makeTrack({ stream_url: "/api/stream/x", playback: { type: "full", source: "library" } }));
    expect(await screen.findByRole("button", { name: "Play Song A" })).toBeEnabled();
    expect(screen.queryByText("Preview 30s")).not.toBeInTheDocument();
  });

  it("playback preview: a discreet 'Preview 30s' label", async () => {
    row(makeTrack());
    expect(await screen.findByRole("button", { name: "Play a 30 second preview of Song A" })).toBeEnabled();
    expect(screen.getByText("Preview 30s")).toBeInTheDocument();
  });

  it("playback none: the play button is disabled", async () => {
    row(makeTrack({ preview_url: null, playback: { type: "none", source: null } }));
    expect(await screen.findByRole("button", { name: "Song A cannot be played" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Play/ })).not.toBeInTheDocument();
  });

  it("a live progress event updates the row of that track", async () => {
    renderInApp(
      <>
        <TrackRow track={makeTrack({ internal_id: "db-1" })} />
        <TrackRow track={makeTrack({ internal_id: "db-2", provider_id: "p2", title: "Other" })} />
      </>,
    );
    await screen.findByText("Other");

    act(() => {
      progressSource.onmessage?.({ data: JSON.stringify({ download_id: "d9", track_id: "db-1", status: "downloading" }) } as MessageEvent);
    });

    expect(await screen.findByRole("status")).toHaveTextContent("Downloading");
    expect(screen.getByRole("button", { name: "Download Other and add it to favorites" })).toBeInTheDocument();
  });
});

describe("TrackRow heart — only for tracks in the library", () => {
  const inLibrary = (overrides: Partial<Track> = {}) =>
    makeTrack({ download_status: "downloaded", in_library: true, library_id: "lib-1", ...overrides });

  it("is hidden for a track not in the library (no disabled heart)", async () => {
    row(makeTrack());
    await screen.findByText("Song A");
    expect(screen.queryByRole("button", { name: /favorites/i, pressed: false })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add Song A to favorites" })).not.toBeInTheDocument();
  });

  it("is hidden for a guest", async () => {
    status = viewerStatus("guest");
    row(inLibrary());
    await screen.findByText("Song A");
    expect(screen.queryByRole("button", { name: /to favorites|from favorites/ })).not.toBeInTheDocument();
  });

  it("empty when not a favorite, filled when it is (aria-pressed)", async () => {
    const { unmount } = row(inLibrary());
    expect(await screen.findByRole("button", { name: "Add Song A to favorites" })).toHaveAttribute("aria-pressed", "false");
    unmount();
    row(inLibrary({ favorite: true }));
    expect(await screen.findByRole("button", { name: "Remove Song A from favorites" })).toHaveAttribute("aria-pressed", "true");
  });

  it("likes and unlikes at once, with the server's answer", async () => {
    mockAddFavorite.mockResolvedValue({ track_id: "lib-1", favorite: true, changed: true });
    mockRemoveFavorite.mockResolvedValue({ track_id: "lib-1", favorite: false, changed: true });
    row(inLibrary());

    fireEvent.click(await screen.findByRole("button", { name: "Add Song A to favorites" }));
    await waitFor(() => expect(mockAddFavorite).toHaveBeenCalledWith("lib-1"));
    const liked = await screen.findByRole("button", { name: "Remove Song A from favorites" });
    expect(liked).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(liked);
    await waitFor(() => expect(mockRemoveFavorite).toHaveBeenCalledWith("lib-1"));
    expect(await screen.findByRole("button", { name: "Add Song A to favorites" })).toHaveAttribute("aria-pressed", "false");
  });

  it("while a user's download is pending: a small heart with a clock, not a button", async () => {
    row(makeTrack({ download_status: "downloading", library_id: "lib-1", favorite_pending: true }));
    const pending = await screen.findByRole("img", { name: "Will be added to favorites when it arrives" });
    expect(pending).toHaveAttribute("title", "Will be added to favorites when it arrives");
    expect(screen.queryByRole("button", { name: /to favorites/ })).not.toBeInTheDocument();
  });

  it("the pending heart appears from the download response, before any refetch", async () => {
    mockDownloadTrack.mockResolvedValue(queued(true));
    row(makeTrack());
    expect(screen.queryByRole("img", { name: PENDING_HEART })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: USER_DOWNLOAD }));

    expect(await screen.findByRole("img", { name: PENDING_HEART })).toBeInTheDocument();
    expect(screen.getByTitle("Queued")).toBeInTheDocument();
    // Nothing read again: the state is the response's.
    expect(mockDownloadTrack).toHaveBeenCalledTimes(1);
    expect(mockGetFavorite).not.toHaveBeenCalled();
  });

  it.each([
    ["plain", "Download Song A", false],
    ["with favorite", "More actions for Song A", true],
  ] as const)("an admin's %s download shows the heart the response says", async (_, button, pending) => {
    status = viewerStatus("admin");
    mockDownloadTrack.mockResolvedValue(queued(pending));
    row(makeTrack());

    fireEvent.click(await screen.findByRole("button", { name: button }));
    if (pending) fireEvent.click(screen.getByRole("menuitem", { name: "Download and add to favorites" }));

    expect(await screen.findByTitle("Queued")).toBeInTheDocument();
    if (pending) expect(screen.getByRole("img", { name: PENDING_HEART })).toBeInTheDocument();
    else expect(screen.queryByRole("img", { name: PENDING_HEART })).not.toBeInTheDocument();
  });

  it("the server's answer wins over the option sent", async () => {
    // An admin's plain download of a track they had already asked to favorite.
    status = viewerStatus("admin");
    mockDownloadTrack.mockResolvedValue(queued(true));
    row(makeTrack());

    fireEvent.click(await screen.findByRole("button", { name: "Download Song A" }));

    expect(await screen.findByRole("img", { name: PENDING_HEART })).toBeInTheDocument();
  });

  it("already in the library: the heart shows the favorite the response carries, without asking", async () => {
    mockDownloadTrack.mockResolvedValue({
      status: "already_in_library",
      download_id: null,
      library_id: "lib-1",
      track: makeTrack({ download_status: "downloaded", in_library: true, library_id: "lib-1", favorite: true }),
    });
    row(makeTrack());

    fireEvent.click(await screen.findByRole("button", { name: USER_DOWNLOAD }));

    expect(await screen.findByRole("button", { name: "Remove Song A from favorites" })).toHaveAttribute("aria-pressed", "true");
    expect(mockGetFavorite).not.toHaveBeenCalled();
  });

  it("the pending heart is gone when the download fails", async () => {
    row(makeTrack({ download_status: "failed", library_id: "lib-1", favorite_pending: false }));
    await screen.findByRole("button", { name: "Retry Song A" });
    expect(screen.queryByRole("img", { name: "Will be added to favorites when it arrives" })).not.toBeInTheDocument();
  });

  it("a live failure removes the pending heart", async () => {
    renderInApp(<TrackRow track={makeTrack({ download_status: "downloading", library_id: "db-1", favorite_pending: true })} />);
    await screen.findByRole("img", { name: "Will be added to favorites when it arrives" });
    act(() => {
      progressSource.onmessage?.({ data: JSON.stringify({ download_id: "d9", track_id: "db-1", status: "failed" }) } as MessageEvent);
    });
    await screen.findByRole("button", { name: "Retry Song A" });
    expect(screen.queryByRole("img", { name: "Will be added to favorites when it arrives" })).not.toBeInTheDocument();
  });

  it("on arrival the row asks the server, which added the favorite", async () => {
    mockGetFavorite.mockResolvedValue({ track_id: "db-1", favorite: true, in_library: true });
    renderInApp(<TrackRow track={makeTrack({ download_status: "downloading", library_id: "db-1", favorite_pending: true })} />);
    await screen.findByRole("img", { name: "Will be added to favorites when it arrives" });
    act(() => {
      progressSource.onmessage?.({ data: JSON.stringify({ download_id: "d9", track_id: "db-1", status: "downloaded" }) } as MessageEvent);
    });
    expect(await screen.findByRole("button", { name: "Remove Song A from favorites" })).toHaveAttribute("aria-pressed", "true");
    expect(mockGetFavorite).toHaveBeenCalledWith("db-1");
  });

  it("the row badge says In library exactly when the server says so", async () => {
    row(inLibrary());
    expect(await screen.findByTitle("In library")).toBeInTheDocument();
  });
});

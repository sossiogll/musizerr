import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { waitingLikes } from "@/test/download-requests";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { FavoritesPage, type FavoritesView } from "./favorites";
import type { AuthConfig, DownloadRequest, FavoriteTrack, LibraryJobRun, LibraryJobs, LikedElsewhereTrack } from "@/types";

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: { id: "u1" } }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const favorite = (i: number): FavoriteTrack => ({
  ...makeTrack({
    provider_id: `f${i}`,
    internal_id: `f${i}`,
    title: `Fav ${i}`,
    download_status: "downloaded",
    in_library: true,
    library_id: `lib-${i}`,
    favorite: true,
    playback: { type: "full", source: "library" },
    stream_url: `/api/stream/lib-${i}`,
  }),
  favorited_at: "2026-09-01T00:00:00Z",
});

const elsewhere = (i: number): LikedElsewhereTrack => ({
  ...makeTrack({ provider_id: `e${i}`, internal_id: `e${i}`, title: `Liked ${i}`, providers: [] }),
  saved_at: "2026-09-01T00:00:00Z",
  provider_saved_at: { prov: { added_at: "2026-09-01T00:00:00Z" } },
});

const mockGetFavorites = vi.fn();
const mockGetElsewhere = vi.fn();
const mockDelete = vi.fn();
const mockDownloadSync = vi.fn();
const mockDownloadTrack = vi.fn();
const mockRemoveFavorite = vi.fn();
const mockCreateSource = vi.fn();
const mockSync = vi.fn();
let jobs: LibraryJobs;
let waiting: DownloadRequest[] = [];
let stream: { onmessage: ((e: MessageEvent) => void) | null };
vi.mock("@/lib/api-client", () => ({
  getFavorites: () => mockGetFavorites(),
  getLikedElsewhere: () => mockGetElsewhere(),
  getLibraryJobs: async () => jobs,
  getMyRequests: async () => ({ items: waiting, total: waiting.length, page: 1, limit: 200, pages: 1 }),
  triggerLibrarySync: () => mockSync(),
  triggerLibraryDownloadSync: (range: string) => mockDownloadSync(range),
  deleteSavedTracks: () => mockDelete(),
  downloadTrack: (...a: unknown[]) => mockDownloadTrack(...a),
  removeFavorite: (...a: unknown[]) => mockRemoveFavorite(...a),
  addFavorite: vi.fn(),
  getFavorite: vi.fn(),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => mockCreateSource(),
  refreshAccessToken: vi.fn(),
}));

function run(kind: LibraryJobRun["kind"], status: LibraryJobRun["status"], extra: Partial<LibraryJobRun> = {}): LibraryJobRun {
  return { kind, status, id: `${kind}-1`, processed: 0, total: null, providers: [], result: {}, ...extra };
}

const send = (data: unknown) =>
  act(() => {
    stream.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  });

function Page({ initial = "favorites" }: { initial?: FavoritesView }) {
  const [view, setView] = useState<FavoritesView>(initial);
  return <FavoritesPage view={view} onViewChange={setView} />;
}

const renderFavorites = (initial?: FavoritesView) => renderPage(() => <Page initial={initial} />, { path: "/favorites", queue: true });

const openMenu = (name: string) => fireEvent.click(screen.getByRole("button", { name }));

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
  waiting = [];
  mockGetFavorites.mockReset().mockResolvedValue({ items: [favorite(1), favorite(2)], total: 2, page: 1, limit: 50, pages: 1 });
  mockGetElsewhere.mockReset().mockResolvedValue({ items: [elsewhere(1)], total: 1, page: 1, limit: 50, pages: 1 });
  mockDelete.mockReset().mockResolvedValue({ ok: true });
  mockDownloadSync.mockReset().mockImplementation(async (range: string) => ({ ...run("download_sync", "queued"), params: { time_range: range }, created: true }));
  mockSync.mockReset().mockResolvedValue({ ...run("library_sync", "queued", { id: "library_sync-2" }), created: true });
  // A user's download, as the server answers it: the track annotated, its favorite pending.
  mockDownloadTrack.mockReset().mockResolvedValue({
    status: "queued",
    download_id: "d1",
    library_id: "lib-e1",
    track: { ...elsewhere(1), download_status: "pending", library_id: "lib-e1", favorite_pending: true },
  });
  mockRemoveFavorite.mockReset().mockResolvedValue({ track_id: "lib-1", favorite: false, changed: true });
  jobs = {
    library_sync: { ...run("library_sync", "completed"), completed_at: "2026-09-01T10:00:00Z" },
    download_sync: { kind: "download_sync", status: "never_run" },
  };
  Object.values(toast).forEach((fn) => fn.mockReset());
  mockCreateSource.mockReset().mockImplementation(() => {
    stream = { onmessage: null };
    return Object.assign(stream, { close: () => {}, onopen: null, onerror: null, readyState: 1 });
  });
});

describe("Favorites page", () => {
  it("is called Favorites and lists the favorites, each with its filled heart", async () => {
    renderFavorites();
    expect(await screen.findByRole("heading", { level: 1, name: "Favorites" })).toBeInTheDocument();
    expect(await screen.findByText("Favorites: 2")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Favorites" });
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(within(list).getByRole("button", { name: "Remove Fav 1 from favorites" })).toHaveAttribute("aria-pressed", "true");
    // The file state of a favorite: in library.
    expect(within(list).getAllByTitle("In library")).toHaveLength(2);
  });

  it("the heart removes a favorite (the file stays: it is still in library)", async () => {
    renderFavorites();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Fav 1 from favorites" }));
    await waitFor(() => expect(mockRemoveFavorite).toHaveBeenCalledWith("lib-1"));
    expect(await screen.findByRole("button", { name: "Add Fav 1 to favorites" })).toHaveAttribute("aria-pressed", "false");
  });

  it("a list that could not be loaded is not shown as empty", async () => {
    mockGetFavorites.mockRejectedValue(new Error("Network Error"));
    renderFavorites();
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/No favorites yet/)).not.toBeInTheDocument();
  });

  it("no favorites: says how to get some", async () => {
    mockGetFavorites.mockResolvedValue({ items: [], total: 0, page: 1, limit: 50, pages: 0 });
    renderFavorites();
    expect(await screen.findByText(/No favorites yet/)).toBeInTheDocument();
  });

  it("'Liked elsewhere, not in library': the likes from the other services, no heart, with a download", async () => {
    renderFavorites();
    await screen.findByText("Fav 1");
    fireEvent.click(screen.getByRole("button", { name: "Liked elsewhere, not in library" }));

    const list = await screen.findByRole("list", { name: "Liked elsewhere, not in library" });
    expect(within(list).getByText("Liked 1")).toBeInTheDocument();
    expect(within(list).queryByRole("button", { name: /^(Add|Remove) .* favorites$/ })).not.toBeInTheDocument();

    fireEvent.click(within(list).getByRole("button", { name: "Download Liked 1 and add it to favorites" }));
    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalledWith("prov", "e1", [], { addToFavorites: true }));
    expect(await within(list).findByRole("img", { name: "Will be added to favorites when it arrives" })).toBeInTheDocument();
  });

  it("nothing liked elsewhere is missing: says so", async () => {
    mockGetElsewhere.mockResolvedValue({ items: [], total: 0, page: 1, limit: 50, pages: 0 });
    renderFavorites("elsewhere");
    expect(await screen.findByText("Nothing liked elsewhere is missing from the library.")).toBeInTheDocument();
  });

  it("deleting the imported likes asks first, and only then calls the server", async () => {
    renderFavorites();
    await screen.findByText("Fav 1");
    openMenu("More actions");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete imported likes" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Your favorites and the library files stay");
    expect(mockDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledTimes(1));
  });

  it("a failed sync says why in words, not the raw message", async () => {
    jobs.library_sync = run("library_sync", "failed", { error_code: "metadata_provider_unreachable", error_message: "raw text" });
    renderFavorites();
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("raw text");
    expect(alert).toHaveTextContent("The catalog provider could not be reached.");
  });

  it("'Sync likes' is disabled with a spinner and the reason while the run waits for the worker", async () => {
    jobs.library_sync = run("library_sync", "queued");
    renderFavorites();
    const button = await screen.findByRole("button", { name: "Sync likes" });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAttribute("title", "Waiting for the worker");
    expect(button).toHaveAccessibleDescription("Waiting for the worker");
    expect(button).toHaveAttribute("aria-busy", "true");
    // The progress area: waiting, no known total.
    expect(screen.getByRole("progressbar", { name: "Syncing likes progress" })).not.toHaveAttribute("aria-valuenow");
  });

  it("while it runs: disabled as 'already in progress', phase in words with the provider's label", async () => {
    jobs.library_sync = run("library_sync", "running", {
      phase: "fetching", provider: "prov", processed: 50,
      providers: [{ provider: "prov", status: "running", processed: 50, total: null, count: 0 }],
    });
    renderFavorites();
    const button = await screen.findByRole("button", { name: "Sync likes" });
    await waitFor(() => expect(button).toHaveAccessibleDescription("Sync already in progress"));
    expect(button).toBeDisabled();
    expect(await screen.findByText("Reading likes from Provider: 50 so far")).toBeInTheDocument();
    // Indeterminate while the total is not known.
    const bar = screen.getByRole("progressbar", { name: "Syncing likes progress" });
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(bar).toHaveAttribute("data-indeterminate");
  });

  it("a known total makes the bar determinate, and the stream updates it live", async () => {
    jobs.library_sync = run("library_sync", "running", { phase: "saving", provider: "prov", processed: 25, total: 100 });
    renderFavorites();
    const bar = await screen.findByRole("progressbar", { name: "Syncing likes progress" });
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByText("Saving likes from Provider: 25 of 100")).toBeInTheDocument();
    await waitFor(() => expect(mockCreateSource).toHaveBeenCalled());

    send({ type: "library_job", user_id: "u1", run: run("library_sync", "running", { phase: "saving", provider: "prov", processed: 75, total: 100 }) });

    await waitFor(() => expect(bar).toHaveAttribute("aria-valuenow", "75"));
    expect(screen.getByText("Saving likes from Provider: 75 of 100")).toBeInTheDocument();
  });

  it("'Sync likes' starts a run and the button turns busy at once", async () => {
    renderFavorites();
    const button = await screen.findByRole("button", { name: "Sync likes" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(mockSync).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAccessibleDescription("Waiting for the worker");
  });

  it("a click that finds a run already active says so instead of starting another", async () => {
    mockSync.mockResolvedValue({ ...run("library_sync", "running", { phase: "fetching", provider: "prov" }), created: false });
    renderFavorites();
    const button = await screen.findByRole("button", { name: "Sync likes" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(toast.info).toHaveBeenCalledWith("Sync already in progress"));
  });

  it("worker_unavailable: a readable message for a user", async () => {
    jobs.library_sync = run("library_sync", "failed", { error_code: "worker_unavailable", error_message: "No worker is running to start the job." });
    renderFavorites();
    expect(await screen.findByRole("alert")).toHaveTextContent("Background jobs can't run on the server right now. Try again later.");
  });

  it("worker_unavailable: what to do, for an admin", async () => {
    status = viewerStatus("admin");
    jobs.library_sync = run("library_sync", "failed", { error_code: "worker_unavailable", error_message: "No worker is running to start the job." });
    renderFavorites();
    expect(await screen.findByRole("alert")).toHaveTextContent("The worker isn't running: start the worker service, then try again.");
  });

  it("downloading the likes from elsewhere says they arrive as favorites, and follows the progress stream", async () => {
    renderFavorites("elsewhere");
    await screen.findByText("Liked 1");
    await waitFor(() => expect(mockCreateSource).toHaveBeenCalledTimes(1));

    openMenu("Download the ones liked elsewhere and add them to favorites");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Liked in the last month" }));
    await waitFor(() => expect(mockDownloadSync).toHaveBeenCalledWith("1m"));

    send({ type: "library_job", user_id: "u1", run: run("download_sync", "running", { phase: "checking", processed: 4, total: 10 }) });

    expect(await screen.findByText("Checking 4 of 10")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Downloading the ones liked elsewhere progress" })).toHaveAttribute("aria-valuenow", "40");
    const menu = screen.getByRole("button", { name: "Download the ones liked elsewhere and add them to favorites" });
    expect(menu).toBeDisabled();
    expect(menu).toHaveAccessibleDescription("Already looking for the tracks to download");
    expect(mockCreateSource).toHaveBeenCalledTimes(1);
  });
});

describe("downloading the ones liked elsewhere under the approval policy", () => {
  const askingPolicy = (autoUpTo: number | null) => ({
    ...DEFAULT_POLICY,
    rules: { ...DEFAULT_POLICY.rules!, liked_elsewhere: { mode: "approval" as const, threshold: 50, auto_up_to: autoUpTo } },
  });

  it("where the likes download always waits, the button says Request", async () => {
    status = viewerStatus("user", askingPolicy(0));
    renderFavorites("elsewhere");
    await screen.findByText("Liked 1");

    const button = screen.getByRole("button", { name: "Request the ones liked elsewhere (needs approval) and add them to favorites" });
    expect(button).toHaveTextContent("Request the ones liked elsewhere");
    expect(button).toHaveAttribute("title", "Needs approval");
  });

  it("where it goes through, it is still Download, and the tooltip says up to how many tracks", async () => {
    status = viewerStatus("user", DEFAULT_POLICY);
    renderFavorites("elsewhere");
    await screen.findByText("Liked 1");

    const button = screen.getByRole("button", { name: "Download the ones liked elsewhere and add them to favorites" });
    expect(button).toHaveAttribute("title", "Goes through at once up to 50 tracks; more needs approval");
    expect(screen.queryByRole("button", { name: /Request/ })).not.toBeInTheDocument();
  });

  it("an admin sees no change", async () => {
    status = viewerStatus("admin");
    renderFavorites("elsewhere");
    await screen.findByText("Liked 1");

    expect(screen.getByRole("button", { name: "Download the ones liked elsewhere and add them to favorites" })).toHaveAttribute(
      "title",
      "Download the ones liked elsewhere and add them to favorites",
    );
  });

  it("a request that is waiting replaces the button with its state, and says how many tracks wait", async () => {
    status = viewerStatus("user", askingPolicy(0));
    waiting = [waitingLikes];
    renderFavorites("elsewhere");
    await screen.findByText("Liked 1");

    expect(await screen.findByRole("status")).toHaveTextContent("Waiting for approval: 120 tracks");
    expect(screen.queryByRole("button", { name: /liked elsewhere/ })).not.toBeInTheDocument();
  });
});

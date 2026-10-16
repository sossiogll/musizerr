import { beforeEach, describe, expect, it, vi } from "vitest";
import { AxiosError, type AxiosResponse } from "axios";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { LOCALE_STORAGE_KEY } from "@/i18n";
import { connectivityMock } from "@/test/connectivity-mock";
import { approvedAlbum, deniedArtist, waitingAlbum, waitingLikes, waitingPlaylist } from "@/test/download-requests";
import { renderPage } from "@/test/render";
import { MyRequestsPanel } from "./downloads-requests";
import type { DownloadRequest } from "@/types";

vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const mockList = vi.fn();
const mockCancel = vi.fn();
vi.mock("@/lib/api-client", () => ({
  getMyRequests: (...a: unknown[]) => mockList(...a),
  cancelMyRequest: (...a: unknown[]) => mockCancel(...a),
}));

const page = (items: DownloadRequest[], over: { page?: number; pages?: number; total?: number } = {}) => ({
  items,
  total: over.total ?? items.length,
  page: over.page ?? 1,
  limit: 25,
  pages: over.pages ?? 1,
});

const renderPanel = () => renderPage(() => <MyRequestsPanel />, { path: "/downloads" });

beforeEach(() => {
  connectivityMock.reset();
  mockList.mockReset().mockResolvedValue(page([waitingPlaylist, approvedAlbum, deniedArtist, waitingLikes]));
  mockCancel.mockReset().mockResolvedValue({ request: { ...waitingPlaylist, status: "cancelled" }, changed: true });
  Object.values(mockToast).forEach((fn) => fn.mockReset());
});

describe("My requests", () => {
  it("each request shows its kind, title, tracks, state and reason, and links to what it asks for", async () => {
    renderPanel();

    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(4);

    const playlist = within(rows[0]);
    expect(playlist.getByRole("link", { name: "Rap 2010" })).toHaveAttribute("href", "/playlist/catalog/playlist-259");
    expect(playlist.getByText("Playlist")).toBeInTheDocument();
    expect(playlist.getByText("259 tracks to download")).toBeInTheDocument();
    expect(playlist.getByText("This kind always needs approval")).toBeInTheDocument();
    expect(playlist.getByText("Waiting")).toBeInTheDocument();
    expect(playlist.getByText(/^Asked /)).toBeInTheDocument();

    const album = within(rows[1]);
    expect(album.getByRole("link", { name: "Box Set" })).toHaveAttribute("href", "/album/catalog/album-31");
    expect(album.getByText("Someone")).toBeInTheDocument();
    expect(album.getByText("Approved")).toBeInTheDocument();
    expect(album.getByText("31 queued")).toBeInTheDocument();
    expect(album.getByText("Note: Enjoy")).toBeInTheDocument();
    expect(album.queryByRole("button")).not.toBeInTheDocument();

    const artist = within(rows[2]);
    expect(artist.getByText("Denied")).toBeInTheDocument();
    expect(artist.getByText("Reason: Too much for this week")).toBeInTheDocument();
    expect(artist.getByRole("link", { name: "Someone" })).toHaveAttribute("href", "/artist/catalog/artist-1");

    // The likes download has no title or target: it is named by its kind and leads to the likes not in the library.
    const likes = within(rows[3]);
    expect(likes.getByRole("link", { name: "Liked elsewhere" })).toHaveAttribute("href", "/favorites?view=elsewhere");
  });

  it("the dates say when it was asked and when it was decided, in full on hover", async () => {
    renderPanel();

    const rows = await screen.findAllByRole("listitem");
    const times = rows[1].querySelectorAll("time");
    expect([...times].map((t) => t.getAttribute("dateTime"))).toEqual(["2026-10-04T08:00:00+00:00", "2026-10-04T09:00:00+00:00"]);
    expect(times[0].title).not.toBe("");
    expect(within(rows[1]).getByText(/^decided /)).toBeInTheDocument();
  });

  it("only a request that waits can be cancelled; cancelling says so and the list is read again", async () => {
    renderPanel();
    await screen.findAllByRole("listitem");
    expect(screen.getAllByRole("button", { name: /^Cancel the request for/ })).toHaveLength(2);

    mockList.mockResolvedValue(page([{ ...waitingPlaylist, status: "cancelled" }, approvedAlbum]));
    fireEvent.click(screen.getByRole("button", { name: "Cancel the request for Rap 2010" }));

    await waitFor(() => expect(mockCancel).toHaveBeenCalledWith("req-playlist"));
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("Request cancelled: Rap 2010"));
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Cancel the request for/ })).not.toBeInTheDocument();
  });

  it("an admin decided it meanwhile (409): says so in words and shows how it stands now", async () => {
    mockCancel.mockRejectedValue(
      new AxiosError("409", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 409,
        data: { detail: { error_code: "request_already_decided", error_message: "The request was already approved." } },
      } as AxiosResponse),
    );
    renderPanel();
    await screen.findAllByRole("listitem");

    mockList.mockResolvedValue(page([{ ...waitingPlaylist, status: "approved", queued_tracks: 259 }]));
    fireEvent.click(screen.getByRole("button", { name: "Cancel the request for Rap 2010" }));

    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith("An administrator already decided this request."));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
  });

  it("filters by state: the first page of that state", async () => {
    renderPanel();
    await screen.findAllByRole("listitem");
    expect(mockList).toHaveBeenLastCalledWith({ page: 1, limit: 25, status: undefined });

    fireEvent.click(screen.getByRole("button", { name: "State" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Denied" }));

    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith({ page: 1, limit: 25, status: ["denied"] }));
    expect(screen.getByRole("button", { name: "Denied" })).toBeInTheDocument();
  });

  it("is paginated by the server: a page at a time", async () => {
    mockList.mockResolvedValue(page([waitingPlaylist], { pages: 3, total: 60 }));
    renderPanel();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith({ page: 2, limit: 25, status: undefined }));
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("says so when nothing needed approval, and when a state has none", async () => {
    mockList.mockResolvedValue(page([]));
    renderPanel();
    expect(await screen.findByText("You have not asked for anything that needed approval.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "State" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Waiting" }));
    expect(await screen.findByText("No requests in this state.")).toBeInTheDocument();
  });

  it("a list that cannot be loaded says so", async () => {
    mockList.mockRejectedValue(new Error("network"));
    renderPanel();
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
  });

  it("in Italian, with the plural forms", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    mockList.mockResolvedValue(page([waitingPlaylist, { ...waitingAlbum, needed_tracks: 1 }]));
    renderPanel();

    expect(await screen.findByText("259 brani da scaricare")).toBeInTheDocument();
    expect(screen.getByText("1 brano da scaricare")).toBeInTheDocument();
    expect(screen.getAllByText("In attesa").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Annulla la richiesta per Rap 2010" })).toBeInTheDocument();
  });
});

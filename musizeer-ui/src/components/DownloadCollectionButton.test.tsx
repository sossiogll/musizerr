import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n";
import { DEFAULT_POLICY, viewerStatus } from "@/test/viewer";
import { pendingApproval, waitingPlaylist } from "@/test/download-requests";
import { CollectionDownloadAction, DownloadCollectionButton } from "./DownloadCollectionButton";
import type { AuthConfig, DownloadRequest, LibraryCoverage } from "@/types";

let status: AuthConfig;
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
vi.mock("@/contexts/QueueContext", () => ({ useQueueActions: () => vi.fn() }));
const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const api = { album: vi.fn(), artist: vi.fn(), playlist: vi.fn() };
let waiting: DownloadRequest[] = [];
vi.mock("@/lib/api-client", () => ({
  downloadAlbum: (...a: unknown[]) => api.album(...a),
  downloadArtist: (...a: unknown[]) => api.artist(...a),
  downloadPlaylist: (...a: unknown[]) => api.playlist(...a),
  getMyRequests: async () => ({ items: waiting, total: waiting.length, page: 1, limit: 200, pages: 1 }),
}));

const coverage = (over: Partial<LibraryCoverage>): LibraryCoverage => ({ total: 40, downloaded: 0, in_progress: 0, failed: 0, ...over });

function renderAction(ui: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>{ui}</I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  status = viewerStatus("user", DEFAULT_POLICY);
  waiting = [];
  Object.values(api).forEach((m) => m.mockReset().mockResolvedValue(pendingApproval));
  Object.values(mockToast).forEach((m) => m.mockReset());
});

describe("an album or a playlist's download says honestly what it will do", () => {
  it("within what goes through at once: Download, as before", async () => {
    renderAction(<CollectionDownloadAction kind="album" provider="prov" id="al1" name="The Album" library={coverage({ total: 10 })} />);

    const button = await screen.findByRole("button", { name: "Download all and add them to favorites" });
    expect(button).toHaveTextContent("Download all");
    expect(screen.queryByText(/Needs approval/)).not.toBeInTheDocument();
  });

  it("over what goes through at once: Request, with how many tracks it needs and how many go at once", async () => {
    renderAction(<CollectionDownloadAction kind="album" provider="prov" id="al1" name="The Album" library={coverage({ total: 40 })} />);

    const button = await screen.findByRole("button", { name: "Request The Album (needs approval) and add it to favorites" });
    expect(button).toHaveTextContent("Request");
    expect(button).not.toHaveTextContent("Download");
    expect(screen.getByText("Needs approval: 40 tracks (up to 30 go through at once)")).toBeInTheDocument();
    expect(button).toHaveAttribute("title", "Needs approval: 40 tracks (up to 30 go through at once)");
  });

  it("counts only what is missing: an album mostly in the library goes through", async () => {
    renderAction(<CollectionDownloadAction kind="album" provider="prov" id="al1" name="The Album" library={coverage({ total: 40, downloaded: 20, in_progress: 5 })} />);

    expect(await screen.findByRole("button", { name: "Download the 15 missing and add them to favorites" })).toBeInTheDocument();
  });

  it("a playlist always waits under the default policy", async () => {
    renderAction(<CollectionDownloadAction kind="playlist" provider="prov" id="pl1" name="Rap 2010" library={coverage({ total: 259 })} />);

    expect(await screen.findByRole("button", { name: "Request Rap 2010 (needs approval) and add it to favorites" })).toHaveTextContent("Request");
    expect(screen.getByText("Needs approval: 259 tracks")).toBeInTheDocument();
  });

  it("pressing Request sends it: a toast that says so, then 'Waiting for approval' in place of the button", async () => {
    renderAction(<CollectionDownloadAction kind="playlist" provider="prov" id="pl1" name="Rap 2010" library={coverage({ total: 259 })} />);

    fireEvent.click(await screen.findByRole("button", { name: /^Request Rap 2010/ }));
    waiting = [{ ...waitingPlaylist, target: { provider: "prov", id: "pl1" } }];

    await waitFor(() => expect(api.playlist).toHaveBeenCalledWith("prov", "pl1", { onlyMissing: true, addToFavorites: true }));
    await waitFor(() => expect(mockToast.info).toHaveBeenCalledWith("Sent for approval: 259 tracks to download", { description: "This kind always needs approval" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Waiting for approval");
    expect(screen.queryByRole("button", { name: /Request|Download/ })).not.toBeInTheDocument();
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it("the wait survives a reload: it comes from the server's list of waiting requests", async () => {
    waiting = [{ ...waitingPlaylist, target: { provider: "prov", id: "pl1" } }];
    renderAction(<CollectionDownloadAction kind="playlist" provider="prov" id="pl1" name="Rap 2010" library={coverage({ total: 259 })} />);

    expect(await screen.findByRole("status")).toHaveTextContent("Waiting for approval");
  });

  it("a request waiting for another playlist does not hold this one", async () => {
    waiting = [{ ...waitingPlaylist, target: { provider: "prov", id: "other" } }];
    renderAction(<CollectionDownloadAction kind="playlist" provider="prov" id="pl1" name="Rap 2010" library={coverage({ total: 259 })} />);

    expect(await screen.findByRole("button", { name: /^Request Rap 2010/ })).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("an admin sees no change: Download, no hint, nothing asked of the server", async () => {
    status = viewerStatus("admin");
    renderAction(<CollectionDownloadAction kind="playlist" provider="prov" id="pl1" name="Rap 2010" library={coverage({ total: 259 })} />);

    expect(await screen.findByRole("button", { name: "Download all" })).toBeInTheDocument();
    expect(screen.queryByText(/Needs approval/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request/ })).not.toBeInTheDocument();
  });
});

describe("a whole artist", () => {
  const artist = <DownloadCollectionButton kind="artist" provider="prov" id="a1" name="Someone" label="Download everything" favoriteLabel="Download everything by Someone and add it to favorites" />;

  it("under a policy that always asks: Request, and the confirmation says an administrator has to approve it", async () => {
    renderAction(artist);

    const button = await screen.findByRole("button", { name: "Request Someone (needs approval) and add it to favorites" });
    expect(button).toHaveTextContent("Request");
    expect(screen.getByText("Needs approval")).toBeInTheDocument();

    fireEvent.click(button);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Send a request for Someone?");
    expect(dialog).toHaveTextContent("An administrator has to approve it before anything is downloaded.");
    expect(api.artist).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() => expect(api.artist).toHaveBeenCalledWith("prov", "a1", { onlyMissing: false, addToFavorites: true }));
    await waitFor(() => expect(mockToast.info).toHaveBeenCalled());
  });

  it("where it goes through, the confirmation is the old one about many downloads", async () => {
    status = viewerStatus("user");
    renderAction(artist);

    fireEvent.click(await screen.findByRole("button", { name: "Download everything by Someone and add it to favorites" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Download everything by Someone?");
    expect(dialog).toHaveTextContent("This can be many downloads.");
  });
});

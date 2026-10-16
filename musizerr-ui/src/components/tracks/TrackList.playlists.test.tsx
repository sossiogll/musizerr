import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { TrackList } from "./TrackList";
import type { AuthConfig, ViewerMode } from "@/types";

// Every row has its menu button for any viewer (the playlists are one of its
// entries, for a viewer who has playlists), and one grid with room for it.

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getUserPlaylists: async () => ({ items: [], max_tracks: 500 }),
  getPublicProviders: async () => ({ providers: [], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const tracks = [makeTrack({ provider_id: "a", title: "Song A" }), makeTrack({ provider_id: "b", title: "Song B" })];
const renderList = () => renderPage(() => <TrackList tracks={tracks} header />, { path: "/", queue: true });
const buttons = () => screen.queryAllByRole("button", { name: /^More actions for/ });

beforeEach(() => {
  status = viewerStatus("user");
  connectivityMock.reset();
});

describe("TrackList and the row menu", () => {
  it.each<[ViewerMode, number]>([["user", 2], ["admin", 2], ["open", 2], ["guest", 2]])("%s: %i menu buttons", async (mode, expected) => {
    status = viewerStatus(mode);
    renderList();
    await screen.findByText("Song A");
    expect(buttons()).toHaveLength(expected);
  });

  it("names each button after its track", async () => {
    renderList();
    await screen.findByText("Song A");
    expect(buttons().map((b) => b.getAttribute("aria-label"))).toEqual(["More actions for Song A", "More actions for Song B"]);
  });

  it("holds the status, the heart and the menu button in a 216px last column on every row, and the header", async () => {
    const { container } = renderList();
    await screen.findByText("Song A");
    expect(container.querySelector("li > div")!.className).toContain("md:grid-cols-[40px_minmax(0,1fr)_48px_216px]");
    expect(container.querySelector("[aria-hidden].lg\\:grid")!.className).toContain("lg:grid-cols-[40px_minmax(0,2fr)_minmax(0,1.4fr)_56px_216px]");
  });

  it("offers Add to playlist in the menu of a viewer who has playlists", async () => {
    renderList();
    await screen.findByText("Song A");
    fireEvent.click(buttons()[0]);
    expect(await screen.findByRole("menuitem", { name: "Add to playlist" })).toBeInTheDocument();
  });

  it("and not to a guest", async () => {
    status = viewerStatus("guest");
    renderList();
    await screen.findByText("Song A");
    fireEvent.click(buttons()[0]);
    await screen.findByRole("menu");
    expect(screen.queryByRole("menuitem", { name: "Add to playlist" })).not.toBeInTheDocument();
  });
});

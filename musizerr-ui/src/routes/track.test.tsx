import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { renderPage } from "@/test/render";
import { TrackPage } from "./track";
import type { Lyrics, Track } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("user"), user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const player = {
  track: null as null | { provider: string; provider_id: string },
  playbackType: null as null | "full" | "preview",
  playing: false,
  currentTime: 0,
};
const mockSeek = vi.fn();
const mockAddToQueue = vi.fn();
const mockPlay = vi.fn();
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => ({ ...player, seek: mockSeek, addToQueue: mockAddToQueue, play: mockPlay }),
  usePlayerProgress: () => ({ currentTime: player.currentTime, duration: 200 }),
}));

let track: Track;
let lyrics: Lyrics | null;
let related: Track[];
let relatedErrors: { provider: string; error_code: string; error_message: string }[];
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getTrack: async () => track,
  getTrackLyrics: async () => {
    if (!lyrics) throw new Error("404");
    return lyrics;
  },
  getAppearsOn: async () => ({ items: [{ title: "Best Of", year: 2010, provider: "prov", provider_id: "al9", track_provider_id: "x" }], provider_errors: [] }),
  getRelatedTracks: async () => ({ items: related, provider_errors: relatedErrors }),
  getDownloadStates: async () => DOWNLOAD_STATES,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }, { id: "lyr", label: "Lyrics Co" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  createProgressSource: () => ({ close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 }),
}));

const SYNCED: Lyrics = {
  plain: null,
  source: "lyr",
  synced: [
    { time_ms: 0, text: "First line" },
    { time_ms: 10_000, text: "Second line" },
    { time_ms: 20_000, text: "Third line" },
  ],
};

const renderTrack = () => renderPage(TrackPage, { path: "/track/$provider/$id", url: "/track/prov/p1", queue: true });

beforeEach(() => {
  connectivityMock.reset();
  mockSeek.mockReset();
  mockAddToQueue.mockReset();
  track = makeTrack({ track_number: 4, stream_url: "/api/stream/x" });
  lyrics = SYNCED;
  related = [];
  relatedErrors = [];
  Object.assign(player, { track: null, playbackType: null, playing: false, currentTime: 0 });
});

describe("Track page", () => {
  it("a provider that failed for the related tracks is named in that section only", async () => {
    related = [makeTrack({ provider_id: "r1", title: "Other Take" })];
    relatedErrors = [{ provider: "prov", error_code: "metadata_provider_timeout", error_message: "x" }];
    renderTrack();

    const notice = await screen.findByText(/Results may be incomplete/);
    const section = notice.closest("section")!;
    expect(section).toHaveTextContent("Related");
    expect(section).toHaveTextContent("Provider did not answer.");
    // Not page-wide: the lyrics and the appears-on list say nothing about it.
    expect(screen.getAllByText(/Results may be incomplete/)).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Lyrics" }).closest("section")).not.toHaveTextContent(/incomplete/);
  });

  it("the related section stays, with its notice, when the failure left it empty", async () => {
    relatedErrors = [{ provider: "prov", error_code: "metadata_provider_timeout", error_message: "x" }];
    renderTrack();
    const notice = await screen.findByText(/Results may be incomplete/);
    expect(notice.closest("section")).toHaveTextContent("Related");
  });

  it("shows the header facts and the lyrics source by its label", async () => {
    renderTrack();
    expect(await screen.findByRole("heading", { level: 1, name: "Song A" })).toBeInTheDocument();
    expect(screen.getByText(/Track · No\. 4 · 2020/)).toBeInTheDocument();
    expect(await screen.findByText("via Lyrics Co")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Best Of\s?2010$/ })).toHaveAttribute("href", "/album/prov/al9");
  });

  it("synced lyrics follow the player: the current line is marked and a line seeks there", async () => {
    Object.assign(player, { track: { provider: "prov", provider_id: "p1" }, playbackType: "full", playing: true, currentTime: 12 });
    renderTrack();
    const active = await screen.findByRole("button", { name: "Second line" });
    expect(active).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("Synced")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Third line" }));
    expect(mockSeek).toHaveBeenCalledWith(20);
  });

  it("from L the lyrics stick in a column as tall as the right one; below L they are in the page flow", async () => {
    renderTrack();
    await screen.findByText("Second line");
    // The panel (the section) and, inside it, the box.
    const [panel, box] = screen.getAllByRole("region", { name: "Lyrics" });
    const column = panel.parentElement!;

    // The column follows the grid row (the right column) and adds no height of its own.
    expect(column).toHaveClass("lg:min-h-80", "lg:[contain:size]");
    // The panel sticks under the shell's top bar, as tall as the column or the screen between the bars.
    expect(panel).toHaveClass("lg:sticky", "lg:flex", "lg:flex-col");
    expect(panel.className).toContain("lg:top-[calc(var(--shell-top)_+_1rem)]");
    expect(panel.className).toContain("lg:h-[min(100%,calc(100dvh_-_var(--shell-top)_-_var(--player-h)_-_2rem))]");
    // The box scrolls on its own from L only.
    expect(box).toHaveClass("lg:flex-1", "lg:min-h-0", "lg:overflow-y-auto");
    const unprefixed = [...box.classList, ...panel.classList].filter((c) => !c.includes(":"));
    expect(unprefixed.filter((c) => /^(sticky|overflow-|max-h-|h-|top-)/.test(c))).toEqual([]);
  });

  it("following the player scrolls the lyrics box, never the page", async () => {
    const boxScroll = vi.fn();
    const windowScroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const scrollIntoView = vi.fn();
    Object.assign(HTMLElement.prototype, { scrollTo: boxScroll, scrollIntoView });
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), media: query, addEventListener() {}, removeEventListener() {} }));
    try {
      Object.assign(player, { track: { provider: "prov", provider_id: "p1" }, playbackType: "full", playing: true, currentTime: 12 });
      renderTrack();
      await screen.findByRole("button", { name: "Second line" });
      // The router puts the window at the top once, when the page loads: that is not the lyrics following.
      windowScroll.mockClear();

      await waitFor(() => expect(boxScroll).toHaveBeenCalled());
      expect(boxScroll.mock.contexts.at(-1)).toBe(screen.getAllByRole("region", { name: "Lyrics" })[1]);
      // Reduced motion: no smooth scrolling.
      expect(boxScroll).toHaveBeenLastCalledWith(expect.objectContaining({ behavior: "auto" }));
      expect(windowScroll).not.toHaveBeenCalled();
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      windowScroll.mockRestore();
      vi.unstubAllGlobals();
      delete (HTMLElement.prototype as Partial<HTMLElement>).scrollTo;
      delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
    }
  });

  it("while another track plays, the lyrics are plain text, not buttons", async () => {
    Object.assign(player, { track: { provider: "prov", provider_id: "other" }, playbackType: "full", playing: true });
    renderTrack();
    expect(await screen.findByText("Second line")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Second line" })).not.toBeInTheDocument();
  });

  it("no lyrics: says so", async () => {
    lyrics = null;
    renderTrack();
    expect(await screen.findByText("No lyrics found for this track.")).toBeInTheDocument();
  });

  it("hides Related when no provider offers any, shows it otherwise", async () => {
    renderTrack();
    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Also on" })).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Related" })).not.toBeInTheDocument();
  });

  it("the page's menu queues the track, and has no separate Add to queue button beside it", async () => {
    related = [makeTrack({ provider_id: "r1", title: "Other" })];
    renderTrack();
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: "Add to queue" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "More actions for Song A" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Add to queue" }));
    expect(mockAddToQueue).toHaveBeenCalledWith(expect.objectContaining({ provider_id: "p1" }));
    expect(await screen.findByRole("heading", { name: "Related" })).toBeInTheDocument();
  });
});

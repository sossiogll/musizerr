import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { DOWNLOAD_STATES, makeTrack } from "@/test/fixtures";
import { TrackRow } from "@/components/tracks/TrackRow";
import { Player } from "./Player";
import { toPlayerTrack, type PlayerTrack } from "@/contexts/PlayerContext";
import type { AuthConfig, Lyrics } from "@/types";

let status: AuthConfig = viewerStatus("guest");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const item = (id: string, title: string, playable = true): PlayerTrack => ({
  title, artist: "Artist", cover_url: null, preview_url: playable ? "https://cdn.example/p.mp3" : null, stream_url: null,
  playback: playable ? { type: "preview", source: "provider" } : { type: "none", source: null },
  provider: "prov", provider_id: id, artist_provider_id: "ar1",
});

const queue = [item("1", "One"), item("2", "Two"), item("3", "Three (no audio)", false)];
const controls = {
  track: queue[0] as PlayerTrack | null,
  playing: true,
  playbackType: "preview" as "preview" | "full" | null,
  volume: 0.8,
  queue,
  index: 0,
  order: [0, 1, 2],
  shuffle: false,
  repeat: "off" as "off" | "all" | "one",
  hasNext: true,
  interlude: false,
  play: vi.fn(),
  playOnce: vi.fn(),
  jumpTo: vi.fn(),
  toggleShuffle: vi.fn(),
  cycleRepeat: vi.fn(),
  togglePlay: vi.fn(),
  next: vi.fn(),
  previous: vi.fn(),
  addToQueue: vi.fn(),
  seek: vi.fn(),
  setVolume: vi.fn(),
};
vi.mock("@/contexts/PlayerContext", async () => ({
  ...(await vi.importActual<typeof import("@/contexts/PlayerContext")>("@/contexts/PlayerContext")),
  usePlayer: () => controls,
  usePlayerProgress: () => ({ currentTime: 30, duration: 200 }),
}));

let lyrics: Lyrics | null;
const getTrackLyrics = vi.fn(async () => {
  if (!lyrics) throw new Error("404");
  return lyrics;
});
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getTrackLyrics: () => getTrackLyrics(),
  getPublicProviders: async () => ({ providers: [{ id: "lyr", label: "Lyrics Co" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  getDownloadStates: async () => DOWNLOAD_STATES,
  downloadTrack: (...a: unknown[]) => mockDownloadTrack(...a),
  addFavorite: (...a: unknown[]) => mockAddFavorite(...a),
  removeFavorite: (...a: unknown[]) => mockRemoveFavorite(...a),
  getFavorite: (...a: unknown[]) => mockGetFavorite(...a),
}));
const mockDownloadTrack = vi.fn();
const mockAddFavorite = vi.fn();
const mockRemoveFavorite = vi.fn();
const mockGetFavorite = vi.fn();
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());

// The shared lyrics component, spied on to prove the panel renders it (the track page does too).
const lyricsBody = vi.hoisted(() => ({ calls: 0 }));
vi.mock("@/components/lyrics/Lyrics", async () => {
  const actual = await vi.importActual<typeof import("@/components/lyrics/Lyrics")>("@/components/lyrics/Lyrics");
  return {
    ...actual,
    LyricsBody: (props: Parameters<typeof actual.LyricsBody>[0]) => {
      lyricsBody.calls++;
      return actual.LyricsBody(props);
    },
  };
});

const SYNCED: Lyrics = {
  plain: null,
  source: "lyr",
  synced: [
    { time_ms: 0, text: "First line" },
    { time_ms: 20_000, text: "Second line" },
    { time_ms: 40_000, text: "Third line" },
  ],
};

const renderPlayer = () => renderPage(Player, { path: "/" });

beforeEach(() => {
  lyrics = SYNCED;
  lyricsBody.calls = 0;
  status = viewerStatus("guest");
  Object.assign(controls, {
    track: queue[0], playing: true, playbackType: "preview", index: 0, volume: 0.8,
    order: [0, 1, 2], shuffle: false, repeat: "off", hasNext: true, interlude: false,
  });
  for (const fn of [
    controls.play, controls.togglePlay, controls.next, controls.previous, controls.seek, controls.setVolume,
    controls.jumpTo, controls.toggleShuffle, controls.cycleRepeat, mockDownloadTrack, mockAddFavorite, mockRemoveFavorite,
  ]) fn.mockReset();
});

describe("Player", () => {
  it("nothing loaded: nothing rendered", () => {
    controls.track = null;
    renderPlayer();
    expect(screen.queryByRole("region", { name: "Player" })).not.toBeInTheDocument();
  });

  it("previous, pause and next, each labelled; a preview says so", async () => {
    renderPlayer();
    const player = await screen.findByRole("region", { name: "Player" });
    fireEvent.click(within(player).getByRole("button", { name: "Previous" }));
    fireEvent.click(within(player).getByRole("button", { name: "Pause" }));
    fireEvent.click(within(player).getByRole("button", { name: "Next" }));
    expect(controls.previous).toHaveBeenCalled();
    expect(controls.togglePlay).toHaveBeenCalled();
    expect(controls.next).toHaveBeenCalled();
    expect(within(player).getByText("Preview")).toBeInTheDocument();
  });

  it("Next is disabled on the last track of the queue", async () => {
    Object.assign(controls, { track: queue[2], index: 2, hasNext: false });
    renderPlayer();
    expect(await screen.findByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("the seek slider moves the playback, with the position in words", async () => {
    renderPlayer();
    const slider = await screen.findByRole("slider", { name: "Position" });
    expect(slider).toHaveAttribute("aria-valuetext", "0:30 of 3:20");
    fireEvent.change(slider, { target: { value: "90" } });
    expect(controls.seek).toHaveBeenCalledWith(90);
  });

  it("the queue panel lists what plays next and jumps to a track", async () => {
    renderPlayer();
    const toggle = await screen.findByRole("button", { name: "Queue (3)" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const panel = screen.getByRole("region", { name: "Up next" });
    expect(within(panel).getByRole("button", { name: /One/ })).toHaveAttribute("aria-current", "true");
    expect(within(panel).getByRole("button", { name: /Three/ })).toBeDisabled();

    fireEvent.click(within(panel).getByRole("button", { name: /Two/ }));
    expect(controls.jumpTo).toHaveBeenCalledWith(1);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "Up next" })).not.toBeInTheDocument();
  });

  it("mute remembers the volume to restore", async () => {
    renderPlayer();
    fireEvent.click(await screen.findByRole("button", { name: "Mute" }));
    expect(controls.setVolume).toHaveBeenCalledWith(0);
  });

  it("three zones: equal side columns, so the controls sit in the middle of the bar", async () => {
    renderPlayer();
    await screen.findByRole("region", { name: "Player" });
    const zones = screen.getByTestId("player-zones");
    expect(zones).toHaveClass("sm:grid", "sm:grid-cols-[minmax(var(--player-side),1fr)_minmax(0,min(36rem,50%))_minmax(var(--player-side),1fr)]");
    // Each tier sizes both side columns alike.
    expect(zones).toHaveClass("[--player-side:6rem]", "md:[--player-side:9rem]", "lg:[--player-side:15rem]", "xl:[--player-side:20rem]");
    expect([...zones.children].slice(0, 3)).toEqual([screen.getByTestId("player-track"), screen.getByTestId("player-controls"), screen.getByTestId("player-actions")]);
    expect(screen.getByTestId("player-controls")).toHaveClass("items-center", "sm:justify-self-center", "sm:w-full");
    expect(screen.getByTestId("player-track")).toHaveClass("min-w-0");
    expect(screen.getByTestId("player-actions")).toHaveClass("justify-end");
  });

  it("Lyrics and Queue: labelled from L, icon-only with the same name on S and M", async () => {
    renderPlayer();
    const actions = await screen.findByTestId("player-actions");
    const lyricsButton = within(actions).getByRole("button", { name: "Lyrics" });
    const queueButton = within(actions).getByRole("button", { name: "Queue (3)" });
    for (const [button, label] of [[lyricsButton, "Lyrics"], [queueButton, "Queue"]] as const) {
      const text = within(button).getByText(label);
      expect(text).toHaveClass("hidden", "lg:inline");
      expect(button).toHaveClass("max-sm:hidden", "lg:border");
      expect(button).toHaveAttribute("aria-expanded", "false");
    }
  });

  it("the lyrics panel follows a full playback with the shared lyrics component: current line marked, a line seeks", async () => {
    controls.playbackType = "full";
    renderPlayer();
    const toggle = await screen.findByRole("button", { name: "Lyrics" });
    await waitFor(() => expect(toggle).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    const panel = screen.getAllByRole("region", { name: "Lyrics" })[0];
    expect(toggle).toHaveAttribute("aria-controls", panel.id);
    expect(panel).toHaveFocus();
    const line = await within(panel).findByRole("button", { name: "Second line" });
    expect(line).toHaveAttribute("aria-current", "true");
    fireEvent.click(within(panel).getByRole("button", { name: "Third line" }));
    expect(controls.seek).toHaveBeenCalledWith(40);
    expect(await within(panel).findByText("via Lyrics Co")).toBeInTheDocument();
    expect(lyricsBody.calls).toBeGreaterThan(0);
  });

  it("Queue and Lyrics are one panel at a time; Escape closes and gives focus back; so does a press outside", async () => {
    renderPlayer();
    const lyricsToggle = await screen.findByRole("button", { name: "Lyrics" });
    const queueToggle = screen.getByRole("button", { name: "Queue (3)" });
    await waitFor(() => expect(lyricsToggle).not.toHaveAttribute("aria-disabled"));

    fireEvent.click(lyricsToggle);
    expect(screen.getAllByRole("region", { name: "Lyrics" })[0]).toBeInTheDocument();
    fireEvent.click(queueToggle);
    expect(screen.getByRole("region", { name: "Up next" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Lyrics" })).not.toBeInTheDocument();
    expect(lyricsToggle).toHaveAttribute("aria-expanded", "false");
    expect(queueToggle).toHaveAttribute("aria-expanded", "true");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "Up next" })).not.toBeInTheDocument();
    expect(queueToggle).toHaveFocus();

    fireEvent.click(queueToggle);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("region", { name: "Up next" })).not.toBeInTheDocument();
  });

  it("without lyrics the Lyrics button stays focusable but disabled, with a tooltip saying why", async () => {
    lyrics = null;
    renderPlayer();
    const toggle = await screen.findByRole("button", { name: "Lyrics" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-disabled", "true"));
    expect(toggle).toHaveAccessibleDescription("No lyrics for this track");
    fireEvent.click(toggle);
    expect(screen.queryByRole("region", { name: "Lyrics" })).not.toBeInTheDocument();
    // It is the trigger of a tooltip with the same reason (not opened here: Radix's positioning keeps jsdom busy).
    expect(toggle).toHaveAttribute("data-state", "closed");
  });

  it("XS: lyrics and queue are in the overflow menu, and open the same panels", async () => {
    renderPlayer();
    const more = await screen.findByRole("button", { name: "More player options" });
    expect(more).toHaveClass("sm:hidden");
    fireEvent.click(more);
    fireEvent.click(await screen.findByRole("menuitem", { name: "Queue (3)" }));
    expect(screen.getByRole("region", { name: "Up next" })).toBeInTheDocument();

    fireEvent.click(more);
    fireEvent.click(await screen.findByRole("menuitem", { name: "Lyrics" }));
    expect(screen.getAllByRole("region", { name: "Lyrics" })[0]).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Up next" })).not.toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(more).toHaveFocus();
  });

  it("XS: shuffle and repeat are in the overflow menu too", async () => {
    Object.assign(controls, { shuffle: true, repeat: "all" });
    renderPlayer();
    const more = await screen.findByRole("button", { name: "More player options" });
    fireEvent.click(more);
    const shuffle = await screen.findByRole("menuitemcheckbox", { name: "Shuffle" });
    expect(shuffle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(shuffle);
    expect(controls.toggleShuffle).toHaveBeenCalled();
    fireEvent.click(more);
    fireEvent.click(await screen.findByRole("menuitem", { name: "Repeat: all" }));
    expect(controls.cycleRepeat).toHaveBeenCalled();
  });
});

describe("Player — shuffle and repeat", () => {
  it("shuffle is a toggle button, pressed when on", async () => {
    renderPlayer();
    const shuffle = await screen.findByRole("button", { name: "Shuffle" });
    expect(shuffle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(shuffle);
    expect(controls.toggleShuffle).toHaveBeenCalled();
  });

  it.each([
    ["off", "Repeat: off", "false"],
    ["all", "Repeat: all", "true"],
    ["one", "Repeat: this track", "true"],
  ] as const)("repeat %s: named after its mode, pressed while it repeats", async (mode, name, pressed) => {
    controls.repeat = mode;
    renderPlayer();
    const repeat = await screen.findByRole("button", { name });
    expect(repeat).toHaveAttribute("aria-pressed", pressed);
    fireEvent.click(repeat);
    expect(controls.cycleRepeat).toHaveBeenCalled();
  });

  it("the queue panel lists the tracks in the order they will play", async () => {
    Object.assign(controls, { shuffle: true, order: [0, 2, 1] });
    renderPlayer();
    fireEvent.click(await screen.findByRole("button", { name: "Queue (3)" }));
    const panel = screen.getByRole("region", { name: "Up next" });
    const titles = within(panel).getAllByRole("button").map((b) => b.textContent);
    expect(titles).toEqual(["1OneArtist", "2Three (no audio)Artist", "3TwoArtist"]);
    fireEvent.click(within(panel).getByRole("button", { name: /Two/ }));
    expect(controls.jumpTo).toHaveBeenCalledWith(1);
  });
});

describe("Player — the heart of the track playing", () => {
  const libraryTrack = (over = {}) =>
    toPlayerTrack(makeTrack({ title: "One", provider: "prov", provider_id: "1", download_status: "downloaded", in_library: true, library_id: "L1", favorite: false, ...over }));

  it("a guest gets neither a heart nor a download", async () => {
    controls.track = libraryTrack();
    renderPlayer();
    await screen.findByRole("region", { name: "Player" });
    expect(screen.queryByRole("button", { name: /favorites/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
  });

  it("in the library: the heart toggles at once, shared with the row of the same track", async () => {
    status = viewerStatus("user");
    controls.track = libraryTrack();
    mockAddFavorite.mockResolvedValue({ track_id: "L1", favorite: true });
    const track = makeTrack({ title: "One", provider: "prov", provider_id: "1", download_status: "downloaded", in_library: true, library_id: "L1", favorite: false });
    renderPage(() => (
      <>
        <TrackRow track={track} />
        <Player />
      </>
    ), { path: "/" });

    const player = await screen.findByRole("region", { name: "Player" });
    const heart = await within(player).findByRole("button", { name: "Add One to favorites" });
    expect(heart).toHaveAttribute("aria-pressed", "false");
    expect(heart).toHaveClass("size-11");
    fireEvent.click(heart);

    await waitFor(() => expect(mockAddFavorite).toHaveBeenCalledWith("L1"));
    expect(await within(player).findByRole("button", { name: "Remove One from favorites" })).toHaveAttribute("aria-pressed", "true");
    // The row of the same track shows it too: one shared state.
    const both = await screen.findAllByRole("button", { name: "Remove One from favorites" });
    expect(both).toHaveLength(2);
  });

  it("not in the library, a user: one Download that also adds it to favorites", async () => {
    status = viewerStatus("user");
    controls.track = libraryTrack({ download_status: null, in_library: false, library_id: null });
    mockDownloadTrack.mockResolvedValue({ status: "queued", download_id: "d1" });
    renderPlayer();
    const download = await screen.findByRole("button", { name: "Download One and add it to favorites" });
    expect(screen.queryByRole("button", { name: /Add One to favorites/ })).not.toBeInTheDocument();
    fireEvent.click(download);
    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalledWith("prov", "1", expect.anything(), { addToFavorites: true }));
  });

  it("not in the library, an admin: the split choice, as a menu", async () => {
    status = viewerStatus("admin");
    controls.track = libraryTrack({ download_status: null, in_library: false, library_id: null });
    mockDownloadTrack.mockResolvedValue({ status: "queued", download_id: "d1" });
    renderPlayer();
    fireEvent.click(await screen.findByRole("button", { name: "Download options for One" }));
    expect(screen.getByRole("menuitem", { name: "Download" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Download and add to favorites" }));
    await waitFor(() => expect(mockDownloadTrack).toHaveBeenCalledWith("prov", "1", expect.anything(), { addToFavorites: true }));
  });

  it("on its way with a favorite intent: the pending heart", async () => {
    status = viewerStatus("user");
    controls.track = libraryTrack({ download_status: "pending", in_library: false, favorite_pending: true });
    renderPlayer();
    expect(await screen.findByRole("img", { name: "Will be added to favorites when it arrives" })).toBeInTheDocument();
  });

  it("a file heard on its own says which file it is, with no heart and no track link", async () => {
    status = viewerStatus("admin");
    Object.assign(controls, {
      interlude: true,
      track: { ...item("m1", "Song"), provider: "", stream_url: "/api/x", playback: { type: "full", source: "library" }, detached: { note: "Duplicate file · OGG · 332 kbps" } },
    });
    renderPlayer();
    const player = await screen.findByRole("region", { name: "Player" });
    expect(within(player).getByText("Duplicate file · OGG · 332 kbps")).toBeInTheDocument();
    expect(within(player).queryByRole("link", { name: "Song" })).not.toBeInTheDocument();
    expect(within(player).queryByRole("button", { name: /favorites|Download/ })).not.toBeInTheDocument();
    expect(within(player).getByRole("button", { name: "Back to the queue" })).toBeInTheDocument();
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { renderHook, act } from "@testing-library/react";
import type { ReactNode } from "react";
import { PlayerProvider, usePlayer, usePlayerProgress, type PlayerTrack } from "./PlayerContext";
import { isRouted, openTap, resetAudioAnalysis } from "@/lib/audio-analysis";

// Full vs preview lo decide il server (``playback`` di ogni traccia, vedi
// services/track_state.py): a un anonimo non manda mai uno stream_url. Qui
// verifichiamo che PlayerContext.play() segua quella decisione, senza
// ricavarla da solo dagli URL o dalle capability.

const mockUseAuth = vi.fn();

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockToastError = vi.fn();
vi.mock("sonner", () => ({ toast: { error: (...args: unknown[]) => mockToastError(...args) } }));

function makeTrack(overrides: Partial<PlayerTrack> = {}): PlayerTrack {
  return {
    title: "Song",
    artist: "Artist",
    cover_url: null,
    preview_url: "http://preview/x.mp3",
    stream_url: "/stream/abc",
    playback: { type: "full", source: "library" },
    provider: "fake",
    provider_id: "abc",
    artist_provider_id: null,
    ...overrides,
  };
}

function renderPlayer() {
  return renderPlayerWith({});
}

function renderPlayerWith(props: { refreshTrack?: (track: PlayerTrack) => Promise<PlayerTrack | null> }) {
  return renderHook(() => usePlayer(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <PlayerProvider {...props}>{children}</PlayerProvider>
    ),
  });
}

describe("PlayerContext — full vs preview come dice il server", () => {
  beforeEach(() => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user") });
    window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
  });

  it("playback full: lo stream completo", () => {
    const { result } = renderPlayer();
    act(() => {
      result.current.play(makeTrack());
    });
    expect(result.current.playbackType).toBe("full");
  });

  it("playback preview: la preview, anche se un stream_url c'e'", () => {
    const { result } = renderPlayer();
    act(() => {
      result.current.play(makeTrack({ playback: { type: "preview", source: "provider" } }));
    });
    expect(result.current.playbackType).toBe("preview");
  });

  it("playback none (o assente): non riproduce nulla", () => {
    const { result } = renderPlayer();
    act(() => {
      result.current.play(makeTrack({ playback: { type: "none", source: null } }));
    });
    expect(result.current.track).toBeNull();
    act(() => {
      result.current.play(makeTrack({ playback: undefined }));
    });
    expect(result.current.playbackType).toBeNull();
  });
});

describe("PlayerContext — queue, previous/next", () => {
  let audios: HTMLAudioElement[] = [];
  const RealAudio = window.Audio;

  beforeEach(() => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user") });
    window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
    audios = [];
    window.Audio = class extends RealAudio {
      constructor() {
        super();
        audios.push(this);
      }
    } as typeof Audio;
  });

  const list = [
    makeTrack({ provider_id: "1", title: "One" }),
    makeTrack({ provider_id: "2", title: "Two (no audio)", stream_url: null, preview_url: null, playback: { type: "none", source: null } }),
    makeTrack({ provider_id: "3", title: "Three" }),
  ];

  it("uses the backend's stream_url as it is: already under the public prefix", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(makeTrack({ stream_url: "/api/stream/provider/sync/abc" })));
    expect(audios[0].src).toBe("http://localhost:3000/api/stream/provider/sync/abc");
  });

  it("playing from a list queues the list; next skips what cannot play", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[0], list));
    expect(result.current.queue).toHaveLength(3);
    expect(result.current.track?.title).toBe("One");

    act(() => result.current.next());
    expect(result.current.track?.title).toBe("Three");
    expect(result.current.index).toBe(2);
  });

  it("previous goes back, or restarts the track after a few seconds", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[2], list));

    act(() => {
      audios[0].currentTime = 10;
      result.current.previous();
    });
    expect(result.current.track?.title).toBe("Three");
    expect(audios[0].currentTime).toBe(0);

    act(() => result.current.previous());
    expect(result.current.track?.title).toBe("One");
  });

  it("moves on by itself when a track ends", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[0], list));
    act(() => {
      audios[0].dispatchEvent(new Event("ended"));
    });
    expect(result.current.track?.title).toBe("Three");
  });

  it("add to queue appends once, and plays when nothing is loaded", () => {
    const { result } = renderPlayer();
    act(() => result.current.addToQueue(list[0]));
    expect(result.current.track?.title).toBe("One");

    act(() => {
      result.current.addToQueue(list[2]);
      result.current.addToQueue(list[2]);
    });
    expect(result.current.queue.map((t) => t.title)).toEqual(["One", "Three"]);
    expect(result.current.track?.title).toBe("One");
  });

  it("play next puts the track right after the current one, and plays when nothing is loaded", () => {
    const four = makeTrack({ provider_id: "4", title: "Four" });
    const { result } = renderPlayer();
    act(() => result.current.playNext(four));
    expect(result.current.track?.title).toBe("Four");

    act(() => result.current.play(list[0], [list[0], list[2], four]));
    const five = makeTrack({ provider_id: "5", title: "Five" });
    act(() => result.current.playNext(five));
    expect(result.current.queue.map((t) => t.title)).toEqual(["One", "Three", "Four", "Five"]);
    act(() => result.current.next());
    expect(result.current.track?.title).toBe("Five");
    act(() => result.current.next());
    expect(result.current.track?.title).toBe("Three");
  });

  it("play next moves a track that is already queued instead of adding it twice", () => {
    const four = makeTrack({ provider_id: "4", title: "Four" });
    const { result } = renderPlayer();
    act(() => result.current.play(list[0], [list[0], list[2], four]));
    act(() => result.current.playNext(four));
    expect(result.current.queue).toHaveLength(3);
    act(() => result.current.next());
    expect(result.current.track?.title).toBe("Four");
    // The track playing is not its own next.
    act(() => result.current.playNext(four));
    expect(result.current.order).toEqual([0, 2, 1]);
  });

  it("time updates do not re-render components that only use the controls", () => {
    let renders = 0;
    const { result } = renderHook(
      () => {
        renders += 1;
        return { controls: usePlayer(), progress: usePlayerProgress() };
      },
      { wrapper: ({ children }: { children: ReactNode }) => <PlayerProvider>{children}</PlayerProvider> },
    );
    act(() => result.current.controls.play(list[0]));
    const controls = result.current.controls;

    act(() => {
      Object.defineProperty(audios[0], "currentTime", { value: 5, configurable: true });
      audios[0].dispatchEvent(new Event("timeupdate"));
    });

    expect(result.current.progress.currentTime).toBe(5);
    // Same controls object: its consumers are not notified.
    expect(result.current.controls).toBe(controls);
    expect(renders).toBeGreaterThan(0);
  });
});

describe("PlayerContext — a source that does not play", () => {
  let audios: HTMLAudioElement[] = [];
  const RealAudio = window.Audio;

  beforeEach(() => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user") });
    window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
    mockToastError.mockClear();
    audios = [];
    window.Audio = class extends RealAudio {
      constructor() {
        super();
        audios.push(this);
      }
    } as typeof Audio;
  });

  const preview = (id: string, url: string) =>
    makeTrack({ provider_id: id, stream_url: null, preview_url: url, playback: { type: "preview", source: "provider" } });

  it("an expired URL recovers by itself: the track as the server describes it now is played", async () => {
    const fresh = preview("1", "http://cdn/fresh.mp3");
    const refreshTrack = vi.fn().mockResolvedValue(fresh);
    const { result } = renderPlayerWith({ refreshTrack });
    act(() => result.current.play(preview("1", "http://cdn/expired.mp3")));
    act(() => audios[0].dispatchEvent(new Event("play")));

    await act(async () => audios[0].dispatchEvent(new Event("error")));

    expect(refreshTrack).toHaveBeenCalledTimes(1);
    expect(audios[0].src).toBe("http://cdn/fresh.mp3");
    expect(result.current.track?.preview_url).toBe("http://cdn/fresh.mp3");
    expect(mockToastError).not.toHaveBeenCalled();
  });

  it("an error that survives the retry stops the player and says so", async () => {
    const refreshTrack = vi.fn().mockResolvedValue(null);
    const { result } = renderPlayerWith({ refreshTrack });
    act(() => result.current.play(preview("1", "http://cdn/expired.mp3")));
    act(() => audios[0].dispatchEvent(new Event("play")));
    expect(result.current.playing).toBe(true);

    // First failure: the track is asked again (not found: same source once more).
    await act(async () => audios[0].dispatchEvent(new Event("error")));
    expect(refreshTrack).toHaveBeenCalledTimes(1);
    expect(mockToastError).not.toHaveBeenCalled();

    // It fails again: nothing plays, and the button must not say "Pause".
    await act(async () => audios[0].dispatchEvent(new Event("error")));
    expect(result.current.playing).toBe(false);
    expect(mockToastError).toHaveBeenCalledTimes(1);
    expect(refreshTrack).toHaveBeenCalledTimes(1);
  });

  it("the same track with a new URL loads the new source instead of replaying the old one", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(preview("1", "http://cdn/old.mp3")));
    act(() => audios[0].dispatchEvent(new Event("error")));

    act(() => result.current.play(preview("1", "http://cdn/fresh.mp3")));

    expect(audios[0].src).toBe("http://cdn/fresh.mp3");
    expect(result.current.track?.preview_url).toBe("http://cdn/fresh.mp3");
  });

  it("the same track with the same URL still toggles pause", () => {
    const { result } = renderPlayer();
    const track = preview("1", "http://cdn/a.mp3");
    act(() => result.current.play(track));
    Object.defineProperty(audios[0], "paused", { value: false, configurable: true });
    act(() => result.current.play(track));
    expect(window.HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    expect(audios[0].src).toBe("http://cdn/a.mp3");
  });

  it("falls back from the full stream to the preview when the stream fails", async () => {
    const { result } = renderPlayer();
    act(() => result.current.play(makeTrack({ stream_url: "/api/stream/abc", preview_url: "http://cdn/p.mp3" })));
    expect(result.current.playbackType).toBe("full");

    await act(async () => audios[0].dispatchEvent(new Event("error")));

    expect(audios[0].src).toBe("http://cdn/p.mp3");
    expect(result.current.playbackType).toBe("preview");
    expect(mockToastError).not.toHaveBeenCalled();
  });

  it("a play() refused by the browser leaves the player paused", async () => {
    window.HTMLMediaElement.prototype.play = vi.fn().mockRejectedValue(new Error("NotAllowedError"));
    const { result } = renderPlayer();
    await act(async () => result.current.play(preview("1", "http://cdn/a.mp3")));
    expect(result.current.playing).toBe(false);
  });
});

describe("PlayerContext — shuffle, repeat, a file heard on its own", () => {
  let audios: HTMLAudioElement[] = [];
  const RealAudio = window.Audio;

  beforeEach(() => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user") });
    window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
    audios = [];
    window.Audio = class extends RealAudio {
      constructor() {
        super();
        audios.push(this);
      }
    } as typeof Audio;
  });

  const list = Array.from({ length: 6 }, (_, i) => makeTrack({ provider_id: String(i), title: `T${i}` }));
  const titles = (queue: PlayerTrack[], order: number[]) => order.map((i) => queue[i].title);

  it("shuffle keeps the track playing, plays every other one once, and next/previous follow it", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[2], list));
    act(() => result.current.toggleShuffle());

    const { order, queue } = result.current;
    expect(result.current.track?.title).toBe("T2");
    expect(order[0]).toBe(2);
    expect([...order].sort()).toEqual([0, 1, 2, 3, 4, 5]);
    expect(queue.map((t) => t.title)).toEqual(list.map((t) => t.title)); // the list itself is untouched

    act(() => result.current.next());
    expect(result.current.track?.title).toBe(titles(queue, order)[1]);
    act(() => result.current.previous());
    expect(result.current.track?.title).toBe("T2");
  });

  it("turning shuffle off goes back to the list order from the track playing", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[0], list));
    act(() => result.current.toggleShuffle());
    act(() => result.current.next());
    const playing = result.current.index;
    act(() => result.current.toggleShuffle());

    expect(result.current.order).toEqual([0, 1, 2, 3, 4, 5]);
    expect(result.current.index).toBe(playing);
    act(() => result.current.next());
    expect(result.current.index).toBe(Math.min(playing + 1, 5));
  });

  it("a list started with shuffle on starts from the track chosen", () => {
    localStorage.setItem("musizeer-play-mode", JSON.stringify({ shuffle: true, repeat: "off" }));
    const { result } = renderPlayer();
    expect(result.current.shuffle).toBe(true); // remembered on this device
    act(() => result.current.play(list[4], list));
    expect(result.current.order[0]).toBe(4);
    expect(result.current.track?.title).toBe("T4");
  });

  it("repeat all starts over after the last track; off stops there", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[5], list));
    expect(result.current.hasNext).toBe(false);
    act(() => result.current.cycleRepeat());
    expect(result.current.repeat).toBe("all");
    expect(result.current.hasNext).toBe(true);
    act(() => {
      audios[0].dispatchEvent(new Event("ended"));
    });
    expect(result.current.track?.title).toBe("T0");
  });

  it("repeat one plays the same track again when it ends, and is remembered", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[1], list));
    act(() => {
      result.current.cycleRepeat();
      result.current.cycleRepeat();
    });
    expect(result.current.repeat).toBe("one");
    expect(JSON.parse(localStorage.getItem("musizeer-play-mode") ?? "{}")).toEqual({ shuffle: false, repeat: "one" });
    act(() => {
      audios[0].currentTime = 120;
      audios[0].dispatchEvent(new Event("ended"));
    });
    expect(result.current.track?.title).toBe("T1");
    expect(audios[0].currentTime).toBe(0);
  });

  it("jumping to a queued track keeps the shuffled order", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[0], list));
    act(() => result.current.toggleShuffle());
    const order = result.current.order;
    act(() => result.current.jumpTo(order[3]));
    expect(result.current.order).toEqual(order);
    expect(result.current.index).toBe(order[3]);
  });

  it("a file heard on its own leaves the queue alone, and next goes back to it", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[3], list));
    const member = makeTrack({ provider: "", provider_id: "m1", title: "Copy", stream_url: "/api/library/duplicates/members/m1/stream" });

    act(() => result.current.playOnce({ ...member, detached: { note: "Duplicate file" } }));
    expect(result.current.interlude).toBe(true);
    expect(result.current.track?.provider_id).toBe("m1");
    expect(audios[0].src).toContain("/api/library/duplicates/members/m1/stream");
    expect(result.current.queue).toHaveLength(6);
    expect(result.current.index).toBe(3);

    act(() => result.current.next());
    expect(result.current.interlude).toBe(false);
    expect(result.current.track?.title).toBe("T3");
  });

  it("when the file heard on its own ends, the queue's track is back, paused", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[3], list));
    const play = window.HTMLMediaElement.prototype.play as ReturnType<typeof vi.fn>;
    act(() => result.current.playOnce({ ...makeTrack({ provider: "", provider_id: "m1" }), detached: { note: "x" } }));
    play.mockClear();
    act(() => {
      audios[0].dispatchEvent(new Event("ended"));
    });
    expect(result.current.interlude).toBe(false);
    expect(result.current.track?.title).toBe("T3");
    expect(play).not.toHaveBeenCalled();
  });

  it("repeat all with a single track plays it again", () => {
    const { result } = renderPlayer();
    act(() => result.current.play(list[0]));
    act(() => result.current.cycleRepeat());
    const play = window.HTMLMediaElement.prototype.play as ReturnType<typeof vi.fn>;
    play.mockClear();
    act(() => {
      audios[0].currentTime = 100;
      audios[0].dispatchEvent(new Event("ended"));
    });
    expect(audios[0].currentTime).toBe(0);
    expect(play).toHaveBeenCalled();
  });
});

describe("PlayerContext — an element routed through the audio analysis", () => {
  let audios: HTMLAudioElement[] = [];
  const RealAudio = window.Audio;

  class RunningContext {
    state = "running";
    sampleRate = 44100;
    destination = {};
    resume = async () => undefined;
    close = vi.fn(async () => undefined);
    createMediaElementSource = () => ({ connect: () => undefined });
    createAnalyser = () => ({ fftSize: 0, frequencyBinCount: 512, connect: () => undefined, getByteFrequencyData: () => undefined });
  }

  beforeEach(() => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user") });
    window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
    audios = [];
    window.Audio = class extends RealAudio {
      constructor() {
        super();
        Object.defineProperty(this, "paused", { configurable: true, get: () => false });
        audios.push(this);
      }
    } as typeof Audio;
    mockToastError.mockClear();
    resetAudioAnalysis();
    vi.stubGlobal("AudioContext", RunningContext);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function routeCurrent() {
    const tap = openTap();
    tap.read();
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    tap.close();
  }

  it("plays a source of another origin on a fresh element, at the same volume", async () => {
    const { result } = renderPlayer();
    act(() => result.current.setVolume(0.4));
    act(() => result.current.play(makeTrack({ provider_id: "1", stream_url: "/api/stream/1" })));
    await routeCurrent();
    expect(isRouted(audios[0])).toBe(true);

    act(() =>
      result.current.play(
        makeTrack({ provider_id: "2", stream_url: null, preview_url: "https://cdn.test/2.mp3", playback: { type: "preview", source: null } }),
      ),
    );
    expect(audios).toHaveLength(2);
    expect(audios[1].src).toBe("https://cdn.test/2.mp3");
    expect(audios[1].volume).toBe(0.4);
    expect(audios[0].getAttribute("src")).toBeNull();
    expect(isRouted(audios[0])).toBe(false);
    expect(result.current.track?.provider_id).toBe("2");
  });

  it("keeps the routed element for a source of this origin", async () => {
    const { result } = renderPlayer();
    act(() => result.current.play(makeTrack({ provider_id: "1", stream_url: "/api/stream/1" })));
    await routeCurrent();
    act(() => result.current.play(makeTrack({ provider_id: "2", stream_url: "/api/stream/2" })));
    expect(audios).toHaveLength(1);
    expect(audios[0].src).toBe("http://localhost:3000/api/stream/2");
  });

  it("the dropped element's last events reach nobody", async () => {
    const { result } = renderPlayer();
    act(() => result.current.play(makeTrack({ provider_id: "1", stream_url: "/api/stream/1" })));
    await routeCurrent();
    act(() =>
      result.current.play(
        makeTrack({ provider_id: "2", stream_url: null, preview_url: "https://cdn.test/2.mp3", playback: { type: "preview", source: null } }),
      ),
    );
    act(() => {
      audios[1].dispatchEvent(new Event("play"));
      audios[0].dispatchEvent(new Event("pause"));
      audios[0].dispatchEvent(new Event("error"));
    });
    expect(result.current.playing).toBe(true);
    expect(mockToastError).not.toHaveBeenCalled();
  });
});

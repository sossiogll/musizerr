import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render } from "@testing-library/react";
import { installMatchMedia } from "@/test/media";
import { BACKGROUND_KINDS, REDUCED_MOTION_QUERY } from "@/lib/backgrounds";
import BackgroundCanvas, { MAX_DPR, stepEnergy, VoiceFollower } from "./BackgroundCanvas";
import { AUDIO_VOICES, AURORA_RESPONSE, WAVES_RESPONSE, drawScene, FALLBACK_PALETTE, type Frame, type SceneAudio, type Voice } from "./scenes";
import { DECIBELS, registerPlayerAudio, resetAudioAnalysis } from "@/lib/audio-analysis";

// The canvas itself: a fake 2D context records the drawing, a manual
// requestAnimationFrame stands for the display, the timing is the test's.
function fakeContext() {
  const gradient = { addColorStop: vi.fn() };
  return {
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    roundRect: vi.fn(),
    fillText: vi.fn(),
    setTransform: vi.fn(),
    measureText: vi.fn(() => ({ width: 600 })),
    createRadialGradient: vi.fn(() => gradient),
    createLinearGradient: vi.fn(() => gradient),
    getImageData: vi.fn(() => ({ data: [10, 20, 30, 255] })),
    fillStyle: "",
    font: "",
    textBaseline: "",
  };
}

let ctx: ReturnType<typeof fakeContext>;
let media: ReturnType<typeof installMatchMedia>;
let callbacks: Map<number, FrameRequestCallback>;
let nextId: number;
let hidden = false;

/** Runs the pending animation frame callbacks at `now` (ms). */
function frame(now: number) {
  const pending = [...callbacks.entries()];
  callbacks.clear();
  pending.forEach(([, cb]) => cb(now));
}

const props = { kind: "aurora" as const, playing: true, title: null, artist: null };

beforeEach(() => {
  ctx = fakeContext();
  media = installMatchMedia();
  callbacks = new Map();
  nextId = 1;
  hidden = false;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    () => ctx as unknown as CanvasRenderingContext2D,
  );
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((cb: FrameRequestCallback) => {
      callbacks.set(nextId, cb);
      return nextId++;
    }),
  );
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((id: number) => {
      callbacks.delete(id);
    }),
  );
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 700 });
  Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: 1 });
});

afterEach(() => {
  media.restore();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function setHidden(value: boolean) {
  hidden = value;
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("BackgroundCanvas", () => {
  it("is a decoration: no pointer events, hidden from assistive technology, behind the content", () => {
    const { container } = render(<BackgroundCanvas {...props} />);
    const canvas = container.querySelector("canvas")!;
    expect(canvas).toHaveAttribute("aria-hidden", "true");
    expect(canvas).toHaveClass("pointer-events-none", "fixed", "-z-10");
    expect(canvas).toHaveAttribute("data-background", "aurora");
  });

  it("paints the first frame and keeps asking for the next ones", () => {
    render(<BackgroundCanvas {...props} />);
    expect(ctx.clearRect).not.toHaveBeenCalled();
    frame(1000);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    expect(callbacks.size).toBe(1);
    frame(1040);
    expect(ctx.clearRect).toHaveBeenCalledTimes(2);
    expect(callbacks.size).toBe(1);
  });

  it("caps the frame rate: a frame 10 ms after the last one is skipped", () => {
    render(<BackgroundCanvas {...props} />);
    frame(1000);
    frame(1010);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    frame(1040);
    expect(ctx.clearRect).toHaveBeenCalledTimes(2);
  });

  it("caps the device pixel ratio", () => {
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: 3 });
    const { container } = render(<BackgroundCanvas {...props} />);
    frame(1000);
    const canvas = container.querySelector("canvas")!;
    expect(canvas.width).toBe(1000 * MAX_DPR);
    expect(canvas.height).toBe(700 * MAX_DPR);
    expect(ctx.setTransform).toHaveBeenCalledWith(MAX_DPR, 0, 0, MAX_DPR, 0, 0);
  });

  it("stops the loop while the tab is hidden and resumes when it is shown", () => {
    render(<BackgroundCanvas {...props} />);
    frame(1000);
    expect(callbacks.size).toBe(1);
    setHidden(true);
    expect(callbacks.size).toBe(0);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    setHidden(false);
    expect(callbacks.size).toBe(1);
    frame(5000);
    expect(ctx.clearRect).toHaveBeenCalledTimes(2);
  });

  it("does not even start a loop in a tab that is hidden", () => {
    hidden = true;
    render(<BackgroundCanvas {...props} />);
    expect(callbacks.size).toBe(0);
  });

  it("with reduced motion paints one still frame and never loops", () => {
    media.set(REDUCED_MOTION_QUERY, true);
    render(<BackgroundCanvas {...props} />);
    frame(1000);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    expect(callbacks.size).toBe(0);
    frame(2000);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
  });

  it("with reduced motion repaints the still frame on resize, once", () => {
    media.set(REDUCED_MOTION_QUERY, true);
    render(<BackgroundCanvas {...props} />);
    frame(1000);
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new Event("resize"));
    expect(callbacks.size).toBe(1);
    frame(1100);
    expect(ctx.clearRect).toHaveBeenCalledTimes(2);
    expect(callbacks.size).toBe(0);
  });

  it("switches to a still frame when reduced motion turns on", () => {
    render(<BackgroundCanvas {...props} />);
    frame(1000);
    act(() => media.set(REDUCED_MOTION_QUERY, true));
    frame(1100); // the old loop was cancelled; the new effect asked for its single frame
    const before = ctx.clearRect.mock.calls.length;
    frame(1200);
    expect(ctx.clearRect.mock.calls.length).toBe(before);
    expect(callbacks.size).toBe(0);
  });

  it("cleans up on unmount: no frame pending, no listener left", () => {
    const addWindow = vi.spyOn(window, "addEventListener");
    const removeWindow = vi.spyOn(window, "removeEventListener");
    const addDocument = vi.spyOn(document, "addEventListener");
    const removeDocument = vi.spyOn(document, "removeEventListener");
    const disconnect = vi.spyOn(MutationObserver.prototype, "disconnect");
    const { unmount } = render(<BackgroundCanvas {...props} />);
    frame(1000);
    unmount();
    expect(callbacks.size).toBe(0);
    expect(addWindow.mock.calls.filter(([type]) => type === "resize")).toHaveLength(1);
    expect(removeWindow.mock.calls.filter(([type]) => type === "resize")).toHaveLength(1);
    expect(addDocument.mock.calls.filter(([type]) => type === "visibilitychange")).toHaveLength(1);
    expect(removeDocument.mock.calls.filter(([type]) => type === "visibilitychange")).toHaveLength(1);
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(media.listeners(REDUCED_MOTION_QUERY)).toBe(0);
  });

  it("a track change does not restart the loop and typography follows it", () => {
    const { rerender } = render(<BackgroundCanvas {...props} kind="typography" />);
    frame(1000);
    expect(ctx.fillText).toHaveBeenCalled();
    expect(ctx.fillText.mock.calls.every(([text]) => text === "MUSIZEER")).toBe(true);
    const before = (requestAnimationFrame as unknown as ReturnType<typeof vi.fn>).mock.calls.length;
    rerender(<BackgroundCanvas kind="typography" playing title="Blue Monday" artist="New Order" />);
    expect((requestAnimationFrame as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
    ctx.fillText.mockClear();
    frame(1040);
    const texts = new Set(ctx.fillText.mock.calls.map(([text]) => text));
    expect(texts).toEqual(new Set(["BLUE MONDAY", "NEW ORDER"]));
  });

  it("a switch of effect paints on a fresh canvas", () => {
    const { container, rerender } = render(<BackgroundCanvas {...props} />);
    const first = container.querySelector("canvas");
    rerender(<BackgroundCanvas {...props} kind="waves" />);
    const second = container.querySelector("canvas");
    expect(second).not.toBe(first);
    expect(second).toHaveAttribute("data-background", "waves");
  });

  it("draws nothing and does not throw without a 2D context", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => null);
    render(<BackgroundCanvas {...props} />);
    expect(callbacks.size).toBe(0);
  });
});

describe("scenes", () => {
  const frameOf = (over: Partial<Frame> = {}): Frame => ({
    width: 1000,
    height: 700,
    time: 3,
    energy: 0.7,
    palette: FALLBACK_PALETTE,
    title: null,
    artist: null,
    appName: "Musizeer",
    audio: null,
    ...over,
  });

  it("there are exactly four effects", () => {
    expect(BACKGROUND_KINDS).toEqual(["aurora", "waves", "spectrum", "typography"]);
  });

  it.each(BACKGROUND_KINDS)("%s clears the canvas and paints something", (kind) => {
    drawScene(kind, ctx as unknown as CanvasRenderingContext2D, frameOf());
    expect(ctx.clearRect).toHaveBeenCalledWith(0, 0, 1000, 700);
    const painted = ctx.fillRect.mock.calls.length + ctx.fill.mock.calls.length + ctx.fillText.mock.calls.length;
    expect(painted).toBeGreaterThan(0);
  });

  it("spectrum draws more and taller bars while playing than idle", () => {
    const heights = (energy: number) => {
      const c = fakeContext();
      drawScene("spectrum", c as unknown as CanvasRenderingContext2D, frameOf({ energy }));
      return c.roundRect.mock.calls.map(([, , , bar]) => bar as number);
    };
    const idle = heights(0.25);
    const playing = heights(1);
    expect(playing).toHaveLength(idle.length);
    expect(playing.reduce((a, b) => a + b, 0)).toBeGreaterThan(idle.reduce((a, b) => a + b, 0));
  });
});

describe("following the playback", () => {
  /** The energy after `seconds` of 30 fps frames towards `target`. */
  const after = (from: number, target: number, seconds: number) => {
    let energy = from;
    for (let t = 0; t < seconds; t += 1 / 30) energy = stepEnergy(energy, target, 1 / 30);
    return energy;
  };

  it("rises to full within a fifth of a second of play", () => {
    expect(after(0, 1, 0.2)).toBeGreaterThan(0.95);
  });

  it("falls to nothing within half a second of pause, and settles exactly at rest", () => {
    expect(after(1, 0, 0.35)).toBeLessThan(0.05);
    expect(after(1, 0, 1)).toBe(0);
  });

  const barHeights = (energy: number, time = 3) => {
    const c = fakeContext();
    drawScene("spectrum", c as unknown as CanvasRenderingContext2D, {
      width: 1000,
      height: 700,
      time,
      energy,
      palette: FALLBACK_PALETTE,
      title: null,
      artist: null,
      appName: "Musizeer",
      audio: null,
    });
    return c.roundRect.mock.calls.map(([, , , bar]) => bar as number);
  };

  it("with nothing playing the spectrum bars lie flat, whatever the time", () => {
    const flat = barHeights(0);
    // The shortest bar (4 px) plus the 6 px tucked under the bottom edge.
    expect(new Set(flat)).toEqual(new Set([10]));
    expect(barHeights(0, 17)).toEqual(flat);
  });

  it.each(BACKGROUND_KINDS)("%s stops repainting once at rest, and wakes on play", (kind) => {
    const { rerender } = render(<BackgroundCanvas kind={kind} playing title={null} artist={null} />);
    frame(1000);
    frame(1040);
    rerender(<BackgroundCanvas kind={kind} playing={false} title={null} artist={null} />);
    // Falling: still painting every frame.
    for (let now = 1080; now <= 2080; now += 40) frame(now);
    const painted = ctx.clearRect.mock.calls.length;
    // At rest: the loop keeps watching but paints nothing.
    frame(2120);
    frame(2160);
    expect(ctx.clearRect).toHaveBeenCalledTimes(painted);
    expect(callbacks.size).toBe(1);
    rerender(<BackgroundCanvas kind={kind} playing title={null} artist={null} />);
    frame(2200);
    expect(ctx.clearRect).toHaveBeenCalledTimes(painted + 1);
  });

});

describe("a scene at rest", () => {
  it("opened with nothing playing, paints one frame and then waits", () => {
    render(<BackgroundCanvas kind="waves" playing={false} title={null} artist={null} />);
    frame(1000);
    frame(1040);
    frame(1080);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    expect(callbacks.size).toBe(1);
  });

  it("repaints once on resize, still at rest", () => {
    render(<BackgroundCanvas kind="aurora" playing={false} title={null} artist={null} />);
    frame(1000);
    window.dispatchEvent(new Event("resize"));
    frame(1040);
    frame(1080);
    expect(ctx.clearRect).toHaveBeenCalledTimes(2);
  });
});

describe("a track change at rest", () => {
  it("repaints the typography once with the new title", () => {
    const { rerender } = render(<BackgroundCanvas kind="typography" playing={false} title="A" artist="X" />);
    frame(1000);
    frame(1040);
    expect(ctx.clearRect).toHaveBeenCalledTimes(1);
    rerender(<BackgroundCanvas kind="typography" playing={false} title="B" artist="Y" />);
    frame(1080);
    frame(1120);
    expect(ctx.clearRect).toHaveBeenCalledTimes(2);
    expect(ctx.fillText.mock.calls.some(([text]) => text === "B")).toBe(true);
  });
});

describe("following the sound", () => {
  const frameWith = (audio: SceneAudio | null, over: Partial<Frame> = {}): Frame => ({
    width: 1000,
    height: 700,
    time: 3,
    energy: 1,
    palette: FALLBACK_PALETTE,
    title: null,
    artist: null,
    appName: "Musizeer",
    audio,
    ...over,
  });
  const voices = (count: number, voice: (i: number) => Voice = () => ({ level: 0.5, pulse: 0, travel: 0 })) =>
    Array.from({ length: count }, (_, i) => voice(i));
  const sound = (over: Partial<SceneAudio> = {}): SceneAudio => ({
    bands: (count) => Array.from({ length: count }, () => 0.5),
    voices: voices(5),
    ...over,
  });

  it("the spectrum's bars are the bands of the audio: loud lows, silent highs", () => {
    const c = fakeContext();
    const audio = sound({ bands: (count) => Array.from({ length: count }, (_, i) => (i < count / 2 ? 0.9 : 0)) });
    drawScene("spectrum", c as unknown as CanvasRenderingContext2D, frameWith(audio));
    const bars = c.roundRect.mock.calls.map(([, , , bar]) => bar as number);
    expect(bars[0]).toBeGreaterThan(300);
    expect(bars[bars.length - 1]).toBe(10);
  });

  it("the same audio draws the same spectrum, whatever the time", () => {
    const heights = (time: number) => {
      const c = fakeContext();
      drawScene("spectrum", c as unknown as CanvasRenderingContext2D, frameWith(sound(), { time }));
      return c.roundRect.mock.calls.map(([, , , bar]) => bar as number);
    };
    expect(heights(17)).toEqual(heights(3));
  });

  it("each wave follows its own band: only the loud one rises", () => {
    /** How far each layer swings up and down (its lineTo ys, layer by layer). */
    const swings = (audio: SceneAudio) => {
      const c = fakeContext();
      drawScene("waves", c as unknown as CanvasRenderingContext2D, frameWith(audio));
      const perLayer: number[][] = [];
      c.moveTo.mock.calls.forEach(() => perLayer.push([]));
      let layer = -1;
      c.lineTo.mock.calls.forEach(([x, y]) => {
        if (x === 0) layer += 1;
        if ((y as number) < 700) perLayer[layer]?.push(y as number);
      });
      return perLayer.map((ys) => Math.max(...ys) - Math.min(...ys));
    };
    const quiet = swings(sound({ voices: voices(5, () => ({ level: 0.1, pulse: 0, travel: 0 })) }));
    const loudBass = swings(sound({ voices: voices(5, (i) => ({ level: i === 0 ? 1 : 0.1, pulse: 0, travel: 0 })) }));
    expect(loudBass[0]).toBeGreaterThan(quiet[0] * 2);
    [1, 2, 3, 4].forEach((i) => expect(loudBass[i]).toBeCloseTo(quiet[i], 5));
    // The same rise in the highs barely moves the nearest wave.
    const loudHighs = swings(sound({ voices: voices(5, (i) => ({ level: i === 4 ? 1 : 0.1, pulse: 0, travel: 0 })) }));
    expect(loudHighs[4] / quiet[4]).toBeLessThan(1.25);
    expect(loudHighs[4]).toBeGreaterThan(quiet[4]);
  });

  it("each aurora cloud follows its own band and flashes on its own hits", () => {
    /** The centre alpha of each cloud, in drawing order. */
    const alphas = (audio: SceneAudio) => {
      const stops: number[] = [];
      const c = {
        ...fakeContext(),
        createRadialGradient: vi.fn(() => ({
          addColorStop: (offset: number, css: string) => {
            if (offset === 0) stops.push(Number(/,([\d.]+)\)$/.exec(css)![1]));
          },
        })),
      };
      drawScene("aurora", c as unknown as CanvasRenderingContext2D, frameWith(audio));
      return stops;
    };
    const quiet = alphas(sound({ voices: voices(4, () => ({ level: 0.1, pulse: 0, travel: 0 })) }));
    const highs = alphas(sound({ voices: voices(4, (i) => ({ level: i === 3 ? 1 : 0.1, pulse: 0, travel: 0 })) }));
    const bassHit = alphas(sound({ voices: voices(4, (i) => ({ level: 0.1, pulse: i === 0 ? 1 : 0, travel: 0 })) }));
    expect(highs[3]).toBeGreaterThan(quiet[3]);
    expect(highs.slice(0, 3)).toEqual(quiet.slice(0, 3));
    expect(bassHit[0]).toBeGreaterThan(quiet[0]);
    expect(bassHit.slice(1)).toEqual(quiet.slice(1));
    // The same hit in the highs flashes far less than in the bass.
    const highsHit = alphas(sound({ voices: voices(4, (i) => ({ level: 0.1, pulse: i === 3 ? 1 : 0, travel: 0 })) }));
    expect(highsHit[3] - quiet[3]).toBeLessThan((bassHit[0] - quiet[0]) / 4);
  });

  it("one band per cloud and per wave", () => {
    expect(AUDIO_VOICES).toEqual({ aurora: 4, waves: 5, spectrum: 0, typography: 0 });
    // The bass leads, every band above it answers less.
    for (const response of [AURORA_RESPONSE, WAVES_RESPONSE]) {
      expect(response[0]).toBe(1);
      response.slice(1).forEach((r, i) => expect(r).toBeLessThan(response[i]));
    }
    expect(AURORA_RESPONSE).toHaveLength(AUDIO_VOICES.aurora);
    expect(WAVES_RESPONSE).toHaveLength(AUDIO_VOICES.waves);
  });

  it("a voice is its band against the band's own recent peak: quiet highs move as much as loud bass", () => {
    const bass = new VoiceFollower();
    const highs = new VoiceFollower();
    let loud: Voice = { level: 0, pulse: 0, travel: 0 };
    let soft: Voice = { level: 0, pulse: 0, travel: 0 };
    for (let i = 0; i < 30; i++) {
      loud = bass.step(i % 2 ? 0.8 : 0.4, 1 / 30);
      soft = highs.step(i % 2 ? 0.3 : 0.15, 1 / 30);
    }
    expect(loud.level).toBeCloseTo(soft.level, 1);
    expect(soft.level).toBeGreaterThan(0.9);
  });

  it("near silence stays near silence, never stretched to full", () => {
    const voice = new VoiceFollower();
    let level = 1;
    for (let i = 0; i < 60; i++) level = voice.step(0.03, 1 / 30).level;
    expect(level).toBeLessThan(0.25);
  });

  it("a hit is a band rising above its recent average, and it fades", () => {
    const voice = new VoiceFollower();
    let pulse = 0;
    for (let i = 0; i < 60; i++) pulse = voice.step(0.4, 1 / 30).pulse;
    expect(pulse).toBeLessThan(0.05);
    expect(voice.step(0.8, 1 / 30).pulse).toBeGreaterThan(0.9);
    for (let i = 0; i < 12; i++) pulse = voice.step(0.4, 1 / 30).pulse;
    expect(pulse).toBeLessThan(0.1);
    expect(voice.step(null, 1 / 30).pulse).toBeLessThan(pulse);
  });

  it("a slow, dense mix (every band at -40 dB) leaves the bars well below the top", () => {
    // The same band on the browser default scale (-100..-30 dB) read 0.86.
    const band = (-40 - DECIBELS[0]) / (DECIBELS[1] - DECIBELS[0]);
    const c = fakeContext();
    drawScene("spectrum", c as unknown as CanvasRenderingContext2D, frameWith(sound({ bands: (n) => Array(n).fill(band) })));
    const bars = c.roundRect.mock.calls.map(([, , , bar]) => (bar as number) - 6);
    expect(bars[0]).toBeLessThan(700 * 0.55 * 0.5);
  });


  it("the typography never asks for the audio; the spectrum does", () => {
    const made = vi.fn();
    vi.stubGlobal(
      "AudioContext",
      class {
        constructor() {
          made();
        }
        state = "suspended";
        resume = () => new Promise(() => undefined);
        close = async () => undefined;
      },
    );
    const audio = document.createElement("audio");
    audio.src = "/api/stream/abc";
    Object.defineProperty(audio, "paused", { configurable: true, get: () => false });
    resetAudioAnalysis();
    registerPlayerAudio(audio);

    const { unmount } = render(<BackgroundCanvas kind="typography" playing title="A" artist="B" />);
    frame(1000);
    frame(1040);
    expect(made).not.toHaveBeenCalled();
    unmount();

    render(<BackgroundCanvas kind="spectrum" playing title={null} artist={null} />);
    frame(2000);
    frame(2040);
    expect(made).toHaveBeenCalledTimes(1);
    resetAudioAnalysis();
  });
});

describe("with the audio, nothing moves by the clock", () => {
  const frameAt = (time: number, audio: SceneAudio | null): Frame => ({
    width: 1000,
    height: 700,
    time,
    energy: 1,
    palette: FALLBACK_PALETTE,
    title: null,
    artist: null,
    appName: "Musizeer",
    audio,
  });
  const steady = (count: number): SceneAudio => ({
    bands: (n) => Array(n).fill(0.4),
    voices: Array.from({ length: count }, () => ({ level: 0.6, pulse: 0, travel: 1.5 })),
  });
  /** Everything a scene drew, as one comparable list. */
  const drawing = (kind: "aurora" | "waves", frame: Frame) => {
    const c = fakeContext();
    drawScene(kind, c as unknown as CanvasRenderingContext2D, frame);
    return [...c.lineTo.mock.calls, ...c.createRadialGradient.mock.calls];
  };

  it.each([
    ["aurora", 4],
    ["waves", 5],
  ] as const)("%s draws the same for the same sound, whatever the time", (kind, count) => {
    expect(drawing(kind, frameAt(17, steady(count)))).toEqual(drawing(kind, frameAt(3, steady(count))));
  });

  it.each(["aurora", "waves"] as const)("%s without the audio still drifts with the time", (kind) => {
    expect(drawing(kind, frameAt(17, null))).not.toEqual(drawing(kind, frameAt(3, null)));
  });

  it("a band carries its source only while it sounds", () => {
    const voice = new VoiceFollower();
    for (let i = 0; i < 30; i++) voice.step(0.5, 1 / 30);
    const moved = voice.step(0.5, 1 / 30).travel;
    expect(moved).toBeGreaterThan(1.5);
    let still = moved;
    for (let i = 0; i < 30; i++) still = voice.step(0, 1 / 30).travel;
    expect(still).toBeCloseTo(moved, 6);
    expect(voice.step(null, 1 / 30).travel).toBe(still);
  });
});

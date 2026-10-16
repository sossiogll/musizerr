import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  DECIBELS,
  isRouted,
  isSameOrigin,
  needsFreshElement,
  openTap,
  registerPlayerAudio,
  releaseAnalysis,
  resetAudioAnalysis,
} from "./audio-analysis";

// A Web Audio stand-in: the analyser answers every bin at `loudness`, the
// context starts suspended and wakes (or not) when asked.
let loudness = 0;
let wakes = true;
const contexts: FakeContext[] = [];

class FakeContext {
  state: AudioContextState = "suspended";
  sampleRate = 44100;
  destination = {};
  routed: HTMLMediaElement[] = [];
  constructor() {
    contexts.push(this);
  }
  resume = vi.fn(async () => {
    if (wakes) this.state = "running";
  });
  close = vi.fn(async () => {
    this.state = "closed";
  });
  createMediaElementSource = vi.fn((media: HTMLMediaElement) => {
    this.routed.push(media);
    return { connect: vi.fn() };
  });
  analysers: Record<string, number>[] = [];
  createAnalyser = vi.fn(() => {
    const analyser = {
    fftSize: 2048,
    minDecibels: -100,
    maxDecibels: -30,
    smoothingTimeConstant: 0.8,
    frequencyBinCount: 512,
    connect: vi.fn(),
    getByteFrequencyData: (bins: Uint8Array) => bins.fill(Math.round(loudness * 255)),
    };
    this.analysers.push(analyser as unknown as Record<string, number>);
    return analyser;
  });
}

function audioPlaying(src: string, paused = false) {
  const audio = document.createElement("audio");
  audio.src = src;
  Object.defineProperty(audio, "paused", { configurable: true, get: () => paused });
  return audio;
}

/** Lets the context's resume settle. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  loudness = 0;
  wakes = true;
  contexts.length = 0;
  resetAudioAnalysis();
  vi.stubGlobal("AudioContext", FakeContext);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isSameOrigin", () => {
  it("a path or a URL of this page's origin is; another host is not", () => {
    expect(isSameOrigin("/api/stream/abc")).toBe(true);
    expect(isSameOrigin(`${window.location.origin}/api/preview/p/1`)).toBe(true);
    expect(isSameOrigin("https://cdn.example.test/mp3-preview/1")).toBe(false);
  });
});

describe("routing the player's audio through the analyser", () => {
  it("routes a playing element of this origin once a tap reads and the context runs", async () => {
    const audio = audioPlaying("/api/stream/abc");
    registerPlayerAudio(audio);
    const tap = openTap();
    expect(tap.read()).toBeNull();
    await settle();
    expect(isRouted(audio)).toBe(true);
    expect(contexts).toHaveLength(1);
    expect(contexts[0].routed).toEqual([audio]);

    loudness = 0.6;
    const reading = tap.read()!;
    expect(reading.bands(32)).toHaveLength(32);
    expect(reading.bands(32).every((band) => Math.abs(band - 0.6) < 0.01)).toBe(true);
    releaseAnalysis(audio);
  });

  it("never routes a source of another origin", async () => {
    const audio = audioPlaying("https://cdn.example.test/mp3-preview/1");
    registerPlayerAudio(audio);
    const tap = openTap();
    tap.read();
    await settle();
    expect(contexts).toHaveLength(0);
    expect(isRouted(audio)).toBe(false);
  });

  it("never routes an element that is not playing", async () => {
    registerPlayerAudio(audioPlaying("/api/stream/abc", true));
    openTap().read();
    await settle();
    expect(contexts).toHaveLength(0);
  });

  it("leaves the element alone when the context stays asleep", async () => {
    wakes = false;
    const audio = audioPlaying("/api/stream/abc");
    registerPlayerAudio(audio);
    openTap().read();
    await settle();
    expect(isRouted(audio)).toBe(false);
    expect(contexts[0].createMediaElementSource).not.toHaveBeenCalled();
    expect(contexts[0].close).toHaveBeenCalled();
  });

  it("routes nothing when the tap closed before the context woke", async () => {
    const audio = audioPlaying("/api/stream/abc");
    registerPlayerAudio(audio);
    const tap = openTap();
    tap.read();
    tap.close();
    await settle();
    expect(isRouted(audio)).toBe(false);
  });

  it("reads nothing while the routed element is paused", async () => {
    let paused = false;
    const audio = document.createElement("audio");
    audio.src = "/api/stream/abc";
    Object.defineProperty(audio, "paused", { configurable: true, get: () => paused });
    registerPlayerAudio(audio);
    const tap = openTap();
    tap.read();
    await settle();
    paused = true;
    expect(tap.read()).toBeNull();
    releaseAnalysis(audio);
  });

  it("a routed element needs a fresh one for another origin, never for its own", async () => {
    const audio = audioPlaying("/api/stream/abc");
    expect(needsFreshElement(audio, "https://cdn.example.test/1")).toBe(false);
    registerPlayerAudio(audio);
    openTap().read();
    await settle();
    expect(needsFreshElement(audio, "https://cdn.example.test/1")).toBe(true);
    expect(needsFreshElement(audio, "/api/preview/p/1")).toBe(false);
    releaseAnalysis(audio);
  });

  it("releasing an element closes its context", async () => {
    const audio = audioPlaying("/api/stream/abc");
    registerPlayerAudio(audio);
    openTap().read();
    await settle();
    releaseAnalysis(audio);
    expect(isRouted(audio)).toBe(false);
    expect(contexts[0].close).toHaveBeenCalled();
  });
});

describe("the loudness scale", () => {
  it("leaves headroom above the browser default, so a dense mix does not fill every band", async () => {
    const audio = audioPlaying("/api/stream/abc");
    registerPlayerAudio(audio);
    openTap().read();
    await settle();
    expect(DECIBELS).toEqual([-90, -12]);
    expect(contexts[0].analysers[0]).toMatchObject({ minDecibels: -90, maxDecibels: -12 });
    releaseAnalysis(audio);
  });
});

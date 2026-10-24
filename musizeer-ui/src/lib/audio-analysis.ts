/**
 * What the player is playing, as numbers a visual can follow: its spectrum,
 * in as many bands as the visual wants.
 *
 * Analysing the audio means routing the player's audio element through a Web
 * Audio graph (`createMediaElementSource`), and that cannot be undone: from
 * then on the element is heard only through the graph. For a source of
 * another origin without CORS headers the graph hears silence, and so does
 * the listener. So the element is routed only when all of these hold:
 *
 * - someone asked for the analysis (an animated background is on: `openTap`);
 * - the element plays a source of this page's own origin (every source the
 *   server gives is one: streams, sync copies, previews through /preview);
 * - an AudioContext is running (the listener already pressed play).
 *
 * And the player, before loading a source of another origin into an element
 * already routed, takes a fresh element (`needsFreshElement`): the analysis
 * stops for that track, the audio never does.
 */

/** The audio element the player plays through, as the player registers it. */
let element: HTMLAudioElement | null = null;
let taps = 0;
let connecting = false;

interface Graph {
  context: AudioContext;
  analyser: AnalyserNode;
  bins: Uint8Array<ArrayBuffer>;
}

const graphs = new WeakMap<HTMLAudioElement, Graph>();

/** Bins of the analyser (half the FFT size): 512 bins of about 43 Hz at 44.1 kHz. */
export const FFT_SIZE = 1024;
/** The lowest and highest frequency the spectrum shows, in Hz. */
const SPECTRUM_HZ: readonly [number, number] = [40, 16000];
/**
 * The loudness range a band spans, in dBFS: below the floor it is empty, at
 * the ceiling full. The ceiling sits well above the browser default (-30), so
 * a dense, sustained mix (a slow sung song) does not fill every band.
 */
export const DECIBELS: readonly [number, number] = [-90, -12];

/** A URL of this page's own origin (a relative one is). */
export function isSameOrigin(src: string): boolean {
  try {
    return new URL(src, window.location.href).origin === window.location.origin;
  } catch {
    return false;
  }
}

/** The player's audio element, each time it makes one. */
export function registerPlayerAudio(audio: HTMLAudioElement | null) {
  element = audio;
}

/** Whether the element already goes through an analysis graph (and so can only play this origin). */
export function isRouted(audio: HTMLAudioElement): boolean {
  return graphs.has(audio);
}

/** The player must load `src` into a new element: this one is routed and `src` is of another origin. */
export function needsFreshElement(audio: HTMLAudioElement, src: string): boolean {
  return isRouted(audio) && !isSameOrigin(src);
}

/**
 * The element starts playing: a routed one is heard only through its graph,
 * so its context must be awake (a browser may suspend it while idle).
 */
export function wakeAnalysis(audio: HTMLAudioElement) {
  const graph = graphs.get(audio);
  if (graph && graph.context.state !== "running") void graph.context.resume().catch(() => undefined);
}

/** The player drops `audio`: its graph goes with it. */
export function releaseAnalysis(audio: HTMLAudioElement) {
  const graph = graphs.get(audio);
  if (!graph) return;
  graphs.delete(audio);
  void graph.context.close().catch(() => undefined);
}

type AudioContextClass = typeof AudioContext;

function audioContextClass(): AudioContextClass | null {
  const w = window as unknown as { AudioContext?: AudioContextClass; webkitAudioContext?: AudioContextClass };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

/**
 * Routes the playing element through an analyser, once every condition of
 * the module comment holds. Asynchronous: the context must be running before
 * the element is routed, or the listener would hear nothing.
 */
function connect(audio: HTMLAudioElement) {
  const Context = audioContextClass();
  const src = audio.currentSrc || audio.src;
  if (connecting || !Context || audio.paused || !src || !isSameOrigin(src)) return;
  connecting = true;
  let context: AudioContext;
  try {
    context = new Context();
  } catch {
    connecting = false;
    return;
  }
  void context
    .resume()
    .catch(() => undefined)
    .then(() => {
      connecting = false;
      // Things moved meanwhile, or the browser keeps the context asleep:
      // leave the element alone, it keeps playing as it was.
      const current = audio.currentSrc || audio.src;
      if (context.state !== "running" || element !== audio || !taps || graphs.has(audio) || !isSameOrigin(current)) {
        void context.close().catch(() => undefined);
        return;
      }
      try {
        const source = context.createMediaElementSource(audio);
        const analyser = context.createAnalyser();
        analyser.fftSize = FFT_SIZE;
        analyser.smoothingTimeConstant = 0.72;
        [analyser.minDecibels, analyser.maxDecibels] = DECIBELS;
        source.connect(analyser);
        analyser.connect(context.destination);
        graphs.set(audio, { context, analyser, bins: new Uint8Array(analyser.frequencyBinCount) });
      } catch {
        void context.close().catch(() => undefined);
      }
    });
}

/** One reading of the audio, every value 0..1. */
export interface AudioReading {
  /** `count` bands from low to high, spaced as the ear hears them (log scale). */
  bands(count: number): number[];
}

function average(bins: Uint8Array, from: number, to: number): number {
  let sum = 0;
  const last = Math.max(from + 1, to);
  for (let i = from; i < last; i++) sum += bins[i] ?? 0;
  return sum / (last - from) / 255;
}

function read(graph: Graph): AudioReading {
  const { analyser, bins, context } = graph;
  analyser.getByteFrequencyData(bins);
  const hzPerBin = context.sampleRate / analyser.fftSize;
  const bin = (hz: number) => Math.min(bins.length - 1, Math.max(0, Math.round(hz / hzPerBin)));
  return {
    bands(count) {
      const [low, high] = SPECTRUM_HZ;
      const ratio = high / low;
      const out: number[] = [];
      for (let i = 0; i < count; i++) {
        const from = bin(low * ratio ** (i / count));
        const to = Math.max(from + 1, bin(low * ratio ** ((i + 1) / count)));
        out.push(average(bins, from, to));
      }
      return out;
    },
  };
}

export interface AudioTap {
  /** The audio now, or null when it cannot be analysed (nothing plays, another origin, no Web Audio yet). */
  read(): AudioReading | null;
  close(): void;
}

/**
 * Asks for the analysis while the tap is open. Reading is cheap and safe to
 * do every frame; the first reads of a playing element route it.
 */
export function openTap(): AudioTap {
  taps += 1;
  let open = true;
  return {
    read() {
      const audio = element;
      if (!open || !audio) return null;
      const graph = graphs.get(audio);
      if (!graph) {
        connect(audio);
        return null;
      }
      if (graph.context.state !== "running") {
        void graph.context.resume().catch(() => undefined);
        return null;
      }
      return audio.paused ? null : read(graph);
    },
    close() {
      if (!open) return;
      open = false;
      taps -= 1;
    },
  };
}

/** For tests: forget the registered element and the open taps. */
export function resetAudioAnalysis() {
  element = null;
  taps = 0;
  connecting = false;
}

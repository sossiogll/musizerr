import type { BackgroundKind } from "@/lib/backgrounds";

/** An sRGB colour, 0-255 per channel. */
export type Rgb = readonly [number, number, number];

/** The theme's colours as the canvas needs them, resolved from the CSS variables (light or dark). */
export interface Palette {
  primary: Rgb;
  secondary: Rgb;
  tertiary: Rgb;
  foreground: Rgb;
}

/** What a scene draws from, once per frame. */
export interface Frame {
  width: number;
  height: number;
  /** Seconds of animation (advances only while a track plays; frozen for a static frame). */
  time: number;
  /** 0 nothing playing .. 1 playing: how lively the scene is. */
  energy: number;
  palette: Palette;
  /** The current track, for the typography scene; null when idle. */
  title: string | null;
  artist: string | null;
  /** The app name, shown when nothing plays. */
  appName: string;
  /**
   * The audio playing, when it can be heard (lib/audio-analysis); null while
   * it cannot: the scenes then move on their own, from `energy` and `time`.
   */
  audio: SceneAudio | null;
}

/** One band of the audio as a scene source follows it, every value 0..1. */
export interface Voice {
  /** The band against its own recent peak: a quiet band (the highs) moves as much as a loud one. */
  level: number;
  /** A hit: jumps when the band rises above its recent average, then fades. */
  pulse: number;
  /**
   * How far the band has carried its source, in seconds of full sound: it
   * grows only while the band sounds, so a source following the audio moves
   * with the music and stands still in its silences.
   */
  travel: number;
}

/** The audio as the scenes read it. */
export interface SceneAudio {
  /** `count` bands, low to high, each 0..1 on one loudness scale (lib/audio-analysis). */
  bands(count: number): number[];
  /** One voice per source of the scene (AUDIO_VOICES), from the lowest band to the highest. */
  voices: Voice[];
}

const rgba = ([r, g, b]: Rgb, a: number) => `rgba(${r},${g},${b},${a})`;

export const FALLBACK_PALETTE: Palette = {
  primary: [124, 58, 237],
  secondary: [59, 130, 246],
  tertiary: [192, 38, 211],
  foreground: [80, 80, 100],
};

/**
 * Resolves the theme variables to RGB through a 1x1 canvas, so any CSS colour
 * syntax the stylesheet uses (hsl, oklch...) works and no parsing is ours.
 * Falls back to a fixed palette where a canvas is not available.
 */
export function readPalette(root: HTMLElement = document.documentElement): Palette {
  const probe = document.createElement("canvas");
  probe.width = probe.height = 1;
  const ctx = probe.getContext("2d", { willReadFrequently: true });
  if (!ctx) return FALLBACK_PALETTE;
  const style = getComputedStyle(root);
  const resolve = (variable: string, fallback: Rgb): Rgb => {
    const css = style.getPropertyValue(variable).trim();
    if (!css) return fallback;
    try {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "#000";
      ctx.fillStyle = css; // an invalid colour leaves the previous one: black, then fallback
      ctx.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
      return a === 0 ? fallback : [r, g, b];
    } catch {
      return fallback;
    }
  };
  return {
    primary: resolve("--color-primary", FALLBACK_PALETTE.primary),
    secondary: resolve("--color-info", FALLBACK_PALETTE.secondary),
    tertiary: resolve("--color-accent-foreground", FALLBACK_PALETTE.tertiary),
    foreground: resolve("--color-foreground", FALLBACK_PALETTE.foreground),
  };
}

/**
 * How much each source answers its band, from the bass up: the bass leads in
 * full, the other bands only colour the scene, so a song that is not dance
 * music does not set off fireworks. 0 = still at its resting value, 1 = full.
 */
export const AURORA_RESPONSE: readonly number[] = [1, 0.35, 0.2, 0.12];
export const WAVES_RESPONSE: readonly number[] = [1, 0.5, 0.3, 0.2, 0.12];

/** Between a resting value and the full answer, by the source's response. */
const answer = (response: number, rest: number, full: number) => rest + response * (full - rest);

/**
 * The clock a source moves by: with the audio, the distance its band carried
 * it (even the quietest answering band keeps a little motion of its own);
 * without, the animation's time.
 */
const clockOf = (voice: Voice | undefined, response: number, time: number) =>
  voice ? voice.travel * Math.max(response, 0.35) : time;

/**
 * Aurora: soft colour clouds drifting slowly. With the audio each cloud
 * follows its own band (bass, low mids, voice, highs), as much as its
 * AURORA_RESPONSE: it glows with it, swells on its hits and drifts only as
 * far as the band carries it. Without it they drift on their own and
 * brighten while a track plays.
 */
function aurora(ctx: CanvasRenderingContext2D, f: Frame) {
  const { width: w, height: h, time: t, energy, palette: p, audio } = f;
  const reach = Math.max(w, h) * 0.5;
  const blobs: { color: Rgb; sx: number; sy: number; px: number; py: number; alpha: number }[] = [
    { color: p.primary, sx: 0.11, sy: 0.08, px: 0, py: 1.3, alpha: 0.55 },
    { color: p.secondary, sx: 0.07, sy: 0.12, px: 2.1, py: 0.4, alpha: 0.45 },
    { color: p.tertiary, sx: 0.09, sy: 0.06, px: 4.2, py: 2.7, alpha: 0.5 },
    { color: p.primary, sx: 0.05, sy: 0.1, px: 5.5, py: 3.9, alpha: 0.35 },
  ];
  blobs.forEach((b, i) => {
    const voice = audio?.voices[i];
    const response = AURORA_RESPONSE[i] ?? 0;
    const clock = clockOf(voice, response, t);
    const x = w * (0.5 + 0.4 * Math.sin(clock * b.sx * 2 + b.px));
    const y = h * (0.5 + 0.4 * Math.cos(clock * b.sy * 2 + b.py));
    const glow = voice ? answer(response, 0.65, 0.3 + 0.7 * voice.level) : 0.6 + 0.4 * energy;
    const hit = voice ? response * voice.pulse : 0;
    const radius = reach * (1 + 0.25 * hit);
    const g = ctx.createRadialGradient(x, y, 0, x, y, radius);
    g.addColorStop(0, rgba(b.color, Math.min(0.85, b.alpha * glow + 0.3 * hit)));
    g.addColorStop(1, rgba(b.color, 0));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);
  });
}

/**
 * Waves: layered sine bands flowing sideways, the nearest layer the
 * strongest. With the audio each layer follows its own band, the farthest
 * the bass and the nearest the highs, as much as its WAVES_RESPONSE, and
 * kicks up on its hits; it flows only while its band sounds. Without it
 * the water just calms down while nothing plays.
 */
function waves(ctx: CanvasRenderingContext2D, f: Frame) {
  const { width: w, height: h, time: t, energy, palette: p, audio } = f;
  const layers = 5;
  const colors = [p.secondary, p.primary, p.tertiary, p.primary, p.secondary];
  for (let i = 0; i < layers; i++) {
    const base = h * (0.5 + 0.1 * i);
    const voice = audio?.voices[i];
    const response = WAVES_RESPONSE[i] ?? 0;
    const clock = clockOf(voice, response, t);
    const drive = voice
      ? answer(response, 0.75, 0.3 + 1.2 * voice.level + 0.6 * voice.pulse)
      : 0.5 + 0.5 * energy;
    const amp = h * (0.03 + 0.012 * i) * drive;
    const freq = (2 * Math.PI) / (w * (0.55 - 0.06 * i));
    const speed = (0.25 + 0.09 * i) * (i % 2 ? -1 : 1);
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let x = 0; x <= w + 10; x += 10) {
      const y =
        base + amp * Math.sin(x * freq + clock * speed * 2 + i) + amp * 0.5 * Math.sin(x * freq * 2.3 - clock * speed * 1.4);
      ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = rgba(colors[i], 0.1 + 0.05 * i);
    ctx.fill();
  }
}

/**
 * Spectrum: one bar per band of the audio playing, low frequencies on the
 * left. Without the audio (a source that cannot be analysed) the bars move on
 * their own while a track plays. Either way they lie flat when it stops.
 */
function spectrum(ctx: CanvasRenderingContext2D, f: Frame) {
  const { width: w, height: h, time: t, energy, palette: p, audio } = f;
  const count = Math.max(24, Math.min(72, Math.floor(w / 26)));
  const slot = w / count;
  const barWidth = slot * 0.56;
  const maxHeight = h * 0.55;
  const gradient = ctx.createLinearGradient(0, h, 0, h - maxHeight);
  gradient.addColorStop(0, rgba(p.primary, 0.55));
  gradient.addColorStop(1, rgba(p.secondary, 0.15));
  ctx.fillStyle = gradient;
  const bands = audio?.bands(count);
  for (let i = 0; i < count; i++) {
    const n = i / (count - 1);
    let level: number;
    if (bands) {
      // Music carries less in the highs: lift them a little so the whole width
      // moves, and bend the curve so only the loudest bands reach the top.
      level = Math.min(1, bands[i] * (1 + 0.5 * n)) ** 1.6;
    } else {
      // Low frequencies on the left carry more energy, like music does.
      const tilt = 1 - 0.55 * n;
      const wobble =
        0.5 +
        0.28 * Math.sin(t * 2.1 + i * 0.55) +
        0.14 * Math.sin(t * 3.7 - i * 1.3) +
        0.08 * Math.sin(t * 6.1 + i * 2.9);
      level = Math.max(0.03, wobble * tilt);
    }
    const bar = Math.max(4, maxHeight * level * energy);
    ctx.beginPath();
    ctx.roundRect(i * slot + (slot - barWidth) / 2, h - bar, barWidth, bar + 6, 4);
    ctx.fill();
  }
}

/** Typography: very large outlined-by-tint type drifting sideways, rows in opposite directions. */
function typography(ctx: CanvasRenderingContext2D, f: Frame) {
  const { width: w, height: h, time: t, energy, palette: p } = f;
  const lines = f.title ? [f.title, f.artist ?? f.title] : [f.appName, f.appName];
  const size = Math.max(120, Math.min(300, h * 0.3));
  ctx.font = `700 ${size}px "Space Grotesk", Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textBaseline = "middle";
  ctx.fillStyle = rgba(p.foreground, 0.04 + 0.03 * energy);
  const gap = size * 0.8;
  lines.forEach((line, row) => {
    const text = line.toUpperCase();
    const span = ctx.measureText(text).width + gap;
    if (!(span > gap)) return;
    const direction = row % 2 ? 1 : -1;
    const offset = (((t * 14 * direction) % span) + span) % span;
    const y = h * (0.3 + 0.4 * row);
    for (let x = offset - span; x < w; x += span) ctx.fillText(text, x, y);
  });
}

/** How many bands of the audio each scene follows, one per source (a cloud, a wave). */
export const AUDIO_VOICES: Record<BackgroundKind, number> = {
  aurora: 4,
  waves: 5,
  spectrum: 0,
  typography: 0,
};

/** The scenes that follow the audio itself; typography shows the track, not its sound. */
export const HEARS_AUDIO: Record<BackgroundKind, boolean> = {
  aurora: true,
  waves: true,
  spectrum: true,
  typography: false,
};

const SCENES: Record<BackgroundKind, (ctx: CanvasRenderingContext2D, frame: Frame) => void> = {
  aurora,
  waves,
  spectrum,
  typography,
};

/** Paints one frame of `kind` on a cleared canvas (the page's own background shows through). */
export function drawScene(kind: BackgroundKind, ctx: CanvasRenderingContext2D, frame: Frame) {
  ctx.clearRect(0, 0, frame.width, frame.height);
  SCENES[kind](ctx, frame);
}

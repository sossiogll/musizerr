import { useEffect, useRef } from "react";
import { REDUCED_MOTION_QUERY, type BackgroundKind } from "@/lib/backgrounds";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { openTap } from "@/lib/audio-analysis";
import {
  AUDIO_VOICES,
  drawScene,
  HEARS_AUDIO,
  readPalette,
  type Frame,
  type Palette,
  type SceneAudio,
  type Voice,
} from "./scenes";

/** At most this many frames a second, and this many device pixels per CSS pixel. */
export const MAX_FPS = 30;
export const MAX_DPR = 1.5;

/** The instant a still frame (reduced motion) shows: arbitrary, but a pleasant one. */
const STILL_TIME = 6;
const STILL_ENERGY = 0.5;
const APP_NAME = "Musizeer";

/**
 * How fast the energy follows play and pause, per second: it rises in about
 * a tenth of a second and falls in about a third, so the scene reacts to the
 * button the way a meter does, never lagging behind it.
 */
export const ATTACK_RATE = 20;
export const RELEASE_RATE = 9;

/** One step of the energy towards `target` (1 playing, 0 not) over `dt` seconds. */
export function stepEnergy(energy: number, target: number, dt: number): number {
  const rate = target > energy ? ATTACK_RATE : RELEASE_RATE;
  const next = energy + (target - energy) * Math.min(1, dt * rate);
  return Math.abs(target - next) < 0.001 ? target : next;
}

/**
 * A band too quiet to stand for its own peak: below this the band is heard
 * as the near silence it is, not stretched to full.
 */
const PEAK_FLOOR = 0.15;

/**
 * One band of the audio as a scene source follows it. Its level is measured
 * against the band's own recent peak (which fades in a few seconds), so the
 * highs, always quieter, move as much as the bass; its pulse jumps when the
 * band rises above its recent average and fades in about a quarter of a second.
 */
export class VoiceFollower {
  private peak = PEAK_FLOOR;
  private average = 0;
  private pulse = 0;
  private travel = 0;

  step(band: number | null, dt: number): Voice {
    this.pulse *= Math.exp(-dt * 8);
    if (band === null) return { level: 0, pulse: this.pulse, travel: this.travel };
    this.peak = Math.max(band, PEAK_FLOOR, this.peak * Math.exp(-dt * 0.25));
    const rise = (band - this.average) / this.peak;
    this.average += (band - this.average) * Math.min(1, dt * 3);
    this.pulse = Math.max(this.pulse, Math.min(1, Math.max(0, rise - 0.08) * 4));
    const level = band / this.peak;
    // Carried by the sound itself, never by the clock: a silent band is still.
    this.travel += dt * 2 * level;
    return { level, pulse: this.pulse, travel: this.travel };
  }
}

export interface BackgroundCanvasProps {
  kind: BackgroundKind;
  playing: boolean;
  title: string | null;
  artist: string | null;
}

/**
 * The canvas of one animated background, behind the whole interface. Loaded
 * on demand (see AppBackground), so none of this is in the main bundle.
 *
 * It never takes part in the page: no pointer events, hidden from assistive
 * technology, at low opacity so text keeps its contrast, painted under the
 * content (the shell is an isolated stacking context). The loop stops while
 * the tab is hidden, is capped at MAX_FPS and MAX_DPR, and with reduced
 * motion a single still frame is painted (and repainted on resize) with no
 * loop at all. Aurora, waves and spectrum follow the sound itself when it can
 * be analysed (lib/audio-analysis), on their own otherwise. The scene follows the player: it moves only while a track
 * plays, comes to rest within a third of a second of a pause (then paints
 * nothing until play resumes) and wakes within a tenth of a second of play.
 * What is playing reaches the loop through a ref, so a track change never
 * restarts it.
 */
export default function BackgroundCanvas(props: BackgroundCanvasProps) {
  const { kind } = props;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const live = useRef(props);
  live.current = props;
  const reducedMotion = useMediaQuery(REDUCED_MOTION_QUERY);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    const animate = !reducedMotion;
    let palette: Palette = readPalette();
    let width = 0;
    let height = 0;
    let resized = true;
    let raf = 0;
    let last = 0;
    let time = 0;
    let energy = live.current.playing ? 1 : 0;
    // The last painted frame no longer shows the scene (size, colours, track): paint again.
    let stale = true;
    let shown = "";
    // The sound itself, for the scenes that follow it (never with a still frame).
    const tap = animate && HEARS_AUDIO[kind] ? openTap() : null;
    const followers = Array.from({ length: AUDIO_VOICES[kind] }, () => new VoiceFollower());
    let audio: SceneAudio | null = null;

    const applySize = () => {
      resized = false;
      width = window.innerWidth;
      height = window.innerHeight;
      const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const frame = (now: number) => {
      raf = 0;
      if (document.hidden) return;
      if (animate && last && now - last < 1000 / MAX_FPS - 2) {
        raf = requestAnimationFrame(frame);
        return;
      }
      if (resized) applySize();
      if (animate) {
        const dt = last ? Math.min((now - last) / 1000, 0.1) : 0;
        last = now;
        energy = stepEnergy(energy, live.current.playing ? 1 : 0, dt);
        time += dt * energy;
        const reading = tap?.read() ?? null;
        const bands = reading?.bands(followers.length);
        const voices = followers.map((follower, i) => follower.step(bands ? bands[i] : null, dt));
        audio = reading ? { bands: (count) => reading.bands(count), voices } : null;
      }
      const { title, artist } = live.current;
      const track = `${title ?? ""}\n${artist ?? ""}`;
      if (track !== shown) stale = true;
      // With nothing playing the scene rests, the same every frame: the loop
      // keeps watching for play, but paints nothing new.
      if (animate && !stale && energy === 0) {
        raf = requestAnimationFrame(frame);
        return;
      }
      stale = false;
      shown = track;
      const state: Frame = {
        width,
        height,
        time: animate ? time : STILL_TIME,
        energy: animate ? energy : STILL_ENERGY,
        palette,
        title,
        artist,
        appName: APP_NAME,
        audio,
      };
      drawScene(kind, ctx, state);
      if (animate) raf = requestAnimationFrame(frame);
    };

    const schedule = () => {
      if (!raf && !document.hidden) raf = requestAnimationFrame(frame);
    };
    const onVisibility = () => {
      if (document.hidden) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        last = 0; // the pause is not time that passed
      } else schedule();
    };
    const onResize = () => {
      resized = true;
      stale = true;
      schedule();
    };
    // A theme switch toggles the `dark` class: take the new colours at once.
    const theme = new MutationObserver(() => {
      palette = readPalette();
      stale = true;
      schedule();
    });

    window.addEventListener("resize", onResize);
    document.addEventListener("visibilitychange", onVisibility);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    schedule();

    return () => {
      if (raf) cancelAnimationFrame(raf);
      tap?.close();
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
      theme.disconnect();
    };
  }, [kind, reducedMotion]);

  // Each effect run draws on a fresh canvas element (the key), so a change of
  // scene never paints over the previous one's pixels or its size.
  return (
    <canvas
      key={`${kind}-${reducedMotion}`}
      ref={canvasRef}
      aria-hidden="true"
      data-background={kind}
      className="pointer-events-none fixed inset-0 -z-10 h-full w-full opacity-60"
    />
  );
}

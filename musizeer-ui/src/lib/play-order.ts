/**
 * The order the player walks its queue in. The queue keeps the list as it
 * was given; the play order is a permutation of its positions: the list
 * order itself, or a shuffled one that starts with the track playing (so
 * turning shuffle on never interrupts it). Pure functions, so they can be
 * tested without an audio element.
 */

/** `off`: stop after the last track; `all`: start over; `one`: play the current track again when it ends. */
export type RepeatMode = "off" | "all" | "one";

export const REPEAT_MODES: readonly RepeatMode[] = ["off", "all", "one"];

/** The mode after `mode` (off → all → one → off). */
export const nextRepeatMode = (mode: RepeatMode): RepeatMode =>
  REPEAT_MODES[(REPEAT_MODES.indexOf(mode) + 1) % REPEAT_MODES.length];

/** The list order: 0, 1, …, length - 1. */
export const listOrder = (length: number): number[] => Array.from({ length }, (_, i) => i);

/**
 * A shuffled order of `length` queue positions that starts with `first`
 * (the track playing; none when it is -1), the others in random order
 * (Fisher–Yates with `random`, injectable for tests).
 */
export function shuffledOrder(length: number, first: number, random: () => number = Math.random): number[] {
  const rest = listOrder(length).filter((i) => i !== first);
  for (let i = rest.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  return first >= 0 && first < length ? [first, ...rest] : rest;
}

/** The play order of a queue of `length` tracks, playing `current`. */
export const playOrder = (length: number, current: number, shuffle: boolean, random?: () => number): number[] =>
  shuffle ? shuffledOrder(length, current, random) : listOrder(length);

/**
 * The queue positions to try, in turn, after (`direction` 1) or before (-1)
 * the track at queue position `current`: the following ones of the play
 * order, and with repeat (`all` or `one`: a skip moves on as with `all`)
 * the ones from the other end, around to just before the current track.
 * The caller plays the first one that can play.
 */
export function candidates(order: readonly number[], current: number, direction: 1 | -1, repeat: RepeatMode): number[] {
  const at = order.indexOf(current);
  if (at < 0) return [];
  const result: number[] = [];
  const wrap = repeat !== "off";
  for (let step = 1; step < order.length; step += 1) {
    const position = at + direction * step;
    if (!wrap && (position < 0 || position >= order.length)) break;
    result.push(order[(position + order.length) % order.length]);
  }
  return result;
}

/** Shuffle and repeat, remembered on this device. */
export interface PlayMode {
  shuffle: boolean;
  repeat: RepeatMode;
}

export const PLAY_MODE_STORAGE_KEY = "musizeer-play-mode";
const DEFAULT_PLAY_MODE: PlayMode = { shuffle: false, repeat: "off" };

/** The mode saved on this device; the default when there is none (or no storage). */
export function readPlayMode(): PlayMode {
  try {
    const saved = JSON.parse(localStorage.getItem(PLAY_MODE_STORAGE_KEY) ?? "null") as Partial<PlayMode> | null;
    return {
      shuffle: saved?.shuffle === true,
      repeat: REPEAT_MODES.includes(saved?.repeat as RepeatMode) ? (saved?.repeat as RepeatMode) : "off",
    };
  } catch {
    return DEFAULT_PLAY_MODE;
  }
}

export function writePlayMode(mode: PlayMode): void {
  try {
    localStorage.setItem(PLAY_MODE_STORAGE_KEY, JSON.stringify(mode));
  } catch {
    /* no storage (private mode): the mode lasts as long as the page */
  }
}

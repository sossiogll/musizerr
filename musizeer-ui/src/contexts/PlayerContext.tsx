import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";
import { useI18n } from "@/i18n";
import { candidates, nextRepeatMode, playOrder, readPlayMode, writePlayMode, type RepeatMode } from "@/lib/play-order";
import { needsFreshElement, registerPlayerAudio, releaseAnalysis, wakeAnalysis } from "@/lib/audio-analysis";
import type { Playback, PlaybackType, Track, TrackStateFields } from "@/types";

export interface PlayerTrack extends TrackStateFields {
  artist: string;
  cover_url: string | null;
  preview_url: string | null;
  stream_url: string | null;
  /** What the viewer can play of it, as the server decided. */
  playback?: Playback;
  artist_provider_id: string | null;
  /**
   * Not a catalog track but one file heard on its own (e.g. a copy in a
   * duplicate group): no track page, no lyrics, no heart; `note` says which
   * file it is.
   */
  detached?: { note: string };
}

interface PlayerControls {
  track: PlayerTrack | null;
  playing: boolean;
  playbackType: PlaybackType | null;
  volume: number;
  /** The play queue, in the order of the list it came from, and the position of its current track. */
  queue: PlayerTrack[];
  index: number;
  /** The order the queue plays in: queue positions, the list order or a shuffled one. */
  order: number[];
  shuffle: boolean;
  repeat: RepeatMode;
  /** Something plays after the current track (next is enabled). */
  hasNext: boolean;
  /** The track playing is heard on its own (`playOnce`): the queue waits behind it. */
  interlude: boolean;
  /**
   * Plays `track`. With `context` (the list it was started from) the queue
   * becomes that list, so previous/next walk through it; otherwise the queue
   * is just this track. Playing the current track again toggles pause.
   */
  play: (track: PlayerTrack, context?: PlayerTrack[]) => void;
  /**
   * Plays `track` on its own, without touching the queue: when it ends (or
   * on next) the queue's track comes back where it was, paused.
   */
  playOnce: (track: PlayerTrack) => void;
  /** Plays the queue's track at `position`, keeping the play order. */
  jumpTo: (position: number) => void;
  togglePlay: () => void;
  next: () => void;
  previous: () => void;
  /** Appends to the queue (once); starts playing if nothing is loaded. */
  addToQueue: (track: PlayerTrack) => void;
  /**
   * Plays `track` right after the current one, whatever the shuffle: one
   * already in the queue is moved there; starts playing if nothing is loaded.
   */
  playNext: (track: PlayerTrack) => void;
  seek: (time: number) => void;
  setVolume: (vol: number) => void;
  /** Shuffle on (a new random order that keeps the current track) or back to the list order. */
  toggleShuffle: () => void;
  /** off → all → one → off. */
  cycleRepeat: () => void;
}

interface PlayerProgress {
  currentTime: number;
  duration: number;
}

const noop = () => {};

const ControlsContext = createContext<PlayerControls>({
  track: null,
  playing: false,
  playbackType: null,
  volume: 0.8,
  queue: [],
  index: -1,
  order: [],
  shuffle: false,
  repeat: "off",
  hasNext: false,
  interlude: false,
  play: noop,
  playOnce: noop,
  jumpTo: noop,
  togglePlay: noop,
  next: noop,
  previous: noop,
  addToQueue: noop,
  playNext: noop,
  seek: noop,
  setVolume: noop,
  toggleShuffle: noop,
  cycleRepeat: noop,
});

// Kept apart from the controls: it changes several times a second while
// playing, and only the few components that show time should re-render.
const ProgressContext = createContext<PlayerProgress>({ currentTime: 0, duration: 0 });

/** Previous goes back to the start of the track after this many seconds. */
const RESTART_THRESHOLD_S = 3;

const sameTrack = (a: PlayerTrack | null | undefined, b: PlayerTrack | null | undefined) =>
  !!a && !!b && a.provider === b.provider && a.provider_id === b.provider_id;

interface Source {
  src: string;
  type: PlaybackType;
}

type PlayableFields = Pick<PlayerTrack, "playback" | "stream_url" | "preview_url">;

/**
 * The audio of `track`, best first, as the server's playback says: `full`
 * plays the stream (the library file, or a provider that streams full
 * tracks), and the preview is the fallback when that stream fails;
 * `preview` plays the preview only. The server only sends a stream_url to a
 * viewer who may hear it (docs/permissions.md); the player never picks one
 * on its own.
 */
export function sourcesFor(track: PlayableFields): Source[] {
  const type = track.playback?.type;
  const sources: Source[] = [];
  if (type === "full" && track.stream_url) sources.push({ src: track.stream_url, type: "full" });
  if ((type === "full" || type === "preview") && track.preview_url) sources.push({ src: track.preview_url, type: "preview" });
  return sources;
}

export function sourceFor(track: PlayableFields): Source | null {
  return sourcesFor(track)[0] ?? null;
}

/** Something to play, as the server decided (full or preview), with its URL. */
export const isPlayable = (track: PlayableFields) => sourcesFor(track).length > 0;

/**
 * What the audio element is working through for the current track (at
 * `index` in the queue, -1 for a track heard on its own): its sources, the
 * one loaded, and whether the one retry with the track asked again (a
 * signed URL may have expired) was spent.
 */
interface Attempt {
  track: PlayerTrack;
  index: number;
  sources: Source[];
  at: number;
  retried: boolean;
}

interface PlayerProviderProps {
  children: React.ReactNode;
  /**
   * The track as the server describes it now (the lists are asked again):
   * its URLs may have expired since it was queued. Used once, when every
   * source of a track failed; null when it cannot be found.
   */
  refreshTrack?: (track: PlayerTrack) => Promise<PlayerTrack | null>;
}

export function PlayerProvider({ children, refreshTrack }: PlayerProviderProps) {
  const { t } = useI18n();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // Makes the audio element (again: see `start`), set by the effect that owns it.
  const freshAudio = useRef<(() => HTMLAudioElement) | null>(null);
  const [queue, setQueue] = useState<PlayerTrack[]>([]);
  const [order, setOrder] = useState<number[]>([]);
  const [index, setIndex] = useState(-1);
  const [interlude, setInterlude] = useState<PlayerTrack | null>(null);
  const [mode, setMode] = useState(readPlayMode);
  const [playing, setPlaying] = useState(false);
  const [playbackType, setPlaybackType] = useState<PlaybackType | null>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolumeState] = useState(0.8);
  const track = interlude ?? (index >= 0 ? (queue[index] ?? null) : null);

  // Latest values for the audio element's listeners, set up once. `load`
  // also writes them at once, so a step right after a load sees its queue.
  const state = useRef({ queue, order, index, interlude, mode, refreshTrack, t });
  state.current = { queue, order, index, interlude, mode, refreshTrack, t };
  const attempt = useRef<Attempt | null>(null);
  // Where the queue's track was when a track heard on its own interrupted it.
  const resumeAt = useRef(0);

  const start = useCallback((source: Source, { autoplay = true, at = 0 }: { autoplay?: boolean; at?: number } = {}) => {
    let audio = audioRef.current;
    if (!audio) return;
    // An element routed through the audio analysis plays only this origin:
    // a source of another one gets a new element (lib/audio-analysis).
    if (needsFreshElement(audio, source.src)) audio = freshAudio.current?.() ?? audio;
    audio.src = source.src;
    setPlaybackType(source.type);
    if (at > 0) audio.addEventListener("loadedmetadata", () => void (audio.currentTime = at), { once: true });
    if (autoplay) audio.play().catch(() => setPlaying(false));
  }, []);

  /** Loads the queue's track at `at` (playing it unless `autoplay` is false); false if it has nothing to play. */
  const load = useCallback(
    (list: PlayerTrack[], at: number, playingOrder: number[], options?: { autoplay?: boolean; at?: number }): boolean => {
      const candidate = list[at];
      if (!audioRef.current || !candidate) return false;
      const sources = sourcesFor(candidate);
      if (!sources.length) return false;
      attempt.current = { track: candidate, index: at, sources, at: 0, retried: false };
      Object.assign(state.current, { queue: list, order: playingOrder, index: at, interlude: null });
      setQueue(list);
      setOrder(playingOrder);
      setIndex(at);
      setInterlude(null);
      setCurrentTime(options?.at ?? 0);
      start(sources[0], options);
      return true;
    },
    [start],
  );

  /**
   * The loaded source failed: the next one (the preview after the full
   * stream), else once the track as the server describes it now, else give
   * up and say so.
   */
  const recover = useCallback(() => {
    const current = attempt.current;
    setPlaying(false);
    if (!current) return;
    if (current.at + 1 < current.sources.length) {
      current.at += 1;
      start(current.sources[current.at]);
      return;
    }
    if (!current.retried) {
      current.retried = true;
      const refresh = state.current.refreshTrack;
      void (refresh ? refresh(current.track).catch(() => null) : Promise.resolve(null)).then((fresh) => {
        // The listener moved on meanwhile: nothing to retry.
        if (attempt.current !== current) return;
        const sources = fresh ? sourcesFor(fresh) : [];
        if (fresh && sources.length) {
          Object.assign(current, { track: fresh, sources, at: 0 });
          // The queue keeps the fresh track: playing it again later must not
          // load the dead URL.
          setQueue((list) => list.map((item, i) => (i === current.index && sameTrack(item, fresh) ? fresh : item)));
        }
        start(current.sources[current.at]);
      });
      return;
    }
    attempt.current = null;
    toast.error(state.current.t("player.unavailable", { title: current.track.title }));
  }, [start]);

  /** The next playable track after (or before) the queue's track `from`, in play order, loaded; false at the end. */
  const step = useCallback(
    (from: number, direction: 1 | -1): boolean => {
      const { queue: list, order: playingOrder, mode: current } = state.current;
      for (const at of candidates(playingOrder, from, direction, current.repeat)) {
        if (load(list, at, playingOrder)) return true;
      }
      return false;
    },
    [load],
  );

  /** Back from a track heard on its own to the queue's track, where it was (playing if `resume`). */
  const endInterlude = useCallback(
    (resume: boolean) => {
      const { queue: list, order: playingOrder, index: at } = state.current;
      setInterlude(null);
      state.current.interlude = null;
      if (at < 0 || !load(list, at, playingOrder, { autoplay: resume, at: resumeAt.current })) {
        attempt.current = null;
        setPlaying(false);
      }
    },
    [load],
  );

  useEffect(() => {
    let detach: AbortController | null = null;
    // Drops the element in use: its listeners first, so its last events
    // (the pause, an error for the emptied source) reach nobody.
    const drop = () => {
      const old = audioRef.current;
      detach?.abort();
      if (!old) return;
      old.pause();
      old.removeAttribute("src");
      releaseAnalysis(old);
    };
    const make = () => {
      const volume = audioRef.current?.volume ?? 0.8;
      drop();
      const audio = new Audio();
      const listen = new AbortController();
      const { signal } = listen;
      detach = listen;
      audio.volume = volume;
      audioRef.current = audio;
      registerPlayerAudio(audio);
      audio.addEventListener("timeupdate", () => setCurrentTime(audio.currentTime), { signal });
      audio.addEventListener("durationchange", () => setDuration(audio.duration || 0), { signal });
      audio.addEventListener("pause", () => setPlaying(false), { signal });
      audio.addEventListener(
        "play",
        () => {
          wakeAnalysis(audio);
          setPlaying(true);
        },
        { signal },
      );
      // An expired or unreachable source: without this the button would stay
      // on "Pause" while nothing plays.
      audio.addEventListener("error", () => recover(), { signal });
      audio.addEventListener(
        "ended",
        () => {
          setPlaying(false);
          const { interlude: alone, index: at, mode: current } = state.current;
          if (alone) endInterlude(false);
          // Repeat one, or repeat all with nothing else to play: this track again.
          else if (current.repeat === "one" || (!step(at, 1) && current.repeat === "all" && at >= 0)) {
            audio.currentTime = 0;
            audio.play().catch(() => setPlaying(false));
          }
        },
        { signal },
      );
      return audio;
    };
    freshAudio.current = make;
    make();
    return () => {
      drop();
      freshAudio.current = null;
      audioRef.current = null;
      registerPlayerAudio(null);
    };
  }, [step, recover, endInterlude]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || !audio.src) return;
    if (audio.paused) audio.play().catch(() => setPlaying(false));
    else audio.pause();
  }, []);

  const play = useCallback(
    (next: PlayerTrack, context?: PlayerTrack[]) => {
      if (!audioRef.current) return;
      const { queue: list, order: playingOrder, index: at, interlude: alone, mode: current } = state.current;
      const loaded = alone ?? list[at];
      // The same track with the same audio toggles pause. With a new URL
      // (the list was asked again, e.g. after an expired preview) the new
      // source is loaded: replaying the old one would fail the same way.
      if (sameTrack(loaded, next) && sourceFor(loaded)?.src === sourceFor(next)?.src) {
        togglePlay();
        return;
      }
      if (!alone && sameTrack(loaded, next) && !context?.length) {
        load(list.map((item, i) => (i === at ? next : item)), at, playingOrder);
        return;
      }
      const newQueue = context?.length ? context : [next];
      const position = Math.max(0, newQueue.findIndex((item) => sameTrack(item, next)));
      load(newQueue, position, playOrder(newQueue.length, position, current.shuffle));
    },
    [load, togglePlay],
  );

  const playOnce = useCallback(
    (alone: PlayerTrack) => {
      const audio = audioRef.current;
      if (!audio) return;
      const current = state.current.interlude;
      if (sameTrack(current, alone)) {
        togglePlay();
        return;
      }
      const sources = sourcesFor(alone);
      if (!sources.length) return;
      // Leaving the queue's track (not another track heard on its own): remember where.
      if (!current) resumeAt.current = state.current.index >= 0 ? audio.currentTime : 0;
      // Nothing to ask again: it is not a catalog track.
      attempt.current = { track: alone, index: -1, sources, at: 0, retried: true };
      state.current.interlude = alone;
      setInterlude(alone);
      setCurrentTime(0);
      start(sources[0]);
    },
    [start, togglePlay],
  );

  const jumpTo = useCallback(
    (position: number) => {
      const { queue: list, order: playingOrder } = state.current;
      load(list, position, playingOrder);
    },
    [load],
  );

  const next = useCallback(() => {
    if (state.current.interlude) endInterlude(true);
    else step(state.current.index, 1);
  }, [step, endInterlude]);

  const previous = useCallback(() => {
    const audio = audioRef.current;
    if (audio && (audio.currentTime > RESTART_THRESHOLD_S || state.current.interlude)) {
      audio.currentTime = 0;
      return;
    }
    if (!step(state.current.index, -1) && audio) audio.currentTime = 0;
  }, [step]);

  const addToQueue = useCallback(
    (item: PlayerTrack) => {
      const { queue: list, order: playingOrder, index: at } = state.current;
      if (at < 0) {
        load([item], 0, [0]);
        return;
      }
      if (list.some((queued) => sameTrack(queued, item))) return;
      const longer = [...list, item];
      const longerOrder = [...playingOrder, list.length];
      Object.assign(state.current, { queue: longer, order: longerOrder });
      setQueue(longer);
      setOrder(longerOrder);
    },
    [load],
  );

  const playNext = useCallback(
    (item: PlayerTrack) => {
      const { queue: list, order: playingOrder, index: at } = state.current;
      if (at < 0) {
        load([item], 0, [0]);
        return;
      }
      const known = list.findIndex((queued) => sameTrack(queued, item));
      // The track playing is already "next" of nothing: leave it be.
      if (known === at) return;
      const longer = known < 0 ? [...list, item] : list;
      const position = known < 0 ? list.length : known;
      const rest = playingOrder.filter((p) => p !== position);
      rest.splice(rest.indexOf(at) + 1, 0, position);
      Object.assign(state.current, { queue: longer, order: rest });
      setQueue(longer);
      setOrder(rest);
    },
    [load],
  );

  const seek = useCallback((time: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = time;
    setCurrentTime(time);
  }, []);

  const setVolume = useCallback((vol: number) => {
    const audio = audioRef.current;
    if (audio) audio.volume = vol;
    setVolumeState(vol);
  }, []);

  const changeMode = useCallback((change: Partial<typeof mode>) => {
    const changed = { ...state.current.mode, ...change };
    state.current.mode = changed;
    writePlayMode(changed);
    setMode(changed);
  }, []);

  const toggleShuffle = useCallback(() => {
    const { queue: list, index: at, mode: current } = state.current;
    const shuffle = !current.shuffle;
    const shuffled = playOrder(list.length, at, shuffle);
    state.current.order = shuffled;
    setOrder(shuffled);
    changeMode({ shuffle });
  }, [changeMode]);

  const cycleRepeat = useCallback(() => changeMode({ repeat: nextRepeatMode(state.current.mode.repeat) }), [changeMode]);

  const hasNext = interlude ? index >= 0 : candidates(order, index, 1, mode.repeat).length > 0;

  const controls = useMemo<PlayerControls>(
    () => ({
      track,
      playing,
      playbackType,
      volume,
      queue,
      index,
      order,
      shuffle: mode.shuffle,
      repeat: mode.repeat,
      hasNext,
      interlude: !!interlude,
      play,
      playOnce,
      jumpTo,
      togglePlay,
      next,
      previous,
      addToQueue,
      playNext,
      seek,
      setVolume,
      toggleShuffle,
      cycleRepeat,
    }),
    [
      track, playing, playbackType, volume, queue, index, order, mode, hasNext, interlude,
      play, playOnce, jumpTo, togglePlay, next, previous, addToQueue, playNext, seek, setVolume, toggleShuffle, cycleRepeat,
    ],
  );
  const progress = useMemo(() => ({ currentTime, duration }), [currentTime, duration]);

  return (
    <ControlsContext.Provider value={controls}>
      <ProgressContext.Provider value={progress}>{children}</ProgressContext.Provider>
    </ControlsContext.Provider>
  );
}

/** Player state and controls: does not change while the time advances. */
export const usePlayer = () => useContext(ControlsContext);

/** Current time and duration: for the components that display them. */
export const usePlayerProgress = () => useContext(ProgressContext);

/** The fields the player needs from a Track. */
export function toPlayerTrack(t: Track): PlayerTrack {
  return {
    title: t.title,
    artist: t.artist,
    cover_url: t.cover_url,
    preview_url: t.preview_url,
    stream_url: t.stream_url,
    playback: t.playback,
    provider: t.provider,
    provider_id: t.provider_id,
    artist_provider_id: t.artist_provider_id,
    // Its library state, for the player's heart.
    internal_id: t.internal_id,
    external_ids: t.external_ids,
    library_id: t.library_id,
    download_status: t.download_status,
    in_library: t.in_library,
    favorite: t.favorite,
    favorite_pending: t.favorite_pending,
  };
}

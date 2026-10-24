import { memo, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { getTrackLyrics } from "@/lib/api-client";
import { usePlayer, usePlayerProgress } from "@/contexts/PlayerContext";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { LrcLine, Lyrics } from "@/types";

/**
 * The lyrics of a track, shared by the track page and the player's lyrics
 * panel: one cache entry per track, never refetched while the app is open.
 */
export function useTrackLyrics(provider: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["lyrics", provider, id],
    queryFn: () => getTrackLyrics(provider!, id!),
    enabled: !!provider && !!id,
    retry: false,
    staleTime: Infinity,
  });
}

export const hasLyrics = (lyrics: Lyrics | null | undefined): lyrics is Lyrics => !!lyrics && (!!lyrics.synced.length || !!lyrics.plain);

/**
 * The lyrics themselves: a skeleton while they load, a note when there are
 * none, else a box of lines. Synced lyrics follow the player when
 * `following` (this track plays in full): the current line is highlighted and
 * kept in view by scrolling the box, never the page, and a line is a button
 * that seeks there. `boxClassName` sizes the box for where it sits.
 */
export function LyricsBody({
  lyrics,
  loading,
  following,
  labelledBy,
  boxClassName,
}: {
  lyrics: Lyrics | undefined;
  loading: boolean;
  following: boolean;
  /** The id of the heading that names the box. */
  labelledBy: string;
  boxClassName?: string;
}) {
  const { t } = useI18n();
  if (loading) {
    return (
      <div aria-hidden className={cn(SURFACE, "space-y-2")}>
        {[70, 55, 80, 60, 75, 50].map((w) => (
          <Skeleton key={w} className="h-4" style={{ width: `${w}%` }} />
        ))}
      </div>
    );
  }
  if (!hasLyrics(lyrics)) return <p className={cn(SURFACE, "text-sm text-muted-foreground")}>{t("track.noLyrics")}</p>;
  if (!lyrics.synced.length) {
    return (
      <LyricsBox labelledBy={labelledBy} className={boxClassName}>
        <p className="whitespace-pre-wrap">{lyrics.plain}</p>
      </LyricsBox>
    );
  }
  if (following) return <FollowedLyrics lines={lyrics.synced} labelledBy={labelledBy} className={boxClassName} />;
  return (
    <LyricsBox labelledBy={labelledBy} className={boxClassName}>
      {lyrics.synced.map((line, i) => (
        <p key={i}>{line.text || " "}</p>
      ))}
    </LyricsBox>
  );
}

const SURFACE = "rounded-lg bg-muted/40 p-5";

// Scrolls on its own where it is given a height: focusable, so the keyboard can scroll it too.
function LyricsBox({
  children,
  labelledBy,
  className,
  boxRef,
}: {
  children: React.ReactNode;
  labelledBy: string;
  className?: string;
  boxRef?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={boxRef}
      tabIndex={0}
      aria-labelledby={labelledBy}
      role="region"
      className={cn(
        SURFACE,
        "relative space-y-1.5 text-base leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** Synced lyrics following the player; only the lines whose state changes re-render. */
function FollowedLyrics({ lines, labelledBy, className }: { lines: LrcLine[]; labelledBy: string; className?: string }) {
  const { seek } = usePlayer();
  const { currentTime } = usePlayerProgress();
  const boxRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLButtonElement>(null);
  const times = useMemo(() => lines.map((l) => l.time_ms), [lines]);
  const active = currentLine(times, currentTime * 1000);

  useEffect(() => {
    const box = boxRef.current;
    const line = activeRef.current;
    if (!box || !line) return;
    // The box is the lines' offset parent (relative): keep the line a third down.
    const offset = line.offsetTop - box.clientHeight / 3;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (typeof box.scrollTo === "function") box.scrollTo({ top: offset, behavior: reduce ? "auto" : "smooth" });
    else box.scrollTop = offset;
  }, [active]);

  return (
    <LyricsBox boxRef={boxRef} labelledBy={labelledBy} className={className}>
      {lines.map((line, i) => (
        <LyricLine key={i} line={line} state={i === active ? "active" : i < active ? "past" : "next"} onSeek={seek} lineRef={i === active ? activeRef : undefined} />
      ))}
    </LyricsBox>
  );
}

const LyricLine = memo(function LyricLine({
  line,
  state,
  onSeek,
  lineRef,
}: {
  line: LrcLine;
  state: "past" | "active" | "next";
  onSeek: (time: number) => void;
  lineRef?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <button
      ref={lineRef}
      type="button"
      aria-current={state === "active" ? "true" : undefined}
      onClick={() => onSeek(line.time_ms / 1000)}
      className={cn(
        "block w-full rounded-sm px-1 py-0.5 text-left transition-colors duration-200 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        state === "active" ? "text-lg font-semibold text-foreground" : state === "past" ? "text-muted-foreground/70" : "text-muted-foreground",
      )}
    >
      {line.text || " "}
    </button>
  );
});

/** The last line whose time has come (binary search), -1 before the first. */
export function currentLine(times: number[], nowMs: number): number {
  let lo = 0;
  let hi = times.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= nowMs) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

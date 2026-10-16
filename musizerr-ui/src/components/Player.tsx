import { forwardRef, memo, useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import {
  Download,
  Ellipsis,
  ListMusic,
  Loader2,
  MicVocal,
  Pause,
  Play,
  Repeat,
  Repeat1,
  Shuffle,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
} from "lucide-react";
import { isPlayable, usePlayer, usePlayerProgress, type PlayerTrack } from "@/contexts/PlayerContext";
import { CoverImage } from "@/components/CoverImage";
import { DownloadButton } from "@/components/DownloadButton";
import { FavoriteHeart } from "@/components/tracks/FavoriteHeart";
import { useTrackDownload } from "@/hooks/useTrackDownload";
import { useTrackFavorite } from "@/hooks/useTrackFavorite";
import { LyricsBody, hasLyrics, useTrackLyrics } from "@/components/lyrics/Lyrics";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useProviderLabel } from "@/components/ProviderBadges";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

function formatTime(seconds: number): string {
  if (!seconds || !isFinite(seconds)) return "0:00";
  const min = Math.floor(seconds / 60);
  const sec = Math.floor(seconds % 60);
  return `${min}:${sec.toString().padStart(2, "0")}`;
}

const ICON_BUTTON =
  "grid size-11 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40 aria-disabled:opacity-40";

// Shuffle and repeat: lit when on.
const TOGGLE_ON = "text-primary hover:text-primary";

type PanelKind = "queue" | "lyrics";

/**
 * The player: a mini-player above the bottom bar on a phone (XS), a full
 * bar from S up in three zones — the track (and its heart) on the left,
 * shuffle / previous / play / next / repeat and the seek slider in the
 * centre, volume, lyrics and queue on the right. On XS shuffle, repeat,
 * lyrics and queue are in the overflow menu. The side zones are equal columns, so the controls sit exactly in
 * the middle of the bar; each tier gives them the room its actions need.
 * The parts that show the time subscribe to the progress on their own, so
 * the rest of the bar does not re-render while a track plays.
 */
export function Player() {
  const { track } = usePlayer();
  return track ? <PlayerBar track={track} /> : null;
}

function PlayerBar({ track }: { track: PlayerTrack }) {
  const { playing, playbackType, queue, hasNext, interlude, togglePlay, next, previous } = usePlayer();
  const { t } = useI18n();
  // A file heard on its own has no lyrics (nor a provider to ask).
  const lyrics = useTrackLyrics(track.detached ? undefined : track.provider, track.provider_id);
  const noLyrics = !lyrics.isLoading && !hasLyrics(lyrics.data);
  const panels = usePlayerPanels();
  const ids = { queue: useId(), lyrics: useId() };

  return (
    <section
      aria-label={t("player.label")}
      className="fixed inset-x-0 bottom-[calc(68px+env(safe-area-inset-bottom))] z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75 sm:bottom-0 xl:left-60"
    >
      <ProgressLine />
      <div
        data-testid="player-zones"
        className="relative mx-auto flex h-16 max-w-[1600px] items-center gap-2 px-3 [--player-side:6rem] sm:grid sm:h-20 sm:grid-cols-[minmax(var(--player-side),1fr)_minmax(0,min(36rem,50%))_minmax(var(--player-side),1fr)] sm:gap-4 sm:px-4 md:[--player-side:9rem] lg:[--player-side:15rem] xl:[--player-side:20rem]"
      >
        <NowPlaying track={track} preview={playbackType === "preview"} />

        <div data-testid="player-controls" className="flex shrink-0 flex-col items-center sm:w-full sm:justify-self-center">
          <div className="flex items-center gap-1">
            <ShuffleButton className="max-sm:hidden" />
            <button type="button" onClick={previous} aria-label={t("player.previous")} className={cn(ICON_BUTTON, "max-sm:hidden")}>
              <SkipBack aria-hidden className="size-5" />
            </button>
            <button
              type="button"
              onClick={togglePlay}
              aria-label={playing ? t("player.pause") : t("player.play")}
              className="grid size-11 shrink-0 place-items-center rounded-md bg-primary text-primary-foreground shadow-sm transition-transform hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              {playing ? <Pause aria-hidden className="size-5" /> : <Play aria-hidden className="ml-0.5 size-5" />}
            </button>
            <button
              type="button"
              onClick={next}
              disabled={!hasNext}
              aria-label={interlude ? t("player.backToQueue") : t("player.next")}
              title={interlude ? t("player.backToQueue") : undefined}
              className={ICON_BUTTON}
            >
              <SkipForward aria-hidden className="size-5" />
            </button>
            <RepeatButton className="max-sm:hidden" />
          </div>
          <SeekBar />
        </div>

        <div data-testid="player-actions" className="flex min-w-0 shrink-0 items-center justify-end gap-1 lg:gap-2">
          <VolumeControl />
          <PanelToggle
            kind="lyrics"
            panels={panels}
            panelId={ids.lyrics}
            label={t("player.lyrics")}
            icon={MicVocal}
            unavailable={noLyrics ? t("player.noLyrics") : undefined}
          />
          <PanelToggle
            kind="queue"
            panels={panels}
            panelId={ids.queue}
            label={t("player.queueButton")}
            ariaLabel={t("player.queue", { count: queue.length })}
            icon={ListMusic}
          />
          <MoreMenu panels={panels} noLyrics={noLyrics} queueLength={queue.length} />
        </div>

        {panels.open && (
          <PlayerPanel
            ref={panels.panelRef}
            id={ids[panels.open]}
            title={panels.open === "queue" ? t("player.queueTitle") : t("player.lyrics")}
            aside={panels.open === "lyrics" ? <LyricsSource source={lyrics.data?.source} /> : undefined}
          >
            {panels.open === "queue" ? (
              <QueueList />
            ) : (
              <LyricsBody
                lyrics={lyrics.data}
                loading={lyrics.isLoading}
                following={playbackType === "full"}
                labelledBy={`${ids.lyrics}-title`}
                boxClassName="max-h-[min(50dvh,24rem)] overflow-y-auto overscroll-contain rounded-none bg-transparent"
              />
            )}
          </PlayerPanel>
        )}
      </div>
    </section>
  );
}

/**
 * The queue and lyrics panels: one open at a time. Opening one moves focus
 * into it; Escape closes it and gives focus back to the control that opened
 * it; a press outside the panel and its toggles closes it too.
 */
function ShuffleButton({ className }: { className?: string }) {
  const { shuffle, toggleShuffle } = usePlayer();
  const { t } = useI18n();
  return (
    <button
      type="button"
      onClick={toggleShuffle}
      aria-pressed={shuffle}
      aria-label={t("player.shuffle")}
      title={t("player.shuffle")}
      className={cn(ICON_BUTTON, shuffle && TOGGLE_ON, className)}
    >
      <Shuffle aria-hidden className="size-5" />
    </button>
  );
}

/** off → all → one: the name says the current mode, pressed while it repeats. */
function RepeatButton({ className }: { className?: string }) {
  const { repeat, cycleRepeat } = usePlayer();
  const { t } = useI18n();
  const label = t(`player.repeat_${repeat}`);
  return (
    <button
      type="button"
      onClick={cycleRepeat}
      aria-pressed={repeat !== "off"}
      aria-label={label}
      title={label}
      className={cn(ICON_BUTTON, repeat !== "off" && TOGGLE_ON, className)}
    >
      {repeat === "one" ? <Repeat1 aria-hidden className="size-5" /> : <Repeat aria-hidden className="size-5" />}
    </button>
  );
}

function usePlayerPanels() {
  const [open, setOpen] = useState<PanelKind | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  const show = useCallback((kind: PanelKind | null, from?: HTMLElement | null) => {
    if (kind) opener.current = from ?? (document.activeElement as HTMLElement | null);
    setOpen(kind);
  }, []);
  const toggle = useCallback((kind: PanelKind, from: HTMLElement) => {
    opener.current = from;
    setOpen((cur) => (cur === kind ? null : kind));
  }, []);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(null);
      opener.current?.focus();
    };
    const onPointer = (e: PointerEvent) => {
      const target = e.target as Element;
      if (panelRef.current?.contains(target) || target.closest?.("[data-player-toggle]")) return;
      setOpen(null);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return { open, show, toggle, panelRef };
}
type Panels = ReturnType<typeof usePlayerPanels>;

/**
 * Lyrics or queue: a labelled button from L, an icon with its label for
 * screen readers on S and M, in the XS overflow menu below that. When the
 * panel has nothing to show it stays focusable (aria-disabled) with a
 * tooltip saying why.
 */
function PanelToggle({
  kind,
  panels,
  panelId,
  label,
  ariaLabel = label,
  icon: Icon,
  unavailable,
}: {
  kind: PanelKind;
  panels: Panels;
  panelId: string;
  label: string;
  ariaLabel?: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  /** Why the panel cannot open, if it cannot. */
  unavailable?: string;
}) {
  const expanded = panels.open === kind;
  const reasonId = useId();
  const button = (
    <button
      type="button"
      data-player-toggle
      aria-label={ariaLabel}
      aria-expanded={expanded}
      aria-controls={panelId}
      aria-disabled={unavailable ? true : undefined}
      aria-describedby={unavailable ? reasonId : undefined}
      onClick={(e) => !unavailable && panels.toggle(kind, e.currentTarget)}
      className={cn(
        ICON_BUTTON,
        "max-sm:hidden lg:flex lg:h-9 lg:w-auto lg:items-center lg:gap-1.5 lg:border lg:px-3 lg:text-sm lg:font-medium",
        expanded && "bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary lg:border-primary/30",
        unavailable && "cursor-not-allowed hover:bg-transparent hover:text-muted-foreground",
      )}
    >
      <Icon aria-hidden className="size-5 lg:size-4" />
      <span aria-hidden className="hidden lg:inline">
        {label}
      </span>
      {unavailable && (
        <span id={reasonId} className="sr-only">
          {unavailable}
        </span>
      )}
    </button>
  );
  // Always inside the tooltip, which only opens when there is a reason: the button keeps its node (and focus).
  return (
    <Tooltip open={unavailable ? undefined : false}>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent side="top">{unavailable}</TooltipContent>
    </Tooltip>
  );
}

/** XS: shuffle, repeat, lyrics and queue behind one overflow button, next to play and next. */
function MoreMenu({ panels, noLyrics, queueLength }: { panels: Panels; noLyrics: boolean; queueLength: number }) {
  const { t } = useI18n();
  const { shuffle, repeat, toggleShuffle, cycleRepeat } = usePlayer();
  const trigger = useRef<HTMLButtonElement>(null);
  const picked = useRef(false);
  const pick = (kind: PanelKind) => {
    picked.current = true;
    panels.show(kind, trigger.current);
  };
  return (
    // Not modal: a modal menu traps the focus and would pull it back from the panel it opens.
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button ref={trigger} type="button" data-player-toggle aria-label={t("player.more")} className={cn(ICON_BUTTON, "sm:hidden")}>
          <Ellipsis aria-hidden className="size-5" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side="top"
        align="end"
        onCloseAutoFocus={(e) => {
          // A panel opened from the menu takes the focus; don't hand it back to the trigger.
          if (picked.current) e.preventDefault();
          picked.current = false;
        }}
      >
        <DropdownMenuItem disabled={noLyrics} onSelect={() => pick("lyrics")} className="min-h-11">
          <MicVocal aria-hidden />
          {noLyrics ? t("player.noLyrics") : t("player.lyrics")}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => pick("queue")} className="min-h-11">
          <ListMusic aria-hidden />
          {t("player.queue", { count: queueLength })}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem checked={shuffle} onCheckedChange={toggleShuffle} className="min-h-11">
          {t("player.shuffle")}
        </DropdownMenuCheckboxItem>
        {/* Stays open: each press moves to the next mode, and the name says which. */}
        <DropdownMenuItem
          onSelect={(e) => {
            e.preventDefault();
            cycleRepeat();
          }}
          className="min-h-11"
        >
          {repeat === "one" ? <Repeat1 aria-hidden /> : <Repeat aria-hidden />}
          {t(`player.repeat_${repeat}`)}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A panel above the bar, at its right end (full width on a phone). */
const PlayerPanel = forwardRef<HTMLDivElement, { id: string; title: string; aside?: React.ReactNode; children: React.ReactNode }>(
  function PlayerPanel({ id, title, aside, children }, ref) {
    return (
      <div
        ref={ref}
        id={id}
        role="region"
        aria-labelledby={`${id}-title`}
        tabIndex={-1}
        className="absolute inset-x-2 bottom-full mb-2 overflow-hidden rounded-lg border bg-popover text-popover-foreground shadow-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:inset-x-auto sm:right-4 sm:w-96"
      >
        <div className="flex items-center gap-2 border-b px-4 py-3">
          <p id={`${id}-title`} className="text-sm font-semibold">
            {title}
          </p>
          {aside}
        </div>
        {children}
      </div>
    );
  },
);

function LyricsSource({ source }: { source: string | undefined }) {
  const { t } = useI18n();
  const providerLabel = useProviderLabel();
  if (!source) return null;
  return <span className="ml-auto text-xs text-muted-foreground">{t("common.via", { provider: providerLabel(source) })}</span>;
}

/** The queue in the order it plays (shuffled or not), and a click to jump there. */
function QueueList() {
  const { queue, order, index, interlude, jumpTo } = usePlayer();
  const current = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    current.current?.scrollIntoView?.({ block: "nearest" });
  }, []);
  return (
    <ol className="max-h-80 overflow-y-auto overscroll-contain p-1.5">
      {order.map((position, i) => {
        const item = queue[position];
        if (!item) return null;
        const isCurrent = position === index && !interlude;
        return (
          <li key={`${item.provider}:${item.provider_id}:${position}`}>
            <button
              ref={isCurrent ? current : undefined}
              type="button"
              aria-current={isCurrent ? "true" : undefined}
              disabled={!isPlayable(item)}
              onClick={() => jumpTo(position)}
              className={cn(
                "flex min-h-11 w-full items-center gap-3 rounded-md px-2.5 py-1.5 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40",
                isCurrent && "bg-primary/10 text-primary",
              )}
            >
              <span className="w-5 shrink-0 text-right text-xs tabular-nums text-muted-foreground">{i + 1}</span>
              <span className="min-w-0">
                <span className="block truncate font-medium">{item.title}</span>
                <span className="block truncate text-xs text-muted-foreground">{item.artist}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function NowPlaying({ track, preview }: { track: PlayerTrack; preview: boolean }) {
  const { t } = useI18n();
  const title = "block truncate text-sm font-semibold";
  return (
    <div data-testid="player-track" className="flex min-w-0 flex-1 items-center gap-3">
      <CoverImage src={track.cover_url} alt="" type="track" className="size-11 rounded-md sm:size-12" />
      <div className="min-w-0">
        {track.detached ? (
          <p className={title}>{track.title}</p>
        ) : (
          <Link
            to="/track/$provider/$id"
            params={{ provider: track.provider, id: track.provider_id }}
            className={cn(title, "hover:underline focus-visible:underline focus-visible:outline-none")}
          >
            {track.title}
          </Link>
        )}
        <p className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          {track.artist_provider_id && !track.detached ? (
            <Link to="/artist/$provider/$id" params={{ provider: track.provider, id: track.artist_provider_id }} className="truncate hover:underline">
              {track.artist}
            </Link>
          ) : (
            <span className="truncate">{track.artist}</span>
          )}
          {preview && (
            <span className="shrink-0 rounded-xs bg-primary/15 px-1.5 text-[10px] font-semibold uppercase tracking-wide text-primary">
              {t("player.preview")}
            </span>
          )}
        </p>
        {track.detached && <p className="truncate text-xs font-medium text-primary">{track.detached.note}</p>}
      </div>
      {!track.detached && <LibraryAction track={track} />}
    </div>
  );
}

const PLAYER_DOWNLOAD = cn(ICON_BUTTON, "text-foreground/90");

/**
 * The heart of the track playing, as a row has it: only in the library
 * (filled when a favorite, the toggle shared with every row and page of
 * it), the pending heart while a download of the viewer's will add it;
 * outside the library the row's Download, as one icon (a user's adds to
 * favorites, an admin chooses).
 */
const LibraryAction = memo(function LibraryAction({ track }: { track: PlayerTrack }) {
  const { t } = useI18n();
  const { slot, modes, download, busy, disabledReason, favoriteQueued, libraryId, approval } = useTrackDownload(track);
  const heart = useTrackFavorite(track, slot, libraryId, favoriteQueued);
  if (heart.kind !== "hidden") return <FavoriteHeart state={heart} title={track.title} />;
  if (slot.kind !== "available") return null;
  return (
    <DownloadButton
      modes={modes}
      onDownload={download}
      icon={busy ? <Loader2 aria-hidden className="size-5 animate-spin" /> : <Download aria-hidden className="size-5" />}
      text={t(approval.waits ? "approval.request" : "slot.download")}
      labels={{
        plain: t("slot.downloadPlainTrack", { title: track.title }),
        favorite: t(approval.waits ? "approval.requestItem" : "slot.downloadWithFavoriteTrack", { title: track.title }),
      }}
      menuTexts={{ plain: t("slot.download"), favorite: t("slot.downloadWithFavorite") }}
      menuLabel={t("slot.downloadOptions", { title: track.title })}
      disabled={busy || !!disabledReason}
      title={disabledReason ?? approval.hint}
      className={PLAYER_DOWNLOAD}
      iconOnly
    />
  );
});

/** XS: a thin line along the top edge; the slider takes over from S up. */
const ProgressLine = memo(function ProgressLine() {
  const { currentTime, duration } = usePlayerProgress();
  const pct = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0;
  return (
    <div aria-hidden className="h-0.5 w-full bg-muted sm:hidden">
      <div className="h-full bg-primary" style={{ width: `${pct}%` }} />
    </div>
  );
});

/** Keyboard- and screen-reader-operable seek (a range input), from S up. */
const SeekBar = memo(function SeekBar() {
  const { seek } = usePlayer();
  const { currentTime, duration } = usePlayerProgress();
  const { t } = useI18n();
  const pct = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0;
  return (
    <div className="hidden w-full items-center gap-2 text-[11px] tabular-nums text-muted-foreground sm:flex">
      <span className="w-9 text-right">{formatTime(currentTime)}</span>
      <input
        type="range"
        min={0}
        max={duration || 0}
        step={1}
        value={Math.min(currentTime, duration || 0)}
        disabled={duration <= 0}
        onChange={(e) => seek(Number(e.target.value))}
        aria-label={t("player.seek")}
        aria-valuetext={t("player.position", { current: formatTime(currentTime), total: formatTime(duration) })}
        className="player-range h-4 flex-1 cursor-pointer"
        style={{ "--range-pct": `${pct}%` } as React.CSSProperties}
      />
      <span className="w-9">{formatTime(duration)}</span>
    </div>
  );
});

/** Mute from M up; the slider joins it at XL, where the right zone has room for it. */
function VolumeControl() {
  const { volume, setVolume } = usePlayer();
  const { t } = useI18n();
  const before = useRef(volume || 0.8);
  const muted = volume === 0;
  return (
    <div className="hidden items-center md:flex">
      <button
        type="button"
        className={cn(ICON_BUTTON, "lg:size-9")}
        aria-label={muted ? t("player.unmute") : t("player.mute")}
        onClick={() => {
          if (muted) setVolume(before.current);
          else {
            before.current = volume;
            setVolume(0);
          }
        }}
      >
        {muted ? <VolumeX aria-hidden className="size-5" /> : <Volume2 aria-hidden className="size-5" />}
      </button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={volume}
        onChange={(e) => setVolume(parseFloat(e.target.value))}
        aria-label={t("player.volume")}
        className="player-range hidden h-4 w-20 cursor-pointer xl:block"
        style={{ "--range-pct": `${volume * 100}%` } as React.CSSProperties}
      />
    </div>
  );
}

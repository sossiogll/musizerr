import { memo, useMemo } from "react";
import { Link } from "@tanstack/react-router";
import { Pause, Play } from "lucide-react";
import { CoverImage } from "@/components/CoverImage";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useTrackDownload } from "@/hooks/useTrackDownload";
import { useTrackFavorite } from "@/hooks/useTrackFavorite";
import type { DownloadOptions } from "@/lib/api-client";
import { isPlayable, toPlayerTrack, usePlayer, type PlayerTrack } from "@/contexts/PlayerContext";
import { useI18n } from "@/i18n";
import { cn, formatDuration } from "@/lib/utils";
import type { DownloadRequestResult, DownloadStatus, Track } from "@/types";
import { TrackMenu } from "@/components/menu/TrackMenu";
import { FavoriteHeart } from "./FavoriteHeart";
import { StatusSlot } from "./StatusSlot";

export interface TrackRowProps {
  track: Track;
  /** Position shown in a # column (L/XL), or in place of the cover when `showCover` is false. */
  number?: number | null;
  showCover?: boolean;
  /** An album column on L/XL. */
  showAlbum?: boolean;
  /** The album in the subtitle where there is no album column (off on an album's own page). */
  albumInSubtitle?: boolean;
  /** The list this row belongs to: the play queue when it is started from here. */
  queue?: PlayerTrack[];
  /** A status known better than the track's own (e.g. the Downloads page's aggregate). */
  statusOverride?: DownloadStatus | null;
  /** How Retry re-queues a failed track (default: a new download of it). */
  retry?: (options: DownloadOptions) => Promise<DownloadRequestResult>;
  /** Inside a playlist the viewer edits: "Remove from this playlist" in the row's menu. */
  onRemove?: () => void;
}

// Full class strings, so Tailwind sees them. Columns per tier (the last one
// holds the status slot, the 44px heart and the 44px menu button):
//   XS: cover-play · title (with duration) · status icon + heart + menu
//   S:  cover-play · title · duration · status icon + heart + menu
//   M:  cover-play · title (artist · album) · duration · status + heart + menu
//   L+: [#] · cover-play · title · album · duration · status + heart + menu
const GRID = {
  base: "grid-cols-[44px_minmax(0,1fr)_132px] sm:grid-cols-[44px_minmax(0,1fr)_48px_132px] md:grid-cols-[40px_minmax(0,1fr)_48px_216px]",
  album: "lg:grid-cols-[40px_minmax(0,2fr)_minmax(0,1.4fr)_56px_216px]",
  noAlbum: "lg:grid-cols-[40px_minmax(0,1fr)_56px_216px]",
  numberedAlbum: "lg:grid-cols-[32px_40px_minmax(0,2fr)_minmax(0,1.4fr)_56px_216px]",
  numberedNoAlbum: "lg:grid-cols-[32px_40px_minmax(0,1fr)_56px_216px]",
};

export function rowGrid(numbered: boolean, showAlbum: boolean): string {
  const large = numbered && showAlbum ? GRID.numberedAlbum : numbered ? GRID.numberedNoAlbum : showAlbum ? GRID.album : GRID.noAlbum;
  return `${GRID.base} ${large}`;
}

const sameTitle = (a: string, b: string) => a.trim().toLocaleLowerCase() === b.trim().toLocaleLowerCase();

/**
 * The one track row, used everywhere: 56px, the cover doubles as the play
 * button (disabled when there is nothing to play, "Preview 30s" when only a
 * preview), title with "artist · album" (or "Single"), an album column on
 * L/XL, duration, the fixed-width status slot (the file state), the heart and
 * the "⋯" menu (also a right click or a long press on the row). Every state
 * comes from the server. No provider chips: the sources
 * are in the title's tooltip and on the track page.
 */
export const TrackRow = memo(function TrackRow({
  track,
  number = null,
  showCover = true,
  showAlbum = true,
  albumInSubtitle = true,
  queue,
  statusOverride,
  retry,
  onRemove,
}: TrackRowProps) {
  const { t } = useI18n();
  const { label } = usePublicProviders();
  const { track: current, playing, play } = usePlayer();
  const downloadState = useTrackDownload(track, statusOverride, retry);
  const { slot, modes, download, busy, disabledReason, favoriteQueued, libraryId, approval } = downloadState;
  const heart = useTrackFavorite(track, slot, libraryId, favoriteQueued);

  const isCurrent = !!current && current.provider === track.provider && current.provider_id === track.provider_id;
  const isPlaying = isCurrent && playing;
  // A playback without its URL (dropped from the offline copy) cannot play.
  const playback = isPlayable(track) ? (track.playback?.type ?? "none") : "none";
  const numbered = number !== null && showCover;
  const single = !track.album || sameTitle(track.album, track.title);
  const albumText = single ? t("row.single") : track.album;
  const sources = useMemo(
    () => (track.providers?.length ? t("row.sources", { providers: track.providers.map(label).join(", ") }) : undefined),
    [track.providers, label, t],
  );
  const hasLink = !!track.provider && !!track.provider_id;
  const duration = track.duration_ms > 0 ? formatDuration(track.duration_ms) : "";

  return (
    <TrackMenu track={track} download={downloadState} heart={heart} onRemove={onRemove}>
      {({ surface, button }) => (
        <div
          {...surface}
          className={cn(
            "group grid h-14 items-center gap-3 rounded-md px-2 transition-colors hover:bg-muted/50 [contain-intrinsic-size:auto_56px] [content-visibility:auto]",
            rowGrid(number !== null && showCover, showAlbum),
            isCurrent && "bg-primary/10",
            surface.className,
          )}
        >
          {numbered && <span className="hidden text-right text-sm tabular-nums text-muted-foreground lg:block">{number}</span>}

          <PlayCell
            track={track}
            number={showCover ? null : number}
            playback={playback}
            isPlaying={isPlaying}
            onPlay={() => play(toPlayerTrack(track), queue)}
          />

          <div className="min-w-0">
            {hasLink ? (
              <Link
                to="/track/$provider/$id"
                params={{ provider: track.provider, id: track.provider_id }}
                title={sources}
                // -my-3/py-3: a 44px-tall target without changing the row's layout.
                className={cn("relative -my-3 block truncate py-3 text-sm font-medium hover:underline", isCurrent && "text-primary")}
              >
                {track.title}
              </Link>
            ) : (
              <span className="block truncate text-sm font-medium">{track.title}</span>
            )}
            <p className="truncate text-[13px] text-muted-foreground">
              {playback === "preview" && <PreviewLabel />}
              {track.artist}
              {albumInSubtitle && albumText && <span className={showAlbum ? "lg:hidden" : undefined}> · {albumText}</span>}
              {duration && <span className="sm:hidden"> · {duration}</span>}
            </p>
          </div>

          {showAlbum && (
            <span className="hidden min-w-0 truncate text-sm text-muted-foreground lg:block">
              {!single && track.album_provider_id ? (
                <Link to="/album/$provider/$id" params={{ provider: track.provider, id: track.album_provider_id }} className="hover:underline">
                  {track.album}
                </Link>
              ) : (
                albumText
              )}
            </span>
          )}

          <span className="hidden text-right text-[13px] tabular-nums text-muted-foreground sm:block">{duration}</span>

          <div className="flex items-center justify-end">
            <StatusSlot state={slot} title={track.title} modes={modes} onDownload={download} busy={busy} disabledReason={disabledReason} approval={approval} />
            <FavoriteHeart state={heart} title={track.title} />
            {button}
          </div>
        </div>
      )}
    </TrackMenu>
  );
});

/** "Preview 30s": the row only plays a preview (server-decided playback). */
function PreviewLabel() {
  const { t } = useI18n();
  return (
    <span className="mr-1.5 inline-block rounded-xs border border-border px-1 align-[1px] text-[11px] leading-4 text-muted-foreground">
      {t("row.preview")}
    </span>
  );
}

function PlayCell({
  track,
  number,
  playback,
  isPlaying,
  onPlay,
}: {
  track: Track;
  number: number | null;
  playback: "full" | "preview" | "none";
  isPlaying: boolean;
  onPlay: () => void;
}) {
  const { t } = useI18n();
  const face =
    number !== null ? (
      <span className="text-sm tabular-nums text-muted-foreground">{number}</span>
    ) : (
      <CoverImage src={track.cover_url} alt="" type="track" className="size-10" />
    );

  if (playback === "none") {
    return (
      <button
        type="button"
        disabled
        aria-label={t("row.notPlayable", { title: track.title })}
        className="grid size-11 cursor-not-allowed place-items-center opacity-70 md:size-10"
      >
        {face}
      </button>
    );
  }

  const Icon = isPlaying ? Pause : Play;
  const label = isPlaying
    ? t("row.pause", { title: track.title })
    : t(playback === "preview" ? "row.previewOf" : "row.play", { title: track.title });
  return (
    <button
      type="button"
      onClick={onPlay}
      aria-label={label}
      className="relative grid size-11 place-items-center overflow-hidden rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:size-10"
    >
      {face}
      <span
        aria-hidden
        className={cn(
          "absolute inset-0 grid place-items-center bg-black/45 text-white transition-opacity",
          // Always visible on touch screens (no hover) and for the playing track.
          isPlaying ? "opacity-100" : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100",
        )}
      >
        <Icon className="size-4" fill="currentColor" />
      </span>
    </button>
  );
}

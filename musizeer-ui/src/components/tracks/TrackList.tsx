import { useMemo } from "react";
import { Clock } from "lucide-react";
import { toPlayerTrack } from "@/contexts/PlayerContext";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { DownloadStatus, Track } from "@/types";
import { TrackRow, rowGrid } from "./TrackRow";

export interface TrackListProps {
  tracks: Track[];
  /** Number the rows: 1..n, or each track's own number (album pages). */
  numbering?: "index" | "track";
  showCover?: boolean;
  showAlbum?: boolean;
  /** The album in each row's subtitle when there is no album column (off on an album page). */
  albumInSubtitle?: boolean;
  /** Column headings on L/XL. */
  header?: boolean;
  statusOf?: (track: Track) => DownloadStatus | null | undefined;
  className?: string;
  label?: string;
}

const key = (t: Track, i: number) => `${t.provider}:${t.provider_id || t.internal_id}:${i}`;

/**
 * Rows of TrackRow sharing one play queue (the list itself), with optional
 * column headings aligned on the same grid.
 */
export function TrackList({
  tracks,
  numbering,
  showCover = true,
  showAlbum = true,
  albumInSubtitle = true,
  header = false,
  statusOf,
  className,
  label,
}: TrackListProps) {
  const { t } = useI18n();
  const queue = useMemo(() => tracks.map(toPlayerTrack), [tracks]);
  const numbered = numbering !== undefined;

  return (
    <div className={className}>
      {header && (
        <div
          aria-hidden
          className={cn(
            "hidden h-9 items-center gap-3 border-b px-2 text-xs font-medium uppercase tracking-wide text-muted-foreground lg:grid",
            rowGrid(numbered && showCover, showAlbum),
          )}
        >
          {numbered && showCover && <span className="text-right">{t("row.colNumber")}</span>}
          <span className={numbered && !showCover ? "text-center" : undefined}>{numbered && !showCover ? t("row.colNumber") : ""}</span>
          <span>{t("row.colTitle")}</span>
          {showAlbum && <span>{t("row.colAlbum")}</span>}
          <span className="flex justify-end" title={t("row.colDuration")}>
            <Clock aria-hidden className="size-4" />
          </span>
          <span>{t("row.colStatus")}</span>
        </div>
      )}
      <ol aria-label={label} className="space-y-0.5">
        {tracks.map((track, i) => (
          <li key={key(track, i)}>
            <TrackRow
              track={track}
              number={numbered ? (numbering === "track" ? (track.track_number ?? i + 1) : i + 1) : null}
              showCover={showCover}
              showAlbum={showAlbum}
              albumInSubtitle={albumInSubtitle}
              queue={queue}
              statusOverride={statusOf?.(track)}
            />
          </li>
        ))}
      </ol>
    </div>
  );
}

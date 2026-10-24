import { useMemo } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { toPlayerTrack } from "@/contexts/PlayerContext";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { TrackRow } from "@/components/tracks/TrackRow";
import type { Track } from "@/types";

interface PlaylistTrackListProps {
  tracks: Track[];
  label: string;
  /** Own playlists only: move a track up or down, and take it out (in the row's menu). A followed playlist is read-only. */
  editable: boolean;
  /** Moves the track at `index` by `step` (-1 up, +1 down). */
  onMove: (index: number, step: -1 | 1) => void;
  onRemove: (track: Track) => void;
  /** An edit is on its way: the controls wait for it. */
  busy: boolean;
}

const CONTROL =
  "grid size-11 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40 md:size-9";

/**
 * The tracks of a playlist in their order: the usual rows with their usual
 * menu, plus the controls to move each one when the playlist is the viewer's
 * to edit (and "Remove from this playlist" in its menu).
 */
export function PlaylistTrackList({ tracks, label, editable, onMove, onRemove, busy }: PlaylistTrackListProps) {
  const { t } = useI18n();
  const queue = useMemo(() => tracks.map(toPlayerTrack), [tracks]);

  return (
    <ol aria-label={label} className="space-y-0.5">
      {tracks.map((track, index) => (
        <li key={track.library_id ?? `${track.provider}:${track.provider_id}`} className="flex items-center gap-1">
          <div className="min-w-0 flex-1">
            <TrackRow track={track} number={index + 1} queue={queue} onRemove={editable && track.library_id ? () => onRemove(track) : undefined} />
          </div>
          {editable && (
            <div className={cn("flex shrink-0 items-center")}>
              <button type="button" className={CONTROL} disabled={busy || index === 0} onClick={() => onMove(index, -1)} aria-label={t("playlists.moveUp", { title: track.title })}>
                <ArrowUp aria-hidden className="size-4" />
              </button>
              <button
                type="button"
                className={CONTROL}
                disabled={busy || index === tracks.length - 1}
                onClick={() => onMove(index, 1)}
                aria-label={t("playlists.moveDown", { title: track.title })}
              >
                <ArrowDown aria-hidden className="size-4" />
              </button>
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

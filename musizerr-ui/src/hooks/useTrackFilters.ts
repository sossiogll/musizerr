import { useMemo, useState } from "react";
import type { FilterChip } from "@/components/ListToolbar";
import { useDownloadStates } from "@/hooks/useDownloadStates";
import { useI18n, type MessageKey } from "@/i18n";
import type { DownloadPhase } from "@/lib/api-client";
import type { Track } from "@/types";

type Playback = "" | "full" | "preview" | "none";
type LibraryFilter = "" | "in_library" | "missing" | "review";

const PLAYBACK: { value: Exclude<Playback, "">; labelKey: MessageKey }[] = [
  { value: "full", labelKey: "album.playbackFull" },
  { value: "preview", labelKey: "album.playbackPreview" },
  { value: "none", labelKey: "album.playbackNone" },
];

const LIBRARY: { value: Exclude<LibraryFilter, "">; labelKey: MessageKey }[] = [
  { value: "in_library", labelKey: "trackFilters.inLibrary" },
  { value: "missing", labelKey: "trackFilters.missing" },
  { value: "review", labelKey: "trackFilters.toReview" },
];

/** Which library filter a track's aggregated state (phase) falls in. */
function libraryBucket(phase: DownloadPhase | undefined): Exclude<LibraryFilter, ""> {
  if (phase === "done") return "in_library";
  if (phase === "failed" || phase === "review") return "review";
  return "missing";
}

/**
 * Text, playback and library-state filters over a list of tracks (an album,
 * a playlist): the state and the chips for ListToolbar, and the filtered
 * list. The library state comes from each track's `download_status` and the
 * phases the backend declares for it; the playback from its `playback`.
 */
export function useTrackFilters(tracks: Track[]) {
  const { t } = useI18n();
  const { meta } = useDownloadStates();
  const [query, setQuery] = useState("");
  const [playback, setPlayback] = useState<Playback>("");
  const [library, setLibrary] = useState<LibraryFilter>("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tracks.filter((track) => {
      if (q && !track.title.toLowerCase().includes(q) && !track.artist.toLowerCase().includes(q)) return false;
      if (playback) {
        if ((track.playback?.type ?? "none") !== playback) return false;
      }
      if (library) {
        const phase = track.download_status ? meta(track.download_status)?.phase : undefined;
        if (libraryBucket(phase) !== library) return false;
      }
      return true;
    });
  }, [tracks, query, playback, library, meta]);

  const chips = useMemo<FilterChip[]>(
    () => [
      {
        key: "library",
        label: t("trackFilters.allStates"),
        options: LIBRARY.map((o) => ({ value: o.value, label: t(o.labelKey) })),
        value: library,
        onChange: (v) => setLibrary(v as LibraryFilter),
      },
      {
        key: "playback",
        label: t("album.allPlayback"),
        options: PLAYBACK.map((o) => ({ value: o.value, label: t(o.labelKey) })),
        value: playback,
        onChange: (v) => setPlayback(v as Playback),
      },
    ],
    [library, playback, t],
  );

  return {
    filtered,
    chips,
    query,
    setQuery,
    /** A filter is narrowing the list (for the empty-state wording). */
    active: !!(query.trim() || playback || library),
  };
}

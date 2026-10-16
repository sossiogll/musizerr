import type { DownloadStateMeta } from "@/lib/api-client";
import type { Track } from "@/types";

/** The download-state vocabulary as GET /config/download-states sends it. */
export const DOWNLOAD_STATES: DownloadStateMeta[] = [
  { key: "downloaded", label: "In libreria", label_key: "download_states.downloaded", tone: "success", phase: "done" },
  { key: "dispatched", label: "Delegato", label_key: "download_states.dispatched", tone: "info", phase: "active" },
  { key: "importing", label: "Importazione", label_key: "download_states.importing", tone: "info", phase: "active" },
  { key: "downloading", label: "Scaricamento", label_key: "download_states.downloading", tone: "info", phase: "active" },
  { key: "conflict", label: "Conflitto", label_key: "download_states.conflict", tone: "warning", phase: "review" },
  { key: "retrying", label: "Nuovo tentativo", label_key: "download_states.retrying", tone: "warning", phase: "queued" },
  { key: "pending", label: "In coda", label_key: "download_states.pending", tone: "neutral", phase: "queued" },
  { key: "failed", label: "Fallito", label_key: "download_states.failed", tone: "danger", phase: "failed" },
];

export function makeTrack(overrides: Partial<Track> = {}): Track {
  return {
    kind: "track",
    internal_id: "t1",
    title: "Song A",
    artist: "Artist A",
    album: "Album A",
    albums: [],
    artists: [],
    duration_ms: 200_000,
    cover_url: null,
    preview_url: "https://cdn.example/preview.mp3",
    stream_url: null,
    provider: "prov",
    provider_id: "p1",
    artist_provider_id: "ar1",
    album_provider_id: "al1",
    year: 2020,
    external_ids: [],
    providers: ["prov"],
    download_status: null,
    in_library: false,
    library_id: null,
    favorite: false,
    favorite_pending: false,
    // As the server sends it: a preview, nothing in the library.
    playback: { type: "preview", source: "provider" },
    ...overrides,
  };
}

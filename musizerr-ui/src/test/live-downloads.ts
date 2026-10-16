/**
 * Real responses of the API and real progress events, captured from a running
 * instance on 29 Sep 2026 (usernames scrubbed). The status-precedence tests
 * run on these shapes, not on hand-written ones:
 *
 * - `download_already_in_library`: POST /download/track from a second
 *   provider's row of a track in the library since 7 Sep;
 * - `download_attached`: the same POST on a track with an open delegation;
 * - `downloads_done_item_with_newer_dispatch`: GET /downloads?phase=done, a
 *   track in the library whose latest attempt is "dispatched";
 * - `downloads_active_item` / `downloads_failed_item`: the other phases;
 * - `event_*`: what services.progress.publish_download_progress sends.
 */
import type { DownloadResponse, ProgressEvent, TrackDownload } from "@/types";

export const LIVE = {
  "download_already_in_library": {
    "status": "already_in_library",
    "download_id": null,
    "library_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
    "track": {
      "kind": "track",
      "internal_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
      "title": "TAXI SULLA LUNA",
      "artist": "Tony Effe",
      "album": "TAXI SULLA LUNA",
      "duration_ms": 143000,
      "cover_url": "https://cdn.example.test/media",
      "preview_url": "https://cdn.example.test/media",
      "stream_url": null,
      "provider": "beta",
      "provider_id": "2321004155",
      "artist_provider_id": "12726119",
      "album_provider_id": "451241145",
      "year": 2023,
      "track_number": 1,
      "disc_number": 1,
      "providers": [],
      "external_ids": [
        {
          "type": "beta_id",
          "value": "2321004155",
          "provider": "beta"
        },
        {
          "type": "isrc",
          "value": "ITUM72300873"
        }
      ],
      "albums": [],
      "artists": [
        {
          "name": "Tony Effe",
          "provider": "beta",
          "provider_id": "12726119"
        },
        {
          "name": "Emma",
          "provider": "beta",
          "provider_id": "56640"
        },
        {
          "name": "Takagi & Ketra",
          "provider": "beta",
          "provider_id": "10686109"
        }
      ]
    }
  },
  "download_attached": {
    "status": "attached",
    "download_id": "8a53a4a9-8faf-4919-ba2a-27c9df82ea22",
    "library_id": "56170493-cfcc-48b5-9b3d-5c36bba98f2a",
    "track": {
      "kind": "track",
      "internal_id": "56170493-cfcc-48b5-9b3d-5c36bba98f2a",
      "title": "Dream On",
      "artist": "Aerosmith",
      "album": "Aerosmith",
      "duration_ms": 266960,
      "cover_url": "https://cdn.example.test/media",
      "preview_url": null,
      "stream_url": null,
      "provider": "alpha",
      "provider_id": "5MxNLUsfh7uzROypsoO5qe",
      "artist_provider_id": "7Ey4PD4MYsKc5I2dolUwbH",
      "album_provider_id": "19lEZSnCCbVEkKchoPQWDZ",
      "year": 1973,
      "track_number": 3,
      "disc_number": 1,
      "providers": [],
      "external_ids": [
        {
          "type": "alpha_id",
          "value": "5MxNLUsfh7uzROypsoO5qe",
          "provider": "alpha"
        },
        {
          "type": "isrc",
          "value": "USSM10011897"
        }
      ],
      "albums": [],
      "artists": [
        {
          "name": "Aerosmith",
          "provider": "alpha",
          "provider_id": "7Ey4PD4MYsKc5I2dolUwbH"
        }
      ]
    }
  },
  "downloads_done_item_with_newer_dispatch": {
    "track_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
    "status": "downloaded",
    "provider_used": "fetcher",
    "file_path": "Tony Effe, Emma, Takagi & Ketra/ICON (Unknown)/Tony Effe, Emma, Takagi & Ketra - ICON - 00 - TAXI SULLA LUNA (feat. Emma & Takagi & Ketra).ogg",
    "requested_by": "admin",
    "track": {
      "kind": "track",
      "internal_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
      "title": "TAXI SULLA LUNA (feat. Emma & Takagi & Ketra)",
      "artist": "Tony Effe, Emma, Takagi & Ketra",
      "album": "ICON",
      "duration_ms": 143506,
      "cover_url": "https://cdn.example.test/media",
      "preview_url": "https://cdn.example.test/media",
      "stream_url": "/api/stream/edf7efac-b77a-4bea-9ba1-79a0e95477d6",
      "provider": "alpha",
      "provider_id": "2MHGhfiCXvfeo3ZQ3RyG0B",
      "artist_provider_id": "6CKch2otN4SPznHf9ms5JF",
      "album_provider_id": "0zGirhBBoAjHz8t37u1IRW",
      "year": 2024,
      "track_number": 17,
      "disc_number": 1,
      "providers": [
        "alpha",
        "beta",
        "mediaserver"
      ],
      "external_ids": [
        {
          "type": "alpha_id",
          "value": "2MHGhfiCXvfeo3ZQ3RyG0B",
          "provider": "alpha"
        },
        {
          "type": "isrc",
          "value": "ITUM72300873"
        },
        {
          "type": "beta_id",
          "value": "2321004155",
          "provider": "beta"
        },
        {
          "type": "mediaserver_sync_id",
          "value": "6re3tqZtTwjhHbdHxxCpLt"
        }
      ],
      "albums": [
        {
          "name": "ICON",
          "provider": "alpha",
          "provider_id": "0zGirhBBoAjHz8t37u1IRW"
        },
        {
          "name": "TAXI SULLA LUNA",
          "provider": "beta",
          "provider_id": "451241145"
        }
      ],
      "artists": [
        {
          "name": "Tony Effe",
          "provider": "alpha",
          "provider_id": "6CKch2otN4SPznHf9ms5JF"
        },
        {
          "name": "Emma",
          "provider": "alpha",
          "provider_id": "0gzwXezN4IUHAwLufA6YcX"
        },
        {
          "name": "Takagi & Ketra",
          "provider": "alpha",
          "provider_id": "76UCIJTB0jcJvBaL0CdIqx"
        }
      ],
      "library_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
      "download_status": "downloaded",
      "in_library": true,
      "favorite": true,
      "favorite_pending": false,
      "playback": {
        "type": "full",
        "source": "library"
      }
    },
    "attempts": [
      {
        "download_id": "0776d53c-2843-48bd-9b38-82f429dabae1",
        "status": "dispatched",
        "provider_used": "delegate",
        "file_path": null,
        "error_code": null,
        "error_message": null,
        "retry_count": 0,
        "started_at": "2026-09-28T22:30:13.508371",
        "completed_at": null
      },
      {
        "download_id": "4a007ebe-8312-4d79-9bb4-1541b8e3fe0f",
        "status": "downloaded",
        "provider_used": "fetcher",
        "file_path": "Tony Effe, Emma, Takagi & Ketra/ICON (Unknown)/Tony Effe, Emma, Takagi & Ketra - ICON - 00 - TAXI SULLA LUNA (feat. Emma & Takagi & Ketra).ogg",
        "error_code": null,
        "error_message": null,
        "retry_count": 0,
        "started_at": "2026-09-07T12:42:10.123788",
        "completed_at": "2026-09-07T12:42:32.691119"
      }
    ]
  },
  "downloads_active_item": {
    "track_id": "56170493-cfcc-48b5-9b3d-5c36bba98f2a",
    "status": "dispatched",
    "provider_used": null,
    "file_path": null,
    "requested_by": "admin",
    "track": {
      "kind": "track",
      "internal_id": "56170493-cfcc-48b5-9b3d-5c36bba98f2a",
      "title": "Dream On",
      "artist": "Aerosmith",
      "album": "Aerosmith",
      "duration_ms": 266960,
      "cover_url": "https://cdn.example.test/media",
      "preview_url": null,
      "stream_url": null,
      "provider": "alpha",
      "provider_id": "5MxNLUsfh7uzROypsoO5qe",
      "artist_provider_id": "7Ey4PD4MYsKc5I2dolUwbH",
      "album_provider_id": "19lEZSnCCbVEkKchoPQWDZ",
      "year": 1973,
      "track_number": 3,
      "disc_number": 1,
      "providers": [
        "alpha"
      ],
      "external_ids": [
        {
          "type": "alpha_id",
          "value": "5MxNLUsfh7uzROypsoO5qe",
          "provider": "alpha"
        },
        {
          "type": "isrc",
          "value": "USSM10011897"
        },
        {
          "type": "alpha_id",
          "value": "1xsYj84j7hUDDnTTerGWlH",
          "provider": "alpha"
        }
      ],
      "albums": [
        {
          "name": "Aerosmith",
          "provider": "alpha",
          "provider_id": "1RDVdxPUaqC4o7Rkm8LoOE"
        }
      ],
      "artists": [
        {
          "name": "Aerosmith",
          "provider": "alpha",
          "provider_id": "7Ey4PD4MYsKc5I2dolUwbH"
        }
      ],
      "library_id": "56170493-cfcc-48b5-9b3d-5c36bba98f2a",
      "download_status": "dispatched",
      "in_library": false,
      "favorite": false,
      "favorite_pending": true,
      "playback": {
        "type": "none",
        "source": null
      }
    },
    "attempts": [
      {
        "download_id": "8a53a4a9-8faf-4919-ba2a-27c9df82ea22",
        "status": "dispatched",
        "provider_used": "delegate",
        "file_path": null,
        "error_code": null,
        "error_message": null,
        "retry_count": 0,
        "started_at": "2026-09-28T23:04:38.329352",
        "completed_at": null
      },
      {
        "download_id": "62b27ca8-31f0-4705-83fb-45fea5d08d15",
        "status": "failed",
        "provider_used": null,
        "file_path": null,
        "error_code": "download_provider_not_found",
        "error_message": "The download provider had nothing matching this track.",
        "error_detail": "No provider could download 'Dream On' by 'Aerosmith'",
        "retry_count": 0,
        "started_at": "2026-09-10T19:07:00.871696",
        "completed_at": "2026-09-10T19:07:00.871696"
      },
      {
        "download_id": "3ba1d2fa-585f-45ac-9c13-6c6769a38017",
        "status": "failed",
        "provider_used": "delegate",
        "file_path": null,
        "error_code": "download_provider_timeout",
        "error_message": "The download provider took too long to answer.",
        "error_detail": "delegation timed out (max-age exceeded)",
        "retry_count": 0,
        "started_at": "2026-09-08T15:58:40.836478",
        "completed_at": "2026-09-19T00:07:26.616918"
      }
    ]
  },
  "downloads_failed_item": {
    "track_id": "4b3637c5-cb50-4054-90c9-dc702a63a6d6",
    "status": "failed",
    "provider_used": null,
    "file_path": null,
    "requested_by": "admin",
    "track": {
      "kind": "track",
      "internal_id": "4b3637c5-cb50-4054-90c9-dc702a63a6d6",
      "title": "CHIAGNE (feat. Lazza & Takagi & Ketra)",
      "artist": "Geolier, Lazza, Takagi & Ketra",
      "album": "CHIAGNE (feat. Lazza & Takagi & Ketra)",
      "duration_ms": 179863,
      "cover_url": "https://cdn.example.test/media",
      "preview_url": "https://cdn.example.test/media",
      "stream_url": null,
      "provider": "alpha",
      "provider_id": "36EFgeHW1tOUyMAhZ6cjfD",
      "artist_provider_id": "27LlKWxS3KXW7RRAxN5S8s",
      "album_provider_id": "4sWzb70s3E66veAlTi0hSr",
      "year": 2022,
      "track_number": 1,
      "disc_number": 1,
      "providers": [
        "alpha",
        "beta"
      ],
      "external_ids": [
        {
          "type": "alpha_id",
          "value": "36EFgeHW1tOUyMAhZ6cjfD",
          "provider": "alpha"
        },
        {
          "type": "isrc",
          "value": "ITB002200834"
        },
        {
          "type": "beta_id",
          "value": "1986830357",
          "provider": "beta"
        }
      ],
      "albums": [
        {
          "name": "CHIAGNE (feat. Lazza & Takagi & Ketra)",
          "provider": "alpha",
          "provider_id": "4sWzb70s3E66veAlTi0hSr"
        }
      ],
      "artists": [
        {
          "name": "Geolier",
          "provider": "alpha",
          "provider_id": "27LlKWxS3KXW7RRAxN5S8s"
        },
        {
          "name": "Lazza",
          "provider": "alpha",
          "provider_id": "0jdNdfi4vAuVi7a6cPDFBM"
        },
        {
          "name": "Takagi & Ketra",
          "provider": "alpha",
          "provider_id": "76UCIJTB0jcJvBaL0CdIqx"
        }
      ],
      "library_id": "4b3637c5-cb50-4054-90c9-dc702a63a6d6",
      "download_status": "failed",
      "in_library": false,
      "favorite": false,
      "favorite_pending": false,
      "playback": {
        "type": "preview",
        "source": "provider"
      }
    },
    "attempts": [
      {
        "download_id": "1d6f2b77-680a-4efc-ba03-906ca508817d",
        "status": "failed",
        "provider_used": null,
        "file_path": null,
        "error_code": "download_provider_not_found",
        "error_message": "The download provider had nothing matching this track.",
        "error_detail": "No provider could download 'CHIAGNE (feat. Lazza & Takagi & Ketra)' by 'Geolier'",
        "retry_count": 0,
        "started_at": "2026-09-28T22:30:05.125679",
        "completed_at": "2026-09-28T22:30:05.125679"
      }
    ]
  },
  "downloads_active_phase_total_for_owned": 0,
  "event_dispatched_on_owned_track": {
    "download_id": "0776d53c-2843-48bd-9b38-82f429dabae1",
    "status": "dispatched",
    "track_title": "TAXI SULLA LUNA",
    "track_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
    "file_state": "downloaded",
    "phase": "done"
  },
  "event_failed_on_owned_track": {
    "download_id": "0776d53c-2843-48bd-9b38-82f429dabae1",
    "status": "failed",
    "track_title": "TAXI SULLA LUNA",
    "track_id": "edf7efac-b77a-4bea-9ba1-79a0e95477d6",
    "file_state": "downloaded",
    "phase": "done"
  },
  "event_downloading_on_missing_track": {
    "download_id": "8a53a4a9-8faf-4919-ba2a-27c9df82ea22",
    "status": "downloading",
    "track_title": "Dream On",
    "track_id": "56170493-cfcc-48b5-9b3d-5c36bba98f2a",
    "file_state": "dispatched",
    "phase": "active",
    "step": "downloading",
    "provider_attempting": "delegate",
    "method": "fuzzy",
    "retry": 0
  }
} as const;

export const alreadyInLibrary = LIVE.download_already_in_library as unknown as DownloadResponse;
export const attached = LIVE.download_attached as unknown as DownloadResponse;
export const doneItemWithNewerDispatch = LIVE.downloads_done_item_with_newer_dispatch as unknown as TrackDownload;
export const activeItem = LIVE.downloads_active_item as unknown as TrackDownload;
export const failedItem = LIVE.downloads_failed_item as unknown as TrackDownload;
export const dispatchedOnOwned = LIVE.event_dispatched_on_owned_track as unknown as ProgressEvent;
export const failedOnOwned = LIVE.event_failed_on_owned_track as unknown as ProgressEvent;
export const downloadingOnMissing = LIVE.event_downloading_on_missing_track as unknown as ProgressEvent;

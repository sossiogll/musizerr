# Playlists

A playlist lives in Musizerr, belongs to one user and is **private** (its owner
sees it, nobody else: another user's playlist is a 404). A playlist is **hybrid**:
kept by Musizerr, mirrored on the music server, and able to follow a playlist of
a metadata provider.

| Kind | What it is | Edited by |
| --- | --- | --- |
| `own` | A playlist the user made: name, description, tracks in an order | its owner (add, remove, reorder, rename) |
| `followed` | A mirror of a playlist of a metadata provider (`source_provider`, `source_id`) | nobody: read-only; it follows its source |

Routes, bodies and errors: `app/openapi.yaml`, tag `playlists`. Capability for the
UI: `playlists` of `GET /auth/account/status` (on for users, admins and open
mode; off for guests).

## Tables

`playlists` (one row per playlist; unique per user and source, so a source is
followed once), `playlist_tracks` (the tracks in order, positions contiguous from
0, a track at most once per playlist), `playlist_syncs` (the copy on a sync
provider: its id there and a hash of what was last pushed; no foreign key to the
playlist, so the row outlives it until the copy is deleted). Migration
`0038_playlists`. Deleting a user deletes all of these.

Every edit of one playlist runs under its lock (`db.locks.xact_lock`), so two
requests never interleave positions or both slip under the cap
(`tests/test_concurrency.py` races both on PostgreSQL).

## The cap

`playlist_max_tracks` (settings catalogue, group `playlists`, system scope, default
500): the most tracks any playlist may hold. A batch that would pass it is refused
whole (409 `playlist_full`, with `max_tracks`, `track_count`, `adding`); a
playlist longer than the cap cannot be followed; a followed playlist that grows
past it keeps its tracks and records `refresh_error_code: playlist_full` until the
cap is raised.

## Own playlists

`POST /playlists`, `PATCH /playlists/{id}`, `POST /playlists/{id}/tracks` (items
are `{track_id}` — the `library_id` of a listed track — or `{provider,
provider_id}`, read from the provider), `DELETE /playlists/{id}/tracks/{track_id}`,
`PUT /playlists/{id}/tracks/order` (must list exactly the playlist's tracks, once
each: 422 `playlist_invalid_order` otherwise, so a client with an old list never
loses a track), `POST /playlists/{id}/duplicate`.

## Following a playlist

`POST /playlists/follow {provider, id}`:

1. reads the playlist from its provider (with the caller's account);
2. keeps its tracks (read-only) and its `snapshot_id`;
3. asks to download the tracks **through the approval policy**
   (`download_requests.request_download`, kind `playlist`), as the caller and
   with `favorite_for=None`: **the tracks do not become favorites**. The answer
   says what the request came to (`download.status`: `queued`, `pending_approval`,
   `none`, `error`); a request that waits does not undo the follow.

Following twice is one follow (200, `created: false`, nothing asked again). The
tracks that were not in the library when the playlist asked are flagged
(`playlist_tracks.download_requested`): they are the ones "delete the downloaded
tracks" may take with the playlist.

A followed playlist is **read-only** (409 `playlist_read_only` for every edit but
`auto_download`); **Duplicate as mine** (`POST /playlists/{id}/duplicate`) makes an
own, editable copy with the tracks it has now.

### Providers that only share some playlists

A metadata provider whose service hands out the tracks of a playlist only for the
ones the connected account owns or collaborates on declares it
(`MetadataProvider.restricts_playlist_tracks()` → capability
`playlist_tracks_restricted`, listed in `GET /providers/public`); the UI says so
next to "Follow". When the provider does not give the tracks of a playlist it
answers with `Playlist.tracks_unavailable` (`access_restricted` / `error`): the
playlist is followed all the same, with that reason and no tracks to download,
and the page shows it. Nothing in the host names a provider.

### Watching the source

The routine `refresh_followed_playlists` (hourly by default, "Run now" from the
routines page; `POST /playlists/{id}/refresh` does it for one playlist) asks each
followed playlist's provider for its version marker
(`MetadataProvider.get_playlist_snapshot`, e.g. a snapshot id) with the **owner's
accounts**. Only when it changed (or the provider has no marker and the tracks
differ) are the tracks read again: the tracks the source gained are added (and
downloaded when `auto_download`, through the approval policy: a request that
waits is a request in Downloads › Requests), the ones it lost leave the
playlist (their files stay), the order is the source's. A source that cannot be
read is recorded on the playlist (`refresh_error_code`) and makes the run an
`error` with that code (`metadata_provider_*`, `playlist_source_not_found`,
`playlist_full`); a playlist whose provider is not installed or whose owner has
no account on it is skipped, not a failure. When something changed, the mirroring
is started.

## Mirroring on the music server

A sync provider that keeps playlists declares it
(`SyncProvider.supports_playlists()`) and implements `create_playlist(name,
sync_ids, session)`, `update_playlist(remote_id, name, sync_ids, session)` (a
replacement: the whole content) and `delete_playlist(remote_id, session)`, raising
`musizerr_sdk.errors.RemotePlaylistGone` when the server no longer has it. The
default is "not supported": a provider that does not implement them is never
touched.

The routine `sync_playlists` (hourly by default; it **follows**
`sync_library_index`, so a track downloaded meanwhile has an id on the server
first; an edit of a playlist and the end of a followed playlist's refresh start
it right away) does, for each such provider and each user with playlists:

- uses the **user's own account** (`own_store`): never a shared one; a user
  without an account on that server is skipped (`skipped_no_account`);
- sends only the tracks the server holds (they carry the provider's id from the
  library index); the others (`tracks_not_indexed`) join on a later run;
- remembers what it sent (`content_hash` of the name and the ordered ids), so an
  unchanged playlist costs no call;
- deletes the copy of a playlist the user deleted, and creates again a copy the
  user deleted on the server;
- reports one failure per provider with a stable code
  (`sync_provider_unreachable`, …): the run is an `error`, never `ok` with a
  warning; one user or provider failing does not stop the others.

The playlist is the master: edits made to the copy on the server are overwritten
by the next push of a changed playlist.

## Removing a playlist

`DELETE /playlists/{id}?delete_tracks=false` deletes an own playlist or unfollows a
followed one. **By default only the playlist goes**; the UI asks, for a followed
playlist, whether to also delete the downloaded tracks (off by default).
With `delete_tracks=true` the files that were downloaded for the playlist
(`download_requested`) are deleted **in the background** (task
`workers.maintenance.delete_playlist_tracks`, on `maintenance`), and only when,
checked again by the task, no other playlist holds the track, no user has it as a
favorite, and every file of it was downloaded by Musizerr for this user (never a
file the library scan found, never one somebody else asked for). They go through
the storage provider; the sync provider is told to rescan and the library index
drops what it no longer lists. The answer says how many tracks were scheduled
(`tracks_scheduled`).

## What is not done (yet)

- Playlists are private: no sharing, no collaborative editing.
- The mirror is one way (Musizerr → server); playlists made on the server are not
  imported.
- A pending approval request for a playlist's tracks holds the tracks it was made
  with; tracks the source gains meanwhile are asked for by the next request.

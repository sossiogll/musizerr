# ADR-0005: Identity data has one home, and reading never writes it

**Status:** Accepted
**Date:** 2026-10-03
**Deciders:** @sossiogll (maintainer)

## Context

On 29 Sep the identity cleanup brought the twin records to 0 and the records with
more than one sync id to 6. On 2 Oct they were back to 72 and 79. Three causes:

1. **Two copies of the identifiers.** `track_identifiers` is the table the database
   resolves identity from, but `tracks.track_data` carried its own `external_ids`.
   The cleanup fixed the table and left the copy: right after it, 1487 of 1916 records
   still had Navidrome ids in the payload that the table no longer had. Every list
   built from a payload (favorites, downloads, "liked elsewhere") carried them.
2. **Reading wrote.** `annotate_tracks` indexed every "sync hit" it saw
   (`find_or_create_track` / `add_identifiers`) on every GET, with the identifiers of
   the list it was annotating. A stale id in a payload came back through that write, and
   `add_identifiers` replaces a sync id only when the incoming set does not also hold it.
3. **Coverage was a function of what had been viewed.** "Liked elsewhere" declared 1363
   and returned 1037, then 1168; a discography went from 47 to 93 tracks in the library in
   20 minutes with no download.

A fourth problem, separate but of the same kind: a favorite the user removed stayed removed
only because the imported like kept its `matched_at`.

## Decision

- **Identifiers live only in `track_identifiers`.** `tracks.track_data` never carries
  `external_ids`: `db.track_store.stored_track_data` does not write them and
  `track_data_dict` ignores them in a payload written before. A list row gets its identifiers
  from the table (`services.stored_tracks.public_track_dicts`, `db.track_store.load_track`).
  `add_identifiers` is the only writer of the table for an existing record; its input is
  evidence just gathered from a live source, never a stored payload. Migration 0025 removes the
  old copy from the rows already stored.
- **Reading is free of side effects.** Nothing in a GET writes the database. A track found on a
  sync provider during a request can play (a sync provider streams whole tracks) but it is not
  "in the library" because of that answer: the library is what the database holds, so the row,
  the album, the discography and Home all say the same thing and say it again on the next read.
- **The sync presence is written by jobs** (`services/sync_index.py`): `index_library` lists the
  sync provider's whole library (`SyncProvider.list_library`) and gives every live file a record
  with its sync id (an existing record by identity and the core matching, or a new one), then
  drops the sync ids the provider no longer lists when the record stays explained. It runs as its
  own routine, `sync_library_index` (hourly, and started right after a library scan or a likes
  sync), and the like matching follows it (see the amendment below). `index_track`
  asks about one track (the download worker, the likes download). A partial or empty listing
  drops nothing. A record with two live files keeps both ids.
- **A removed favorite is remembered by what names the song**, not by a record id
  (`RemovedFavorite`, migration 0026): its stable identifiers and, for each artist, its title
  identity (`core.matching.song_keys`). The like matching and the likes download honour it,
  whichever record the like comes back on; only the user's own heart or download clears it.
  Migration 0026 backfills it from the matched likes that have no favorite.
- **Optionally** the like is taken back on the origin provider
  (`MetadataProvider.unlike_saved_track`, setting `unlike_on_origin_providers`, off by default).
  Since migration 0030 it is each user's own choice (scope `user`) and acts only with that
  user's own connected account, never a shared or app-only one.

## Consequences

- The first run of `index_library` on a library that was only indexed by viewing creates the
  records of every file nobody had looked at (on the live data: ~3,900 on top of ~3,800).
  They are in the library, which is what the sync provider holds.
- A file the sync provider has but no job has indexed yet is not "in the library" for a few
  minutes at most (the routine, or the next scan or likes sync); a download request still
  finds it (`index_track` in the worker) instead of fetching a second copy.
- Twins (the same value as provider id on one record and sync id on another) are only
  counted by the indexer; merging them stays with `scripts/track_identity_cleanup.py`.
- An OAuth2 metadata provider now asks for the scope that lets it take a like back on a new
  connection; a token without it
  makes the optional unlike a logged, harmless failure. Since migration 0030 the provider
  declares the scope per capability (`OAuth2Config.capability_scopes`): GET /providers reports
  such a connection (`missing_permissions`), Settings › Providers asks to reconnect, and the task
  skips it with the outcome `missing_permission`.

## Amendment (3 Oct 2026): the index is a routine of its own

The index first ran inside the like matching task, behind a try/except that only logged a
warning. On 3 Oct the music server was unreachable (DNS) from 09:06 to 12:06: every hourly run
logged the failure and was recorded `ok` with `{"checked": 1019, "matched": 0}`, so the routines
page never showed it, and the first full index (3,749 → 7,675 records in 27 s) had no history or
progress of its own.

- `sync_library_index` is a routine (`core/routines.py`): its own interval (60 min, minimum 15),
  switch, run history and once-per-period claim. It runs on the `jobs` queue (`docs/workers.md`).
- Its result has one report per sync provider (files listed, records created, attached, aliased,
  stale ids dropped, files gone). A provider that cannot answer, and the listing guards (an empty
  or a partial listing), make the run `error` with a stable code (`sync_provider_unreachable`,
  `sync_provider_auth_failed`, `sync_provider_empty_listing`, `sync_provider_partial_listing`...);
  a provider that is not configured is skipped. The providers that answered are indexed anyway.
- The like matching follows it (`RoutineDef.follows`): it runs after every index run, ok or not,
  on what the database knows, and its result says how the last index ended. It never indexes.
- The library scan and the likes sync start the index (`services.routines.start_routine`); the
  matching follows.
- A long index reports its progress (phase, provider, processed/total, heartbeat) on its run,
  shown on Amministrazione › Routine.

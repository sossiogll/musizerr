# ADR-0004: URLs served by the host are never stored

**Status:** Accepted
**Date:** 2026-09-28
**Deciders:** @sossiogll (maintainer)

## Context

The backend serves some resources itself: provider covers (`/cover/provider/{p}/{id}`),
provider streams (`/stream/provider/{p}/{id}`) and library files (`/stream/{track_id}`).
The frontend reaches the API only under a public prefix (`PUBLIC_API_PREFIX`, default
`/api`), so a URL without it loads the frontend page instead of the image or audio.

`services/public_urls.py` builds these URLs with the prefix, and every live route was
correct. But two columns keep a copy of the track as it was when it was written:

- `tracks.track_data` (JSON: `cover_url`, `preview_url`, `stream_url`);
- `user_saved_track_providers.stream_url`.

`GET /library/saved-tracks` and `GET /downloads` returned those values verbatim. Rows
written before the prefix existed held `/stream/provider/...`; newer rows held
`/api/stream/...`, which would break again the day the prefix changes.

## Decision

A URL served by the host is never persisted, and never read back as a URL.

- **Stored form.** It is stored as a prefix-free reference with its own scheme:
  `hostref:/cover/provider/{p}/{id}`, `hostref:/stream/provider/{p}/{id}`,
  `hostref:/stream/{track_id}` (`core.urls.to_host_ref`). The scheme makes it
  impossible to mistake for, or serve as, a URL. An absolute URL (a provider CDN) is
  stored unchanged. A root-relative path that is not one of those resources is dropped.
- **Write side.** `db.track_store.stored_track_data()` is the only writer of
  `tracks.track_data`; `workers.tasks._persist_saved_tracks` converts
  `user_saved_track_providers.stream_url` with `to_host_ref`.
- **Read side.** `db.track_store.track_data_dict()` is the only reader of the column
  and returns the stored form. `services.stored_tracks.public_track_dict()` is the
  only place a stored track becomes a response: it rebuilds each reference through the
  builders in `core/urls.py` with the current prefix (`services.public_urls.publish*`).
  It also accepts the legacy forms (with or without an old prefix), so an unmigrated
  row still comes out right.
- **Internal use.** `track_from_record()` returns a `Track` whose URLs are still
  references: it is for host logic (matching, availability, re-download), not for
  responses.
- **Migration.** `0019_host_url_refs` rewrites both columns to the stored form. It is
  idempotent (a reference stays a reference); its downgrade publishes the references
  with the current prefix, the form the previous code served verbatim.

## Consequences

- Changing `PUBLIC_API_PREFIX` needs no data migration.
- A new route that returns stored tracks must go through `public_track_dict()` (or
  `public_urls.publish()` for a single URL). `tests/test_stored_host_urls.py` checks
  `/library/saved-tracks` and `/downloads` against old rows; the OpenAPI response test
  validates their shape.
- The frontend no longer needs to prepend the prefix to `stream_url`
  (`musizerr-ui/src/contexts/PlayerContext.tsx`).

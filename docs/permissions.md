# Permission model

Who may see and do what. Three principals, one switch, and a settings scope.

## The switch: `auth_enabled`

When `auth_enabled` is **false** there is no notion of identity. `get_current_user`
returns a synthetic admin (`__system__`), so every route that asks for a user gets
one, and every admin gate opens. The frontend mirrors this through the
capabilities GET /auth/account/status reports for mode `open` (see the
permission matrix below). A deployment in this mode is a
single-tenant appliance and the roles below do not apply, except for the one
thing it still has: the instance's owner and its accounts (next section).

Everything that follows describes `auth_enabled = true`.

### Open mode: the instance is the one owner

With authentication off there is ONE person, the synthetic user `__system__`
(its row is not active: nobody signs in as it, and it is neither listed nor
managed). For it "my own account" and "the instance's shared account" are the
same thing, so its personal account IS the shared one. This is decided in one
place, `services/credentials.py` (`_principal`, which sets
`CredentialStore(instance_owner=True)`), and only while `auth_enabled` is off
(read from the settings table; anything unreadable counts as on):

- reading the synthetic user's own account reads the shared row, and an
  `own_store` for it (importing its likes, propagating a favorite, the
  playlists pushed to the sync provider) sees that row too; an app-only token
  is still never seen through `own_store`;
- a write to its `personal` target lands on the shared row, so Settings ›
  Linked accounts and Administration › Providers show the same connection (a
  shared row never carries an `external_id`: it is what a person signs in with,
  and the instance signs nobody in);
- with authentication ON the synthetic user is nobody: its store is a plain
  one (no row of its own, nothing shared), and no user, whatever the role,
  reaches the shared row through `own_store`;
- the host's per-user jobs act for `services.credentials.account_holders` (the
  active users; the synthetic user in open mode): the periodic likes sync
  (`sync_all_users_saved_tracks`) and `holds_accounts` for the followed
  playlists refresh. The synthetic user is never made active.
- Migration 0042 moves the account rows an older version wrote under
  `__system__` to the shared ones.

In open mode the owner has the WHOLE experience of a signed-in admin
(`services/capabilities.py`: the `open` capabilities equal the `admin` ones,
`settings_personal` included; `tests/test_capabilities.py` pins it). Their
personal settings (scope `user`: language, theme, the animated background,
`unlike_on_origin_providers`, and any future one) are stored for
`SYSTEM_USER_ID` like any user's, through the same `PATCH /settings`
(`SettingsService` treats the synthetic user as a user: its row exists,
`user_service.ensure_system_user`). Works end to end for it: favorites and the
heart, pending likes and "favorite on arrival", removed favorites, the likes
import (manual and periodic), "download the ones liked elsewhere", own,
followed and mirrored playlists, download requests (never governed in open
mode), Settings › Linked accounts and Administration › Providers (the same
connections), the unlike on the origin provider. A likes sync that cannot
import anything ends `failed` with a code the UI words
(`provider_not_connected`: nothing connected; `no_library_provider`: no
installed provider keeps a likes library), never "completed, 0 tracks".

What still differs from an admin with an account, and why: there is no identity
block, no sign-out and no password to change (there is no session or
credential), no user management (there is only one person) and no sign-in
through a linked account (the instance signs nobody in: `login` is null and a
shared row never keeps an `external_id`); the per-user download policy does not
apply (nobody is governed). Everything the owner owns is parented to
`__system__`, so it can be moved to a real account later. Tests:
`app/tests/test_open_mode_accounts.py`.

### `AUTH_SECRET`: the key that signs every session

With `auth_enabled = true` access and refresh tokens are signed with the
`AUTH_SECRET` environment variable. Whoever knows it can forge the token of an
administrator, so it is a secret of the deployment, never a value from the
repository.

- **Compose requires it** (`docker-compose.yaml`, `docker-compose.dev.yml`):
  `${AUTH_SECRET:?set AUTH_SECRET in .env}`, like `POSTGRES_PASSWORD`. Write it in
  `.env` (never committed, see `.env.example`); every `docker compose`
  command on those files needs it, `stop` and `config` included.
- **The application refuses to start** with authentication enabled and an
  `AUTH_SECRET` that is missing, empty, shorter than 32 characters or one of
  the published placeholders (`change-me-in-production`, `change-me`,
  `secret`). The error names the variable and how to fix it. There is no
  setting to skip the check. With authentication off nothing is signed and the
  secret is not looked at, which is also why the test suite (it runs with
  `AUTH_SECRET=test`) starts no application with authentication on unless a test
  sets the secret it means (`tests/test_auth_secret_startup.py`).
- **Turning authentication on while the app runs** (the settings page, `PATCH
  /settings {"auth_enabled": true}`) asks the same rule, where every setting is
  written (`SettingsService.write_values`, through the `guard_on` the catalog
  declares for `auth_enabled`). A weak secret answers 400 with
  `detail.error_code` `auth_secret_insecure` (translated by the UI), the whole
  batch is refused and nothing is written; the message never contains the
  secret. Turning authentication off is always allowed
  (`tests/test_auth_enabled_guard.py`).
- **Generate one**: `openssl rand -hex 32` (64 hex characters), or
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **Changing it signs everybody out**: tokens signed with the old value stop
  verifying and each user signs in again. After the PostgreSQL switch
  (`docs/postgresql.md`) keep the value you set, or accept that.
- An instance that ran with the old compose default (`change-me-in-production`)
  must be treated as exposed: set a new secret, and review the accounts.
- `AUTH_SECRET` signs; it encrypts nothing. The connected provider accounts, tokens,
  API keys and secret settings in the database are encrypted with a different key,
  `CREDENTIALS_KEY`, required in the same way: `docs/credentials.md`.

## The three principals

| Principal | How it is recognised | Intent |
| --- | --- | --- |
| **Anonymous** | no bearer token, or an invalid one | may browse, may not own anything |
| **User** | valid token, `role = "user"` | owns a library, a set of connected provider accounts, and personal preferences |
| **Admin** | valid token, `role = "admin"` | additionally configures the deployment |

Routes declare which they require through a dependency:

- `CurrentUserDep` — rejects anonymous with 401.
- `AdminUserDep` — rejects anonymous with 401 and a non-admin with 403.
- `OptionalUserDep` — never rejects; returns `None` for anonymous so the route
  can decide. Used where a deployment may deliberately open an action up.
- no user dependency at all — the route is public.

`SearchAggregatorDep` transitively requires a user, because it resolves
per-user provider credentials. Routes that take it are authenticated even
though they do not name a user dependency.

## Settings scope

Since the settings refactor, who may read or write a setting is declared, not
coded. `SettingDef.scope` carries one of three values and the resolver enforces
it. Never reintroduce a key-name allow-list in a route.

| scope | admin reads | admin writes | user reads | user writes |
| --- | --- | --- | --- | --- |
| `system` | yes | yes | **no** | no |
| `user` | own value | own value | own value | own value |
| `system_with_user_override` | yes | the global default | yes | their own override |

A `system` setting is invisible to a non-admin: it is omitted from the response,
not blanked. There is no second, inner safety net beyond that omission: the
settings path relies solely on `can_read`/scope. Every `secret` setting declared in the catalog today is `scope="system"`, so
this is not currently an exploitable gap, but a future `secret` setting
declared with a more permissive scope would be readable by a non-admin.

Anonymous callers have no settings at all. `GET /settings/values` requires a user.

A setting that makes the host act on a user's own provider accounts is `user`,
never `system_with_user_override`: an admin default would act on other
people's accounts without their choice, and an admin writing such a key would
set that default instead of their own value. `unlike_on_origin_providers`
(take a like back on the provider it came from when a favorite is removed) is
one: off until the user turns it on, read for the user who removes the
favorite (`services.settings.personal_value`), and the task uses only that
user's own connected account (`services.credentials.own_store`, no system
fallback, no app-only token). In open mode the owner has it like any user (stored for
`__system__`, off until they turn it on) and the shared account is theirs.
Migration 0030 dropped the old global value; when it was on, each admin kept
it as their own choice. Tests: `app/tests/test_unlike_on_origin.py`.

## Provider accounts

Whose account a call to a provider uses is not a role rule: each provider
declares an account model (`musizerr_sdk.auth.AccountModel`) and one routing rule
(`db/auth_store.py::CredentialStore`) applies it, for a route, a worker and a
script alike.

| Model | Users | The instance |
| --- | --- | --- |
| `none` | no account | no account |
| `personal` | each connects their own; never anyone else's | no shared account |
| `personal_with_shared` | each may connect their own | an admin manages a shared account, used for the users who have none and by the host's jobs |
| `system` | cannot connect anything | an admin manages the one account |

A read goes to the user's own account first and then to the shared one, when
the model has them. A guest, a host job and a script see only the shared
accounts. What acts on a person's own data (importing their likes, taking one
back, propagating a favorite) uses `own_store`: the user's own account only,
whoever the user is: an admin's shared account is not their library (open
mode is the one exception, see "Open mode" above: there the instance is the user).

A write names its target, `personal` or `shared` (`?target=` on
`/auth/{provider}/{authorize-url,start,credentials,test}` and `DELETE
/auth/{provider}`; the OAuth2 state token carries it and the callback checks it
again):

- `personal`: any signed-in user, only where the model has personal accounts
  (400 otherwise).
- `shared`: admins only (403), only where the model has a shared account (400
  otherwise). The API key and the OAuth2 app client of a provider are always
  the instance's: admin-only.
- An admin who connects their own account does not write the shared one: the
  copy that older versions made of it is gone, the two are independent accounts.
- `GET /auth/linked-accounts` (capability `linked_accounts`) shows each user
  only their own accounts and whether a shared one exists, never who it is;
  `GET /providers` shows the shared account's name to admins only.
- `DELETE /auth/me/accounts` disconnects every account of the caller and never a
  shared one.

Where to manage them: Settings › Linked accounts for a user, Administration ›
Providers for an admin. Tests: `app/tests/test_account_routing.py`,
`app/tests/test_linked_accounts_routes.py`.

Signing in through a linked account (`docs/provider-login.md`) is public like
the login: `GET /auth/account/login-providers`, `GET
/auth/account/login/{provider}/start`, `POST
/auth/account/login/{provider}/credentials` and `POST
/auth/account/login/exchange` need no session (they are the way to get one) and
share a per-address rate limit. Linking stays behind a session, and an account
never links by email or name.

## What anonymous can reach today

This is the current inventory, not an endorsement. Several entries are gaps.

**Intended and fine**

- `GET /health` — liveness; the internals are for admins only.
- `GET /auth/account/status`, `POST /login`, `POST /register`, `POST /refresh`,
  `POST /logout` — the sign-in surface itself.
- `GET /{provider}/callback` — the OAuth2 return leg. It cannot carry a bearer
  token, so it authenticates via a signed state token instead.
- `GET /config/download-states` — declaration metadata only. No values, so
  nothing to disclose. The settings schema itself (`GET /settings/schema`)
  requires a user.
- `POST /delegation/webhook/{provider_name}` — inbound push from an external
  system. Authenticated by a shared token compared in constant time, and
  disabled entirely when the feature is off.

**Downloads require a signed-in user**

- `POST /download/track`, `/download/album`, `/download/artist`,
  `/download/playlist`, `POST /downloads/{track_id}/retry` and
  `POST /downloads/retry-failed` take `DownloaderDep` (`routes/deps.py`),
  built on `get_current_user`: a guest gets 401 and nothing is created or
  queued. With auth disabled the download has no requester, as before. There
  is no setting to open downloads to guests any more (migration 0016 removes
  the old `allow_anonymous_downloads` value). Tests:
  `app/tests/test_download_permissions.py`.
- **A user's download always adds the track to their favorites when it
  arrives.** Every one of those routes, and the library's "download the
  missing likes" (`POST /library/download-sync`), registers a pending like
  intent for the caller before queueing (for a request that waits for an
  admin, when it is approved, never before); no parameter turns it off
  (`services.capabilities.favorite_on_arrival`). Only an admin (and open
  mode) can download without it: plain by default, with favorite when the
  body (or the retry query) says `add_to_favorites: true`. If the download
  fails the intent stays, for the retry. Tests: `app/tests/test_favorites.py`.

**Download requests wait for an admin when the policy says so**

A download of a user (a signed-in non-admin) goes through the approval policy
before anything is queued or delegated (`services/download_requests.py`; the
rules are in `core/download_policy.py`). Admins are never subject to it, and
with authentication off (open mode) it does not apply: there are no individual
users, only the built-in administrator.

- **Per kind** (`track`, `album`, `playlist`, `artist`, and `liked_elsewhere`,
  the library job "download the likes that are not in the library"): `auto`
  (approved at once), `approval` (always waits for an admin) or `auto_up_to` N
  (approved at once when the request needs at most N tracks downloaded). The
  count is the tracks that NEED a download, never the ones already in the
  library or with an open download the request would join.
- **Quota per user**: the tracks the approved requests of one user may queue
  in a rolling window of days (0 = none). A request that would pass it waits,
  whatever its mode (`reason: over_quota`).
- **Defaults**: track `auto`; album `auto_up_to` 30; playlist and artist
  `approval`; `liked_elsewhere` `auto_up_to` 50; quota 200 tracks per 7 days.
- **Two levels.** The general policy is the `download_request_*` settings of
  the catalogue (admin-only, `scope="system"`; `GET`/`PATCH /admin/download-policy`
  or the settings page). A user's override (a mode or threshold per kind, the
  quota) is a row of `user_request_policies`, written ONLY by an admin for
  another user (`GET`/`PUT`/`DELETE /admin/users/{id}/download-policy`): a
  user cannot change their own policy. It is not a settings row of scope
  `user`, because the settings pipeline attributes those to whoever writes
  them.
- **A waiting request** answers the download route with 202
  `{status: "pending_approval", request_id, kind, needed_tracks, reason}`. It
  queues nothing, delegates nothing (an album's hand-over to an external system
  included) and registers no favorite on arrival; those happen, through the
  same gate, when an admin approves. A denied or cancelled request leaves
  nothing to undo.
- **Who may do what**: a user lists their own requests
  (`GET /download-requests`) and cancels their own pending one
  (`POST /download-requests/{id}/cancel`; somebody else's is 404). An admin
  lists everybody's with filters (`GET /admin/download-requests`), counts the
  pending ones (`/admin/download-requests/pending-count`), approves and denies
  one or many, always by ids (`POST /admin/download-requests/approve` and
  `.../deny` with `ids`; each id answers its own `outcome`). Deciding is
  idempotent and race-free: of two admins approving at once, or an approval racing a
  cancellation, exactly one outcome happens and the downloads are queued once.
- **A retry is not a request.** `POST /downloads/{track_id}/retry` and
  `/downloads/retry-failed` re-run a download that exists, which exists only
  because a request was approved: a user may retry only a track they have a
  download of (their own, or one they joined), any other is 403
  `retry_not_approved`. The periodic retry of transient failures is not
  governed either.
- **What the UI reads**: `GET /auth/account/status` has `download_policy`
  (`applies`, per kind the `mode`, `threshold` and `auto_up_to`, the quota used
  and left). The server stays the only judge when the request is made.
- **Notifications** on `/progress`: the requester gets `{"type":
  "download_request", "event": "approved" | "denied", "user_id", "request"}`
  (and only they); the admins get the same type, with no `user_id`, when a
  request is waiting (`event: "pending"`, plus the `pending` count) or was
  decided or cancelled.
- Tests: `app/tests/test_download_policy.py`, `test_download_requests.py`,
  and the PostgreSQL races in `test_concurrency.py`.

**Favorites**

- `GET /favorites`, `PUT|DELETE /favorites/{track_id}`, `POST
  /favorites/batch`, `GET /favorites/elsewhere` take `CurrentUserDep`: a
  guest gets 401. Favorites are per user (open mode: the built-in system
  user). Only a track in the library can be a favorite (409
  `not_in_library`); removing one never deletes the file — the library is
  shared by every user, deleting files stays an admin action.

**Browsing, currently open by design**

- `GET /album|artist|playlist|track/{provider}/{id}` and the artist-albums and
  lyrics variants.
- `GET /new-releases`, `GET /trending/*`.

The track lists among them take `ViewerDep` (`OptionalUserDep` underneath,
policy `optional` in the route inventory): they never reject, and only a
signed-in viewer gets their own `favorite` / `favorite_pending` and a `full`
`playback`; a guest gets previews only (`services/track_state.py`).

These read third-party metadata through the shared accounts, or through the viewer's own when they are signed in and connected one (`routes.deps.get_credential_store`). They are open
so an unauthenticated visitor can browse before signing in. Note the cost: an
anonymous caller consumes the deployment's provider API quota.

**Closed**

- `GET /downloads` now takes `CurrentUserDep`: an anonymous caller gets a 401
  instead of the whole instance's download history. An admin still sees every
  request; a non-admin sees only their own (`Download.requested_by`, an
  indexed column, so the extra `WHERE` is cheap — applied to the count query,
  the grouping query, and the per-track attempts query, so another user's
  attempt on a track a non-admin also tried is neither counted nor listed).
  The frontend route `/downloads` (`musizerr-ui/src/router.tsx`) is now
  wrapped in `RequireCapability`, matching `/config`, so an anonymous visitor is
  redirected to sign in instead of hitting a bare 401.
- `POST /downloads/{track_id}/retry` follows the same rule as the four
  `/download/*` routes. It also now stamps `Download.requested_by` and threads
  `user_id` into the Celery task the same way `enqueue_track` does for a fresh
  download; before this fix a retry was never attributed to anyone, which
  would have made it invisible to its own requester once `GET /downloads`
  started scoping by `requested_by`.
- Tests: `app/tests/test_history_permissions.py`.
- `GET /stream/{track_id}` and `GET /stream/provider/{provider}/{track_id}`
  (full audio) now take `StreamUserDep` (`routes/deps.py`). An `<audio>`
  element's `src` request cannot carry a bearer header, so identity comes
  from an HttpOnly, `SameSite=Lax` cookie instead: `POST
  /auth/account/stream-session` (`CurrentUserDep`) mints a short-lived JWT of
  a distinct `type="stream"` (`core/user_auth.py` — an access token cannot be
  used as this cookie, and this cookie cannot be used as a bearer access
  token) and sets it; `StreamUserDep` accepts that cookie or, for API
  clients with no cookie, a regular bearer access token. `DELETE
  /auth/account/stream-session` and `POST /auth/account/logout` both clear
  it. The frontend (`musizerr-ui/src/lib/api-client.ts`) calls the mint
  endpoint after login and after every access-token refresh, over the same `/api`
  same-origin proxy the rest of the app uses, so the cookie rides along with
  the `<audio>` request. `GET /playback/{provider}/{provider_id}` follows the
  same rule: an anonymous caller (auth enabled) gets `type: "preview"` with
  the track's `preview_url`, or `type: "none"`, never a `stream_url`. Tests:
  `app/tests/test_stream_permissions.py`.
- `GET /library/duplicates/members/{member_id}/stream` (one file of a
  duplicate group, heard before choosing the one to keep) takes
  `StreamAdminDep` (`routes/deps.require_stream_admin`): the same stream
  cookie or bearer token as above, and the admin role (403 otherwise). It
  plays the member by its own identity (its sync id, else its real storage
  path), never through `/stream/{track_id}`. Tests:
  `app/tests/test_duplicate_member_stream.py`.

**Closed: external utility credentials**

- The `/credentials/*` routes of `routes/external_utility.py` (both mounts) are
  admin-only. The session capture utility signs in at `/api/auth/login` and
  sends a bearer token, so it keeps working with an admin account. The config
  read masks the OAuth client secret. The paths exist only for a name a provider
  declares (`ExternalUtilityConfig.intake_name`); any other is a 404. Tests:
  `app/tests/test_external_utility_intake.py`.
- `GET /progress` authenticates with the stream cookie and sends each user
  only the events of downloads they can see in `GET /downloads`.

## The frontend reads capabilities, never rules

The UI never re-derives a permission from `auth_enabled`, a role or a token.
`useCapabilities()` (`musizerr-ui/src/hooks/useCapabilities.ts`) returns the
`mode` and `capabilities` of GET /auth/account/status, and
`RequireCapability` guards routes with them: a guest is sent to sign in, a
signed-in viewer without the capability is told why. While the viewer is
unknown nothing is allowed and the app itself is not rendered (fail-closed,
`AppGate`). The server still enforces every rule on its own.

## Permission matrix

What each viewer can do. `GET /auth/account/status` reports the same thing as
`mode` and `capabilities` (`services/capabilities.py`), and
`app/tests/test_capabilities.py` checks that each capability matches the real
HTTP outcome. `app/tests/test_route_auth_inventory.py` lists the policy of
every route.

| Capability | guest | user | admin | open (auth off) |
| --- | --- | --- | --- | --- |
| Browse discover, track/album/artist/playlist details, lyrics, previews | yes | yes | yes | yes |
| Provider labels and metadata priority (`GET /providers/public`) | yes | yes | yes | yes |
| Search | no (401) | yes | yes | yes |
| Download and add to favorites on arrival, retry (`download`) | no (401) | yes (always with favorite) | yes | yes, no requester |
| Download goes through the approval policy (a request may wait for an admin) | no | yes | no (never) | no (never) |
| Approve/deny requests, change the general or a user's download policy | no (401) | no (403) | yes | yes |
| Plain download, no favorite (`download_plain`) | no (401) | no (the favorite is forced) | yes (`add_to_favorites: false`, the default) | yes |
| Heart: like/unlike a track in the library, favorites list (`favorite`) | no (401) | yes | yes | yes (system user) |
| Own playlists: create, edit, follow a provider's, delete (`playlists`; private: another user's is a 404, `docs/playlists.md`) | no (401) | yes | yes | yes (the system user's) |
| Full audio stream, download progress (`/progress`) | no (401) | yes (own downloads) | yes (all) | yes (all) |
| Library sync of likes, "liked elsewhere" (`library`) | no (401) | yes | yes | yes |
| Personal settings | no (401) | yes | yes | yes (stored for the instance's user) |
| Connect, test and disconnect their own provider accounts (`linked_accounts`) | no (401) | yes | yes | yes (the system user's) |
| Connect, test and disconnect a shared (instance) account, the OAuth2 app, an API key | no (401) | no (403) | yes | yes |
| Admin routes, credentials, routines | no (401) | no (403) | yes | yes |
| Raw error detail (`error_detail`), `/health` internals | no | no | yes | yes |

## Rules for new work

1. Default to `CurrentUserDep`. Choose `OptionalUserDep` only when a config flag
   decides the outcome, and honour that flag.
2. A route that queues work, spends provider quota, or serves stored audio is
   not read-only. Gate it.
3. Express who may touch a setting with `scope` in the declaration. Do not add
   role checks to route bodies.
4. Anything a non-admin must not see is omitted from the response, not blanked.
5. A public endpoint that must accept unauthenticated calls needs its own
   authentication: a shared token or a signed state token, never obscurity.

## Animated background (personal appearance setting)

`background` is a `user` enum setting (`off`, `aurora`, `waves`, `spectrum`,
`typography`), default `off`, in the Account group like `theme` and
`language`; its UI is generated from the catalogue. Nothing server-side acts on
it: it is only stored per user. In open mode it is the owner's, stored for the
instance's user, and drawn like for any account.

The frontend (`components/shell/AppBackground.tsx`) draws it only on a desktop
(`BACKGROUND_DESKTOP_QUERY`: width >= 1024px, hover and a fine pointer, followed
live). On any other device, or with the setting off, nothing is imported or
mounted: no canvas, no loop, no listener but that one media query. The canvas
code is a separate chunk (dynamic import). It runs at most 30 fps and 1.5
device pixels per CSS pixel, stops while the tab is hidden, paints a single
still frame with `prefers-reduced-motion`, and never takes pointer events.
Every scene follows the player: it moves only while a track plays, comes to
rest (still and dimmer, the spectrum's bars flat) within a third of a second
of a pause, paints nothing more until play resumes, and wakes within a tenth
of a second.

Aurora, waves and spectrum follow the sound itself (`lib/audio-analysis.ts`):
the spectrum draws the bands of the audio on a scale with headroom (-90..-12
dBFS, so a dense mix does not fill every bar); each aurora cloud (bass, low
mids, voice, highs) and each wave (five bands, the farthest the bass) follows a
band of its own, measured against that band's recent peak so the quiet highs
move as much as the bass, and flashes or kicks up on that band's own hits. The
bass leads in full; the bands above it answer less and less
(`AURORA_RESPONSE`, `WAVES_RESPONSE`), so a song that is not dance music does
not set off fireworks. With the audio nothing moves by the clock: a cloud
drifts and a wave flows only as far as its band carries it (`Voice.travel`),
and stands still in that band's silences. Typography
shows the track, not its sound. Analysing means routing the player's audio
element through a Web Audio graph (`createMediaElementSource`), which cannot be
undone and plays silence for a source of another origin without CORS headers.
So the element is routed only while a background asks for it, only for a
source of the page's own origin (every source the server gives is one: streams,
sync copies, previews through `/preview/{provider}/{id}`) and only once an
AudioContext runs; before loading a source of another origin into a routed
element the player takes a fresh one. Where the audio cannot be analysed the
scenes move on their own while a track plays.

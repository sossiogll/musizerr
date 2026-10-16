# Signing in through a provider

A user can sign in to Musizerr with an account they have linked on a provider
(an OAuth2 service, or a service with a login account). It is generic: a
provider declares it, the host does the rest, and nothing outside `providers/`
knows a provider by name.

## What a provider declares

In `musizerr_sdk.interfaces`, on `BaseProvider`, both optional:

- `supports_login() -> bool`: the account can sign users in (default `False`).
  It only counts for a provider with accounts of a user's own
  (`AuthDefinition.account_model` of `personal` or `personal_with_shared`): an
  account the instance owns belongs to nobody, so nobody signs in through it.
- `identify_account(session) -> AccountIdentity | None`: who the account behind
  `session` is, as `AccountIdentity(external_id, display_name)`. The id must be
  the provider's own stable id, never an email or a name that can change hands.
  For an OAuth2 provider `session` is the token just granted; for a login
  account the submitted fields are in `session.extra`, and this call is what
  checks them. `None`: the provider does not know the account (credentials
  refused). Raise when the service cannot be reached (the host answers 503).

A login-capable provider carries the `login` capability in the `capabilities`
of its personal account (`sdk/src/musizerr_sdk/capabilities.py`).

## The rule: the provider's id, and only it

Linking is done **only from an open session** (Settings › Linked accounts, the
flow of `docs/permissions.md`). The host asks the provider who the account is
(`identify_account`) and stores that id with the linked account
(`provider_credentials.external_id`, unique per provider: an account of a
provider is linked to one user). A sign-in finds the user by that id and by
nothing else: **an email, a name or a username never links two accounts**.

An account linked before this feature carries no id and cannot sign anybody in;
the linked-accounts list marks it (`personal.login: false`) and reconnecting it
fixes it. A provider whose service says the id belongs to another user is
refused with 409.

## The first sign-in of an account nobody linked

An admin policy decides, the `provider_login_new_accounts` setting (group
Authentication, system scope, admin only):

| Value | What happens |
| --- | --- |
| `disabled` (default) | Nothing is created; the sign-in is refused (`provider_login_disabled`). |
| `pending` | An account is created inactive with `pending_approval`; the sign-in answers `account_pending`. An admin activates it in Administration › Users (activating clears the flag). |
| `active` | An account is created and the user is signed in at once. |

The account created is always a plain `user`: **the admin role never comes from
a provider**. It gets an unguessable password nobody knows, so it is entered
through its provider; its username comes from the provider's display name, made
unique with a suffix when taken. Whoever linked the provider from their own
session signs in whatever the policy says.

## The two ways in

- **OAuth2**: `GET /auth/account/login/{provider}/start` redirects to the
  provider. The state names the provider and carries the hash of a random nonce
  that the response also sets in the HttpOnly cookie `musizerr_login_nonce`
  (SameSite=Lax). The callback is the one every OAuth2 provider already has
  (`/auth/{provider}/callback`, so no new redirect URI to register): its state
  says linking or sign-in, and a sign-in needs the state AND the cookie, so a
  victim's browser cannot be made to finish an authorization an attacker
  started. The callback ends on the frontend page `/login/provider` with a
  one-time `?code=` (or `?error=<code>`); the page exchanges it
  (`POST /auth/account/login/exchange`) for a session. The code lives 60 s,
  only its hash is stored, and one conditional UPDATE spends it. A session never
  travels in a URL. **The start and the callback must be served from the same
  host** (the cookie is host-scoped): `APP_BASE_URL` and the address users open
  must share it.
- **A login account**: `POST /auth/account/login/{provider}/credentials` with
  exactly the fields the provider declares (`credential_fields`).

The session is issued by `services/refresh_tokens.start_session`, the same as a
password login: same access token, same refresh-token family and rotation.

`GET /auth/account/login-providers` lists, publicly, the providers the login
page offers (an OAuth2 provider once an admin set its app up); the page renders
one button per entry and the form of a login account from its fields.

## Rate limit

The sign-in routes (start, credentials, exchange, and the callback of a
sign-in) share a fixed window per client address in Redis
(`services/login_rate_limit.py`): 20 requests per 60 s, then 429 with
`Retry-After` (the callback lands on `/login/provider?error=provider_login_rate_limited`).
The address is the last entry of `X-Forwarded-For`, the one the bundled proxy
appends. Without Redis nothing is limited. The existing password login has no
limit of its own.

## Provider limits to know

What a provider can do is its own: whether it declares `supports_login`, what its
service accepts, and the limits it puts on an application. Each provider's
documentation (in its own repository) says so. Two things are general:

- A service may cap the users of an application in development mode, so that
  signing in through it works for the users it allows and fails for everyone else
  (the provider refuses the authorization; the user lands on the error page).
- A provider with a login account (`credential_fields`) signs in with its
  credentials against its service, which checks them; the id is the one the
  service reports.

## Where it lives

`core/provider_login.py` (rules, policy values, error codes),
`services/provider_login.py` (the flows), `routes/provider_login.py` (HTTP),
`services/login_rate_limit.py`, `db/auth_store.py` (`external_id`, `linked_user_id`),
migration `0037_provider_login`. Tests: `test_provider_login.py`,
`test_provider_login_providers.py`, `test_migration_0037_provider_login.py`
and the races in `test_concurrency.py`.

# Stored credentials are encrypted

Everything Musizeer keeps in its database to sign in to other services is encrypted
at rest, with a key of its own: `MUSIZEER_CREDENTIALS_KEY`. Someone who gets a copy of the
database (a backup, a dump, a stolen volume) reads no token, password, API key or
client secret from it. They need the key too, and the key is not in the database.

This is **not** the key of the sessions (`MUSIZEER_AUTH_SECRET`, `docs/permissions.md`): that one
signs tokens, this one encrypts data. They are independent and neither can be derived
from the other.

## What is encrypted

| Where | What | Notes |
|---|---|---|
| `provider_credentials.data` | every account and secret of a provider: OAuth2 tokens (personal and shared), logins, OAuth clients (id and secret), API keys, the app-only token, the accounts a capture utility registered | all rows, whoever they belong to |
| `provider_sources.credential` | the token of a private source (catalog and images) | `NULL` stays `NULL` |
| `settings.value` | the value of a **secret setting**: one the catalog declares `secret` (`delegation_webhook_token`) or one a provider declares with `ProviderSetting(secret=True)` | only those rows; every other setting stays readable JSON |

Left as it is, on purpose:

- **Password hashes** (`users.password_hash`) and **token hashes** (`refresh_tokens.token_hash`,
  `provider_login_codes.code_hash`): they are one-way hashes, there is nothing to decrypt and
  nothing to encrypt them for.
- **`provider_credentials.external_id`**: the id a provider gave an account. The sign-in through a
  provider looks users up by it and it is unique per provider, so it has to be comparable. It is an
  identifier, not a secret.
- The columns the credentials are looked up by (`provider`, `credential_type`, `user_id`), the
  OAuth `client_id` inside the sealed object, and the names of the settings: they say *that*
  something is stored, not what.
- Redis holds no credential (locks, counters, events). A provider is handed the credential a call
  needs, decrypted by the host, for that call only, through the sandbox's socket; it never sees the
  key nor the stored rows. The provider gateway never has `MUSIZEER_CREDENTIALS_KEY` (it is given a source's
  token by the host, in memory, to pull an image).

A secret setting is returned **decrypted to an administrator** by `GET /settings/values` and in the
`config` of `GET /providers` (that is what the settings page shows and edits, and has been the
contract since before this; encrypting at rest does not change who may read a value, only what a copy
of the database holds); a non-admin never receives it, and no route returns a credential of the
other kinds.

## The format

A stored value is `enc:v1:` followed by a [Fernet](https://cryptography.io/en/latest/fernet/)
token (AES-128-CBC and an HMAC-SHA256 over the whole token, a fresh random IV each time: the same
secret never encrypts to the same text twice, and an altered value is refused rather than decrypted
to garbage). The prefix tells an encrypted value from a legacy one in clear and carries the version
of the scheme. A setting's value is JSON, so a sealed one is stored as the object
`{"$sealed": "enc:v1:..."}` (no setting has an object for a value, so it cannot be mistaken for one).
`core/secret_box.py` owns the format; `db/credential_cipher.py` is the only module that reads the
key from the environment.

## The key

`MUSIZEER_CREDENTIALS_KEY` is a Fernet key: the urlsafe base64 of 32 random bytes (44 characters ending in
`=`). Generate it **once**:

```sh
docker run --rm musizeer-app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

or, where the application image has not been rebuilt yet (it has no `cryptography` library before
this feature, and compose refuses to build anything without the key), with `openssl`, which makes
the same thing: the urlsafe base64 of 32 random bytes.

```sh
openssl rand -base64 32 | tr '+/' '-_'
```

Write it in `.env` (never committed, see `.env.example`), like `MUSIZEER_AUTH_SECRET` and
`POSTGRES_PASSWORD`. A key still written under its old name, `CREDENTIALS_KEY`, is **not read**: compose, the
application and the workers stop with `CREDENTIALS_KEY is no longer read: rename it to MUSIZEER_CREDENTIALS_KEY`
(the value stays the same: rename the line, do not generate a new key). Both compose files require it (`${MUSIZEER_CREDENTIALS_KEY:?...}`) for the services that
read the database: `init`, `app`, the workers and `beat`. The gateway, the frontend, Redis and
PostgreSQL do not get it.

The application, every worker and beat **refuse to start** when it is missing, empty, not a Fernet
key, or a placeholder (a key made of one repeated byte, such as all zeros): the error names the
variable and how to make a key, and never contains the value. There is no switch to skip the check,
and it does not depend on `auth_enabled`: the credentials are encrypted whether or not users sign
in. It runs before the database is touched, because the migration needs the key.

### Lose the key, reconnect the accounts

**The key is the only way to read the encrypted values. Back it up with the database**: keep `.env`
(or at least `MUSIZEER_CREDENTIALS_KEY`) next to every database backup, in a place that is not the same
volume. A database backup without the key restores everything except the accounts.

If the key is lost, nothing else is: users, library, downloads, favorites, playlists and settings are
untouched. What cannot be read is treated, everywhere, as **not there**:

- a provider account is "not connected" (and the provider's own page says so), an OAuth app has no
  client, an API key is "not set". Connect or enter it again: that replaces the unreadable value;
- a private source has no token (`has_credential: false`); edit the source to give it again;
- a secret setting is back at its declared default (an empty webhook token rejects every push);
- registering an account of a capture utility while the stored ones cannot be read starts again
  from that account.

Each unreadable value is reported **once per process** in the log, in a line that names what it is
("The api_key credential of provider 'x' (shared) cannot be read: ...") and never holds a value,
a key or the encrypted text. Nothing is deleted or rewritten because it could not be read, so
putting the right key back makes it all readable again.

A value that is **in clear** where a sealed one is expected (a row inserted by hand) is treated as
unreadable too, not used as it is. Only the migration below accepts clear values. The reason is that
a store that quietly accepts both cannot tell you whether anything is still unencrypted, and the
migration covers every real case: a database that predates this feature, a restored older backup
included, is upgraded by it when the application (or `init`) starts, before a request is served.

## Upgrading an instance that already has credentials

Migration `0041_encrypt_credentials` runs with the others (`alembic upgrade head`, which the
`init` container and the API run at start). It seals every credential still in clear and the catalog's
secret settings; the secret settings **a provider declares** are not known to a migration (only the
running registry knows which settings its providers declared secret), so the API seals them as soon
as it has loaded the providers, and again whenever they change (`services/secret_settings.py`).
New values are always sealed when written.

The migration needs `MUSIZEER_CREDENTIALS_KEY` and **refuses to run without it**, before it changes a row
(the message says so). It can run twice (a value already sealed is left alone, after checking the
configured keys open it), and it stops, changing nothing, if it finds a value sealed with a key
that is not configured. Both directions work on SQLite and PostgreSQL. Its downgrade opens
everything back to clear text, with the same key, and refuses (writing nothing) if some value
cannot be opened.

**Deploy order:**

1. **Back up the database** (`pg_dump`, or the SQLite file with its `-wal` and `-shm`) **and `.env`**.
   Once the migration has run, the previous version cannot read the credentials (it would try to
   parse `enc:v1:...` as JSON) unless the migration is reverted, with the key.
2. Generate `MUSIZEER_CREDENTIALS_KEY` and write it in `.env`, **before** pulling or rebuilding: compose
   interpolates the whole file on every command, `build` included, and stops without it. Generate it
   with `openssl` (above): the old image has no `cryptography` library to do it with. Back the key up
   with the backup of step 1.
3. Pull and **rebuild the images** (`cryptography` is a new dependency, baked into the image: the
   development compose bind-mounts the code but not the libraries), then `docker compose up -d`.
   `init` runs the migration (it has the key), the rest starts after it.
4. Check the log of `init` for `0041_encrypt_credentials: N stored credential(s) encrypted`, and that
   the accounts show as connected. One line may come just *before* the migration, once: "The setting
   'delegation_webhook_token' cannot be read: it is stored in clear" (the process reads the settings
   at import, before the migrations run, and a catalog secret in clear is not accepted at run time;
   the migration seals it right after).

To go back: restore the backup, or, with the new image and the key, `alembic downgrade 0040_storage_provider_explicit`
(it writes everything in clear again), then start the previous version.

## Rotating the key

Rotating means encrypting everything with a new key and retiring the old one. There is no outage:
for the whole procedure the application reads with both keys.

1. Generate the new key (the command above).
2. In `.env`: `MUSIZEER_CREDENTIALS_KEY=<new>` and `MUSIZEER_CREDENTIALS_KEY_PREVIOUS=<old>` (several old keys can be
   listed, comma separated). Restart the stack. It now reads values sealed with either key and
   seals every new one with the new key.
3. Seal the existing values again, from the `app` container:

   ```sh
   docker compose exec app python -m scripts.rotate_credentials_key            # dry run: counts only
   docker compose exec app python -m scripts.rotate_credentials_key --apply
   ```

   It goes through `provider_credentials`, `provider_sources` and the sealed settings, in one
   transaction, and prints counts: how many were already sealed with the current key, how many it
   sealed again, how many **no configured key opens** (`unreadable`, left exactly as they are), how
   many are in clear (`in_clear`, left as they are) and how many someone changed while it ran
   (`skipped`: run it again). It prints no value, key or encrypted text. The exit status is 1 if
   anything was left as it is, 2 if the key is unusable.
4. When every table says `unreadable 0, in_clear 0`, nothing depends on the old key any more. Remove
   `MUSIZEER_CREDENTIALS_KEY_PREVIOUS` (or the old key from it) and restart. Keep the old key in the backups
   made *before* the rotation, if you want to be able to restore them.

## For developers

- A new place that stores a secret writes it through `db/credential_cipher.seal` and reads it with
  `unseal`, and **decides what an unreadable value is** for its kind (treated as absent, never
  returned as text). `CredentialStore` and `ProviderSource` (`credential`, `set_credential`) are the
  existing ones; `SettingsRepository` seals what `SettingDef.secret` declares. Add the place to
  the migration's successor and to `services/credentials_rotation.py`, and to the table above.
- The test suite runs with a fixed, public test key (`tests/credentials_support.py`, set by
  `tests/conftest.py` like `MUSIZEER_AUTH_SECRET=test`), forced so a run never uses a real key. A test about
  keys sets its own with `use_keys`.
- `tests/test_layering.py` keeps the key's name out of every module but `db/credential_cipher.py`,
  `core/secret_box.py` pure, and the gateway without it.

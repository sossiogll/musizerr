# ADR-0009: The configuration surface: deployment versus application, and the `MUSIZEER_` environment names

**Status:** Proposed
**Date:** 2026-10-24 (revised the same day after the maintainer's decisions)
**Deciders:** @sossiogll (maintainer)
**Related:** ADR-0010 (the rename to Musizeer, one breaking release with no compatibility window: this record's names take
effect in it, and its former staged rename is gone), ADR-0008 (root folders and folder settings: the first user of the
boundary rule below), ADR-0007 (what a MAJOR is), ADR-0006 (the gateway)

## Context

The product is configured in two places: the process environment (set by whoever runs the containers) and the settings (the
database, set in the UI). The line between them was never drawn, and the names of the environment variables were never
chosen: they grew one by one. Three things made that visible on the owner's home-server install.

1. **One fact, two homes.** The workspace folder is both an environment default and a setting, and the stored value wins
   (`app/core/settings_catalog.py:457-466,649`); the gateway reads it from a third variable. They drifted and the
   full-sync check broke (pull request #50 added a single `WORKSPACE_DIR` for the environment side; ADR-0008 removes the
   second home and the variables altogether).
2. **Names that clash in a shared environment.** The owner runs the application next to other containers and wants a prefix
   "to avoid clashes in clusters". The risk is real: `APP_PORT`, `DATA_DIR`, `AUTH_SECRET`, `FRONTEND_URL` and
   `BACKEND_URL` are names any other application may use, and in Kubernetes a Service called `app` injects
   `APP_PORT=tcp://...` into every pod of its namespace, which is the very name the compose file uses for the published
   port.
3. **A surface that does not match its documentation.** The inventory below shows variables the code reads that the
   production compose file never passes to a container, so setting them in `.env` does nothing, and `.env.example`
   documents some of them (`DB_*`).

The product is being renamed Musizeer (ADR-0010) in a single release without a compatibility window, so the names change
once and directly: this record decides **what the names are**, not how to migrate between two sets.

### Inventory: every environment variable today, and what it becomes

Found by reading `os.environ`, `getenv`, `env.get`, `process.env`, `import.meta.env`, the Dockerfiles, the nginx scripts and
every `${...}` of the compose files. "Compose passes it" is about `docker-compose.yaml` (production): Compose forwards to a
container only what its `environment:` lists, and `.env` is used for interpolation only.

**Operator surface (documented or meant to be set by whoever deploys)**

| Today | New | Read by | Default | Required | Compose passes it |
|---|---|---|---|---|---|
| `DATABASE_URL` | **unchanged** | `app/db/engine.py:75`; `scripts/migrate_sqlite_to_postgres.py:540` | unset: SQLite file in the data directory | no (compose always builds it) | yes, built from `POSTGRES_*` |
| `DATA_DIR` | `MUSIZEER_DATA_DIR` | `app/db/engine.py:77`; migration `0010_settings_store.py:187` | `/data` | no | yes (`/data`) |
| `REDIS_URL` | **unchanged** | `app/services/progress.py:20`; `app/services/provider_events.py:46`; `app/workers/celery_app.py:23`; `app/workers/maintenance.py:54` (four reads, four copies of the default) | `redis://redis:6379/0` | no | yes |
| `AUTH_SECRET` | `MUSIZEER_AUTH_SECRET` | `app/core/user_auth.py:36,47` | none | yes in compose; the app refuses a weak one when authentication is on | `app` only |
| `CREDENTIALS_KEY`, `CREDENTIALS_KEY_PREVIOUS` | `MUSIZEER_CREDENTIALS_KEY`, `MUSIZEER_CREDENTIALS_KEY_PREVIOUS` | `app/db/credential_cipher.py:28-29,56` (the only reader) | none | `CREDENTIALS_KEY` yes, always; the previous one only during a rotation | `init`, `app`, workers, `beat`; never the gateway |
| `ADMIN_BOOTSTRAP_EMAIL`, `ADMIN_BOOTSTRAP_PASSWORD` | `MUSIZEER_ADMIN_BOOTSTRAP_EMAIL`, `MUSIZEER_ADMIN_BOOTSTRAP_PASSWORD` | `app/main.py:58-59` | none | no | `app` |
| `APP_BASE_URL` | `MUSIZEER_APP_BASE_URL` | `app/core/auth_manager.py:16` | `http://localhost:8000` | no | yes, derived from `PUBLIC_URL` |
| `FRONTEND_URL` | `MUSIZEER_FRONTEND_URL` | `app/routes/auth.py:30`; `app/routes/provider_login.py:30` | `http://localhost:3000` | no | yes, derived from `PUBLIC_URL` |
| `PUBLIC_API_PREFIX` | `MUSIZEER_PUBLIC_API_PREFIX` | `app/services/public_urls.py:15`; migration `0019_host_url_refs.py:95` | `/api` | no | **no** |
| `ALLOWED_ORIGINS` | `MUSIZEER_ALLOWED_ORIGINS` | `app/main.py:12` | `*` | no | **no** |
| `DB_BUSY_TIMEOUT_MS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `MUSIZEER_DB_BUSY_TIMEOUT_MS`, `MUSIZEER_DB_STATEMENT_TIMEOUT_MS`, `MUSIZEER_DB_POOL_SIZE`, `MUSIZEER_DB_MAX_OVERFLOW` | `app/db/engine.py:58,118,130,135-136` | 15000, 120000, 5, 10 | no | **no** (yet `.env.example` documents them) |
| `MUSIZERR_UPDATE_REPOSITORY` | `MUSIZEER_UPDATE_REPOSITORY` | `app/services/update_check.py:34` | `sossiogll/musizerr` becomes `sossiogll/musizeer` | no | **no** |
| `PROVIDER_GATEWAY_SOCKET` | `MUSIZEER_GATEWAY_SOCKET` | `app/services/gateway_client.py:40,78` | `/run/musizerr/gateway.sock` becomes `/run/musizeer/gateway.sock` | no | no |
| `WORKSPACE_DIR` | **removed**: the working folder is a folder setting over a root folder (ADR-0008) | `app/core/settings_catalog.py:462,466`; compose gives it to the gateway as `GATEWAY_WORKSPACE_DIR` | `/downloads/.musizerr-work` | no | `app` and the workers; the gateway under the other name |
| `GATEWAY_WORKSPACE_DIR` | **removed**: the host pushes the working folder to the gateway (ADR-0008 §9) | `gateway/musizerr_gateway/config.py:38,81` | | no | the gateway |
| `GATEWAY_FILESYSTEM_ROOTS` | **removed**: the host pushes the declared root folders (ADR-0008 §9) | `gateway/musizerr_gateway/config.py:45,85` | empty | no (but a provider is granted no folder without it) | the gateway |
| `GATEWAY_STATE_DIR`, `GATEWAY_RUN_DIR`, `GATEWAY_SANDBOX_UID`, `GATEWAY_SANDBOX_GID`, `GATEWAY_MAX_MEMORY_MB`, `GATEWAY_MAX_OPEN_FILES`, `GATEWAY_MAX_PROCESSES` | the same names with `MUSIZEER_` in front | `gateway/musizerr_gateway/config.py:72-85` | image paths, uid 10001, ceilings 2048 MB / 4096 / 1024 | no | **no** (the ceilings are documented in `docs/gateway.md:96`) |
| `BACKEND_URL`, `DNS_RESOLVER` | `MUSIZEER_BACKEND_URL`, `MUSIZEER_DNS_RESOLVER` | `musizerr-ui/Dockerfile:57-59,71`; `musizerr-ui/nginx/15-backend-url.envsh`; `musizerr-ui/nginx/default.conf.template:30,33` | `http://app:8000`, `127.0.0.11` | no | `BACKEND_URL` yes; the image sets both with `ENV`, so an unset variable cannot be told from the default |

**Compose interpolation only (never read by the application code)**

| Today | New | Used for | Default |
|---|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | **unchanged** | the `postgres` service (the stock image reads them) and `DATABASE_URL` | `musizerr` becomes `musizeer`, none (required), `musizerr` becomes `musizeer` |
| `PUBLIC_URL` | `MUSIZEER_PUBLIC_URL` | derives `MUSIZEER_APP_BASE_URL` and `MUSIZEER_FRONTEND_URL` | `http://localhost` |
| `APP_PORT` | `MUSIZEER_PORT` | the published port of the web app | `80` |
| `MUSIZERR_VERSION`, `MUSIZERR_IMAGES` | `MUSIZEER_VERSION`, `MUSIZEER_IMAGES` | the image tags and the registry/owner of the images | `latest`, `ghcr.io/sossiogll` |
| `GATEWAY_SECCOMP_PROFILE` | `MUSIZEER_GATEWAY_SECCOMP_PROFILE` | the path of the seccomp profile on the host | `./gateway/seccomp/musizeer-gateway.json` |
| `MUSIC_DIR` (dev compose) | `MUSIZEER_DOWNLOADS_DIR` and `MUSIZEER_LIBRARY_DIR`: the developer stack gets two folders like production (ADR-0008) | `docker-compose.dev.yml` only, mounted as `/downloads` in the application, the workers and the gateway | `./data/music` becomes `./data/downloads` and `./data/library` |
| `FRONTEND_PORT`, `OCI_REGISTRY_PORT` (dev compose) | `MUSIZEER_DEV_FRONTEND_PORT`, `MUSIZEER_DEV_OCI_REGISTRY_PORT` | `docker-compose.dev.yml` only | 3000, 15000 |

**Internal (baked into images, build or test tooling; not an operator surface)**

| Today | New | Where | Note |
|---|---|---|---|
| `MUSIZERR_VERSION`, `MUSIZERR_COMMIT` | `MUSIZEER_VERSION`, `MUSIZEER_COMMIT` | build arguments baked as `ENV` (`Dockerfile:54-56`, `gateway/Dockerfile:32-34`); read by `app/services/app_version.py:12-13`, `gateway/musizerr_gateway/control.py:52` | the same name as the compose tag variable above, a different thing (see §5) |
| `VITE_MUSIZERR_VERSION`, `VITE_API_URL` | `VITE_MUSIZEER_VERSION`, `VITE_API_URL` unchanged | `musizerr-ui/Dockerfile:41-42`, `src/lib/version.ts:5`, `vite.config.ts:7` | build time / dev server; Vite exposes only `VITE_*` to the page |
| `NGINX_ENVSUBST_FILTER` | **unchanged**, lists the new names | `musizerr-ui/Dockerfile:59` | the nginx image's own |
| `E2E_USERNAME`, `E2E_PASSWORD`, `E2E_ADMIN_USERNAME`, `E2E_ADMIN_PASSWORD`, `TEST_DATABASE_URL` | **unchanged** | `app/scripts/e2e_user.py:37-46`; the backend test suite | tests |
| `PROVIDERS_REPO`, `INTEGRATION_PROJECT`, `INTEGRATION_REGISTRY_PORT`, `FRONTEND_TEST_PROJECT`, `FRONTEND_TEST_PORT`, `GHCR_NAMESPACE`, `PROVIDER_BASE_IMAGE`, `PROVIDER_INSTALL_REGISTRY`, `DEV_CATALOG_DIR`, `PROVIDER_LABELS_OUT`, `PROVIDER_LOCAL_TAG_PREFIX`, `HOST_IMAGE`, `PROFILE_SOURCE` | **unchanged names**; their defaults carry the new product name (`../musizeer-providers`, `musizeer-provider-`, `musizeer-app`) | `scripts/*.sh` | development and publishing scripts |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (and lower case) | **unchanged** | **set by** the runner inside a sandbox (`sdk/src/musizerr_sdk/runner.py:337-340`) | an output, not an input |
| `TZ` | **unchanged** | read by the C library and Python, not by the application (`grep`: no reference in the repository); the owner's server sets it on the app and gateway containers | a standard |
| the token variable of `provider_sources` | **unchanged** | `app/scripts/provider_sources.py:178,322`: `--token-env NAME` takes the name of any variable | user-named |

### What the inventory shows

- About twenty variables are the product's own and unprefixed.
- Seven variables the code reads cannot be set from `.env` with the shipped compose file (`PUBLIC_API_PREFIX`,
  `ALLOWED_ORIGINS`, the four `DB_*`, `MUSIZERR_UPDATE_REPOSITORY`, and most `GATEWAY_*`).
- The defaults of one variable differ between the code and the compose file (`APP_BASE_URL`: `http://localhost:8000` in
  code, `http://localhost/api` in compose; `FRONTEND_URL`: `:3000` versus none), and `REDIS_URL` has four copies.
- The same name `MUSIZERR_VERSION` means "which image tag to run" in compose and "the version I was built as" inside the
  image.
- Only six names contain the product name. A prefix is a mechanical change for the rest.

## Decision

### 1. The boundary: deployment configuration and application settings

**Deployment configuration** is the process environment. A value belongs there when at least one of these is true:

1. It is needed **before the database can be read**: where the database is (`DATABASE_URL`, `MUSIZEER_DATA_DIR`), Redis,
   the sockets.
2. It is a **secret that protects the stored data or the sessions** and so cannot live in the database it protects
   (`MUSIZEER_CREDENTIALS_KEY`, `MUSIZEER_AUTH_SECRET`).
3. It names something **only the party who runs the containers controls**: a port, the address of the service as the
   outside sees it (`MUSIZEER_PUBLIC_URL`: it is registered at third parties as a redirect URI), the image to run, a resource
   ceiling that sandboxes may not exceed, a security ceiling.
4. It must be **identical in several containers**, and a mismatch fails silently. Such a value is not written twice:
   one process reads it and the others ask that process (the lesson of `WORKSPACE_DIR`), or it is not an
   environment value at all (rule 3 below).

**Application settings** are the database, edited in the UI. A value belongs there when an administrator should be able to
change it at run time without access to the server, it is a policy or a choice that outlives a deployment, and nothing needs
it to boot.

Three rules keep the line from blurring:

1. **A value has one home.** No setting has an environment default. The environment may *bound* a choice (a ceiling, an
   allowed set), the database holds the choice, and the choice is validated against the bound; it is never a second copy of
   the same value. (`workspace_dir` was the counter-example; ADR-0008 makes it a folder setting.)
2. **One reader per process.** Every environment variable is read in one module per deployable
   (`app/core/environment.py` for the API, workers, beat and scripts; `gateway/musizeer_gateway/environment.py` for the
   gateway), through one registry of names, defaults and whether each is required. Nothing else calls `os.environ`. A
   layering test enforces it, like `credential_cipher.py` is already the only reader of the credentials key
   (`tests/test_layering.py`). Providers read no environment (unchanged: the Host Boundary).
3. **Folders are not environment.** The folders the product works in are **application** (ADR-0008: root folders and folder
   settings in the database; the mounts that make them real are the deployment's). The environment carries no folder list,
   not even a ceiling: what an administrator can declare is bounded by what Docker mounted into the gateway, and the
   gateway checks that (ADR-0008 §9). Therefore `WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` and `GATEWAY_FILESYSTEM_ROOTS` do
   not exist any more.

`public_base_url` (the setting: the address a delegate calls back) and `MUSIZEER_PUBLIC_URL` (the variable: the address a
browser uses) stay two things on purpose, since they are addresses for different audiences. The guide says so
(`docs/deployment.md` already does).

### 2. The prefix and what gets it

**The prefix is `MUSIZEER_`.** Rule: *a variable that only this product defines is prefixed; a variable whose name and
meaning another project defines keeps its name.* The table above is the decision, row by row. In short:

| Group | Decision |
|---|---|
| The product's own (everything in the operator surface not listed below) | `MUSIZEER_` + the old name |
| Gateway (`GATEWAY_*`) | `MUSIZEER_GATEWAY_*`; `PROVIDER_GATEWAY_SOCKET` becomes `MUSIZEER_GATEWAY_SOCKET` |
| The ones that already carried the product name | the same with `MUSIZEER_` (`MUSIZEER_VERSION`, `_IMAGES`, `_UPDATE_REPOSITORY`, `_COMMIT`) |
| Port | `APP_PORT` becomes `MUSIZEER_PORT` (the Kubernetes clash above) |
| Development compose | `MUSIC_DIR` is replaced by `MUSIZEER_DOWNLOADS_DIR` and `MUSIZEER_LIBRARY_DIR`; `FRONTEND_PORT`, `OCI_REGISTRY_PORT` become `MUSIZEER_DEV_*` |
| **Keep their standard names** | `DATABASE_URL`, `REDIS_URL` (platforms inject them), `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (the stock `postgres` image defines them and they go only to that container), `TZ`, `NGINX_*` (the nginx image's), `VITE_*` (Vite exposes only these to the page), `HTTP_PROXY` and friends |
| Internal and tooling | the names stay, except the ones that contain the product name (`VITE_MUSIZEER_VERSION`) |

**No prefixed override of the standard names** (the first draft had `MUSIZEER_DATABASE_URL` win over `DATABASE_URL`). An
environment variable is per container, and compose forwards each by name, so a clash needs two applications reading one
`.env`, which the prefix already prevents for everything the product owns; an override would be a second name for one value,
which is the thing this record removes.

### 3. Old names: one safety net, no alias

There are no aliases and no precedence rules: a process reads the new name and only that. The cost of that clarity is
that an old `.env` fails silently when a variable is merely ignored, and two of the variables protect the credentials
and the sessions. So there is **one cheap check**, in the same registry (rule 2 of §1):

- Each registry holds a table `REMOVED` of the **old names** its deployable used to read, with the new name (or, for the
  three removed folder variables, what replaced them).
- **At start, a process that finds an old name set (non-empty) in its environment logs an error naming the new one**:
  `AUTH_SECRET is no longer read: rename it to MUSIZEER_AUTH_SECRET`. For `WORKSPACE_DIR`: `WORKSPACE_DIR is no longer read:
  the working folder is a setting (Administration › Root folders)`. Values are never printed.
- **When the new variable is one the process requires and it is unset, the start fails with that message** instead of the
  generic one. That covers the two that matter: `MUSIZEER_CREDENTIALS_KEY` (always required; a missing key is already a
  refusal to start, `app/db/credential_cipher.py`) and `MUSIZEER_AUTH_SECRET` (required when authentication is on,
  `app/core/user_auth.py`). A variable that is not required is only reported: the process starts with the default and says
  so.
- Compose says the same at the earliest point: `MUSIZEER_AUTH_SECRET: ${MUSIZEER_AUTH_SECRET:?set MUSIZEER_AUTH_SECRET in
  .env (it was AUTH_SECRET)}`.
- The frontend image does it in its entrypoint script (`15-backend-url.envsh`): an old `BACKEND_URL` is an error on stderr
  naming `MUSIZEER_BACKEND_URL`.
- **Typo guard.** A variable that starts with `MUSIZEER_` and is in no registry entry is logged at start ("unknown variable
  MUSIZEER_AUTH_SECERT; did you mean MUSIZEER_AUTH_SECRET?", by edit distance). The old product prefix `MUSIZERR_` differs
  by one letter, so the same table also catches it.
- Nothing is shown in Administration, nothing is counted, nothing is removed later: the table is deleted when the
  maintainer decides nobody has an old file any more, by an ordinary pull request.

**Duplicated code, on purpose.** The gateway imports only the standard library and the SDK (ADR-0006), and the SDK is the
providers' contract: a module of deployment names does not belong in it. The registry therefore exists twice, in the
application and in the gateway, and a test compares the entries both declare (names, defaults) so they cannot diverge.

### 4. Compose and `.env.example`

- **Compose passes only the new names**, once. The old names appear nowhere in the shipped files except in the message of
  the required ones (§3).
- **Variables the code reads but compose never passed are now passed** (`MUSIZEER_PUBLIC_API_PREFIX`,
  `MUSIZEER_ALLOWED_ORIGINS`, `MUSIZEER_DB_*`, `MUSIZEER_UPDATE_REPOSITORY`, the sandbox ceilings of the gateway), each with
  an empty default so the application default applies. This fixes the `.env.example` entries that did nothing.
- **One definition of defaults**: the registry's. Compose does not repeat a default that the code has (the two defaults of
  `APP_BASE_URL` disappear: compose passes `MUSIZEER_PUBLIC_URL` and the application derives the callback and the frontend
  address from it; the derived forms remain overridable).
- **The folders in compose** (ADR-0008): the `downloads` volume at `/downloads` in the application, the workers, beat and
  the gateway, and a new `library` volume at `/library` in the gateway only; `WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` and
  `GATEWAY_FILESYSTEM_ROOTS` are gone. The developer compose mounts `MUSIZEER_DOWNLOADS_DIR` and `MUSIZEER_LIBRARY_DIR` the
  same way (its `MUSIC_DIR` single folder is replaced).
- **The release compose pins its project name** (`name: musizeer`, ADR-0010), so container, network and volume names do not
  depend on the folder the file lives in. A compose file that embeds the services in a shared file keeps its own name.
- **`.env.example`** lists only the new names, grouped (required / address / database / images / advanced). `docs/deployment.md`
  gets the inventory above as its reference table, generated from the registry by a test that fails when the two differ.
- **The frontend image** stops declaring `ENV BACKEND_URL=...`/`DNS_RESOLVER=...` (a default set by the image is
  indistinguishable from the operator's choice); `15-backend-url.envsh` reads `MUSIZEER_BACKEND_URL` and defaults to
  `http://app:8000`. `NGINX_ENVSUBST_FILTER` lists the new names.

### 5. Two meanings of one name

`MUSIZERR_VERSION` was both the compose selector of the image tag and the version baked into the image. They stay two
things: `MUSIZEER_VERSION` in `.env` selects the tag (`${MUSIZEER_VERSION:-latest}` in compose), and the image's baked value is
the build argument of the same name, read by `app_version.py`. They never meet at run time (compose does not forward the
selector). The registry documents both under one entry with the two roles.

## Implementation plan

All of it ships in the single breaking release of ADR-0010 (one pull request into `dev`); the commits inside it are ordered
so each can be reviewed alone.

1. **Registries and single readers** (no name change): `app/core/environment.py`, `gateway/musizeer_gateway/environment.py`,
   every `os.environ` read goes through them (`REDIS_URL` once instead of four times), the layering test, the equality test
   between the two registries.
2. **The names**: the table of this record in both registries, the `REMOVED` table with its start-time error and the failing
   start for required variables, the typo guard, tests for each (an old file produces the named error; a required one
   fails; an unknown `MUSIZEER_` variable is reported).
3. **Compose, `.env.example`, frontend image, developer and build compose files, scripts, CI workflows**: section 4, the
   forwarded-but-missing variables, `scripts/package-release-assets.sh` (it rewrites a line of the compose file and checks
   it), the build arguments of `.github/workflows/images.yml`.
4. **ADR-0008 retires `WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` and `GATEWAY_FILESYSTEM_ROOTS`** (steps 1 and 2 of its plan);
   the compose change of step 3 depends on it.
5. **Documentation**: `docs/deployment.md` and its generated reference table, `CLAUDE.md`, the other `docs/*.md`.

## Alternatives considered

- **Aliases for one MAJOR** (the first draft: new names with the old ones read as a fallback, a precedence, a conflict rule
  for secrets, compose forwarding both, an Administration notice, a removal question). A compatibility window for an install
  base of one. Rejected by the maintainer; the start-time message of §3 replaces all of it.
- **Prefix everything, including `DATABASE_URL`, `REDIS_URL`, `POSTGRES_*`, `TZ`.** Consistent on paper. `POSTGRES_*` would
  need a second name for the same value to reach the `postgres` image, platforms that inject `DATABASE_URL` would stop
  working untouched, and `TZ` is read by the C library. Rejected.
- **Keep `GATEWAY_FILESYSTEM_ROOTS` as an optional ceiling** (the first draft of ADR-0008). Every environment value that
  bounds a choice is one more thing to keep equal to the database; the mounts already bound what the gateway can see.
  Removed by the maintainer's decision.
- **Read the new names only in compose and translate to the old ones.** No code change in the application, but then an
  operator of Kubernetes or `docker run` gets nothing and the rename is invisible to the code. Rejected.
- **A configuration file instead of variables.** A bigger change (a second format to document and secure). Not needed to fix
  what was observed.
- **Move more into the database (the public URL, the database tuning).** The public URL is needed before login to build the
  OAuth redirect and is registered at third parties; database tuning is needed to open the database. Both fail rule 1 of
  the boundary.
- **One shared registry module in `musizeer_sdk`.** Avoids the duplicate, but puts deployment knowledge in the providers'
  contract package. Rejected; the equality test replaces it.

## Consequences

- Names stop clashing in a shared environment; `MUSIZEER_` is greppable and documented in one table that a test keeps true.
- The boundary has a rule that a reviewer can apply, and the first case it settles (the workspace) is gone as a source of
  drift together with the three folder variables.
- Seven variables that did nothing from `.env` now work.
- No alias code exists: an old `.env` produces an error naming the new variable, and a missing required secret fails the
  start with that name.
- The owner rewrites `.env` once (ADR-0010 §6 says how, for a dedicated `.env` and for one shared with other services); the same step removes four lines.
- Documentation and scripts churn once, inside the rename.

## Questions for the maintainer

1. **`APP_PORT` becomes `MUSIZEER_PORT`.** *Recommended:* yes (it is the variable that collides in Kubernetes).
2. **The safety net fails the start only when the new variable is required.** For a variable that has a default (the public
   URL, say) the process starts with the default and logs the error, so a forgotten rename is visible but does not take the
   site down. *Recommended:* yes.
3. **Developer compose names:** `MUSIZEER_DOWNLOADS_DIR` and `MUSIZEER_LIBRARY_DIR` replace `MUSIC_DIR`, the ports become
   `MUSIZEER_DEV_*`. *Recommended:* yes, in the same pull request as the production compose.
4. **No prefixed override of `DATABASE_URL` and `REDIS_URL`** (§2). *Recommended:* yes, none.
5. **The production compose gets a second named volume, `library`, at `/library` in the gateway** so that a new install has
   the two folders of ADR-0008 without an override file. *Recommended:* yes; an administrator swaps in a bind mount in the
   override file as today.

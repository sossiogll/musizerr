# ADR-0009: The configuration surface: deployment versus application, and the `MUSIZEER_` environment names

**Status:** Proposed
**Date:** 2026-10-24
**Deciders:** @sossiogll (maintainer)
**Related:** ADR-0007 (what a MAJOR and a MINOR mean, rollback of one minor), ADR-0008 (root folders: the first
user of the boundary rule below), ADR-0006 (the gateway)

## Context

Musizerr is configured in two places: the process environment (set by whoever runs the containers) and the
settings (the database, set in the UI). The line between them was never drawn, and the names of the environment variables
were never chosen: they grew one by one. Three things made that visible on the owner's home-server install.

1. **One fact, two homes.** The workspace folder is both an environment default and a setting, and the stored value wins
   (`app/core/settings_catalog.py:457-466,649`); the gateway reads it from a third variable. They drifted and the
   full-sync check broke (pull request #50 added a single `WORKSPACE_DIR` for the environment side; ADR-0008 removes the
   second home).
2. **Names that clash in a shared environment.** The owner runs Musizerr next to other containers and wants a prefix "to avoid clashes in clusters".
   The risk is real: `APP_PORT`, `DATA_DIR`, `AUTH_SECRET`, `FRONTEND_URL` and `BACKEND_URL` are names any other application may
   use; and in Kubernetes a Service called `app` injects `APP_PORT=tcp://...` into every pod of its namespace, which is
   the very name the compose file uses for the published port.
3. **A surface that does not match its documentation.** The inventory below shows variables the code reads that the
   production compose file never passes to a container, so setting them in `.env` does nothing, and `.env.example`
   documents some of them (`DB_*`).

The owner also plans to rename the product from "Musizerr" to "Musizeer" (Seerr-style naming), later, and wants the
variables now.

### Inventory: every environment variable today

Found by reading `os.environ`, `getenv`, `env.get`, `process.env`, `import.meta.env`, the Dockerfiles, the nginx
scripts and every `${...}` of the compose files. "Compose passes it" is about `docker-compose.yaml` (production):
Compose forwards to a container only what its `environment:` lists, and `.env` is used for interpolation only.

**Operator surface (documented or meant to be set by whoever deploys)**

| Variable | Read by | Default | Required | Compose passes it |
|---|---|---|---|---|
| `DATABASE_URL` | `app/db/engine.py:75`; `scripts/migrate_sqlite_to_postgres.py:540` | unset: SQLite file in `DATA_DIR` | no (compose always builds it) | yes, built from `POSTGRES_*` |
| `DATA_DIR` | `app/db/engine.py:77`; migration `0010_settings_store.py:187` | `/data` | no | yes (`/data`) |
| `REDIS_URL` | `app/services/progress.py:20`; `app/services/provider_events.py:46`; `app/workers/celery_app.py:23`; `app/workers/maintenance.py:54` (four reads, four copies of the default) | `redis://redis:6379/0` | no | yes |
| `AUTH_SECRET` | `app/core/user_auth.py:36,47` | none | yes in compose; the app refuses a weak one when authentication is on | `app` only |
| `CREDENTIALS_KEY`, `CREDENTIALS_KEY_PREVIOUS` | `app/db/credential_cipher.py:28-29,56` (the only reader) | none | `CREDENTIALS_KEY` yes, always; the previous one only during a rotation | `init`, `app`, workers, `beat`; never the gateway |
| `ADMIN_BOOTSTRAP_EMAIL`, `ADMIN_BOOTSTRAP_PASSWORD` | `app/main.py:58-59` | none | no | `app` |
| `APP_BASE_URL` | `app/core/auth_manager.py:16` | `http://localhost:8000` | no | yes, derived from `PUBLIC_URL` |
| `FRONTEND_URL` | `app/routes/auth.py:30`; `app/routes/provider_login.py:30` | `http://localhost:3000` | no | yes, derived from `PUBLIC_URL` |
| `PUBLIC_API_PREFIX` | `app/services/public_urls.py:15`; migration `0019_host_url_refs.py:95` | `/api` | no | **no** |
| `ALLOWED_ORIGINS` | `app/main.py:12` | `*` | no | **no** |
| `DB_BUSY_TIMEOUT_MS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `app/db/engine.py:58,118,130,135-136` | 15000, 120000, 5, 10 | no | **no** (yet `.env.example` documents them) |
| `MUSIZERR_UPDATE_REPOSITORY` | `app/services/update_check.py:34` | `sossiogll/musizerr` | no | **no** |
| `PROVIDER_GATEWAY_SOCKET` | `app/services/gateway_client.py:40,78` | `/run/musizerr/gateway.sock` | no | no |
| `WORKSPACE_DIR` | `app/core/settings_catalog.py:462,466` (default of the setting `workspace_dir`); compose gives it to the gateway as `GATEWAY_WORKSPACE_DIR` | `/downloads/.musizerr-work` | no | `app` and the workers; the gateway under the other name |
| `GATEWAY_STATE_DIR`, `GATEWAY_RUN_DIR`, `GATEWAY_WORKSPACE_DIR`, `GATEWAY_SANDBOX_UID`, `GATEWAY_SANDBOX_GID`, `GATEWAY_MAX_MEMORY_MB`, `GATEWAY_MAX_OPEN_FILES`, `GATEWAY_MAX_PROCESSES`, `GATEWAY_FILESYSTEM_ROOTS` | `gateway/musizerr_gateway/config.py:72-85` | image paths, uid 10001, ceilings 2048 MB / 4096 / 1024, roots empty | no | only `GATEWAY_WORKSPACE_DIR` and `GATEWAY_FILESYSTEM_ROOTS`; the rest **no** (the ceilings are documented in `docs/gateway.md:96`) |
| `BACKEND_URL`, `DNS_RESOLVER` | `musizerr-ui/Dockerfile:57-59,71`; `musizerr-ui/nginx/15-backend-url.envsh`; `musizerr-ui/nginx/default.conf.template:30,33` | `http://app:8000`, `127.0.0.11` | no | `BACKEND_URL` yes; the image sets both with `ENV`, so an unset variable cannot be told from the default |

**Compose interpolation only (never read by Musizerr code)**

| Variable | Used for | Default |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | the `postgres` service (the stock image reads them) and `DATABASE_URL` | `musizerr`, none (required), `musizerr` |
| `PUBLIC_URL` | derives `APP_BASE_URL` and `FRONTEND_URL` | `http://localhost` |
| `APP_PORT` | the published port of the web app | `80` |
| `MUSIZERR_VERSION`, `MUSIZERR_IMAGES` | the image tags and the registry/owner of the images | `latest`, `ghcr.io/sossiogll` |
| `GATEWAY_SECCOMP_PROFILE` | the path of the seccomp profile on the host | `./gateway/seccomp/musizerr-gateway.json` |
| `MUSIC_DIR`, `FRONTEND_PORT`, `OCI_REGISTRY_PORT` | `docker-compose.dev.yml` only | `./data/music`, 3000, 15000 |

**Internal (baked into images, build or test tooling; not an operator surface)**

| Variable | Where | Note |
|---|---|---|
| `MUSIZERR_VERSION`, `MUSIZERR_COMMIT` | build arguments baked as `ENV` (`Dockerfile:54-56`, `gateway/Dockerfile:32-34`); read by `app/services/app_version.py:12-13`, `gateway/musizerr_gateway/control.py:52` | the same name as the compose tag variable above, a different thing (see §6) |
| `VITE_MUSIZERR_VERSION`, `VITE_API_URL` | `musizerr-ui/Dockerfile:41-42`, `src/lib/version.ts:5`, `vite.config.ts:7` | build time / dev server; Vite exposes only `VITE_*` to the page |
| `NGINX_ENVSUBST_FILTER` | `musizerr-ui/Dockerfile:59` | the nginx image's own |
| `E2E_USERNAME`, `E2E_PASSWORD`, `E2E_ADMIN_USERNAME`, `E2E_ADMIN_PASSWORD` | `app/scripts/e2e_user.py:37-46` | tests |
| `TEST_DATABASE_URL` | the backend test suite | tests |
| `PROVIDERS_REPO`, `INTEGRATION_PROJECT`, `INTEGRATION_REGISTRY_PORT`, `FRONTEND_TEST_PROJECT`, `FRONTEND_TEST_PORT`, `GHCR_NAMESPACE`, `PROVIDER_BASE_IMAGE`, `PROVIDER_INSTALL_REGISTRY`, `DEV_CATALOG_DIR`, `PROVIDER_LABELS_OUT`, `PROVIDER_LOCAL_TAG_PREFIX`, `HOST_IMAGE`, `PROFILE_SOURCE` | `scripts/*.sh` | development and publishing scripts |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (and lower case) | **set by** the runner inside a sandbox (`sdk/src/musizerr_sdk/runner.py:337-340`) | an output, not an input |
| `TZ` | read by the C library and Python, not by Musizerr (`grep`: no reference in the repository); the owner's server sets it on the app and gateway containers | a standard |
| `provider_sources` token variable | `app/scripts/provider_sources.py:178,322`: `--token-env NAME` takes the name of any variable | user-named |

### What the inventory shows

- About twenty variables are Musizerr's own and unprefixed.
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

1. It is needed **before the database can be read**: where the database is (`DATABASE_URL`, `DATA_DIR`), Redis, the
   sockets.
2. It is a **secret that protects the stored data or the sessions** and so cannot live in the database it protects
   (`CREDENTIALS_KEY`, `AUTH_SECRET`).
3. It names something **only the party who runs the containers controls**: a port, the address of the service as the
   outside sees it (`PUBLIC_URL`: it is registered at third parties as a redirect URI), the image to run, a resource ceiling that sandboxes
   may not exceed, a security ceiling.
4. It must be **identical in several containers**, and a mismatch fails silently. Such a value is not written twice:
   one process reads it and the others ask that process (the lesson of `WORKSPACE_DIR`), or it is not an
   environment value at all (rule 3 below).

**Application settings** are the database, edited in the UI. A value belongs there when an administrator should be able to
change it at run time without access to the server, it is a policy or a choice that outlives a deployment, and nothing needs
it to boot.

Three rules keep the line from blurring:

1. **A value has one home.** No setting has an environment default. The environment may *bound* a choice (a ceiling, an allowed
   set), the database holds the choice, and the choice is validated against the bound; it is never a second copy of the
   same value. (`workspace_dir` was the counter-example; ADR-0008 turns it into a derived folder of a declared root.)
2. **One reader per process.** Every environment variable is read in one module per deployable (`app/core/environment.py`
   for the API, workers, beat and scripts; `gateway/musizerr_gateway/environment.py` for the gateway), through one
   registry of names, defaults and aliases. Nothing else calls `os.environ`. A layering test enforces it, like
   `credential_cipher.py` is already the only reader of `CREDENTIALS_KEY` (`tests/test_layering.py`). Providers read no
   environment (unchanged: the Host Boundary).
3. **Roots and folders.** The folders Musizerr works in are **application** (ADR-0008: root folders in the database, the
   mounts that make them real are the deployment's); what the environment keeps is, at most, a ceiling
   (`MUSIZEER_GATEWAY_FILESYSTEM_ROOTS`, optional). Therefore `WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` and the required
   `GATEWAY_FILESYSTEM_ROOTS` go away.

`public_base_url` (the setting: the address a delegate calls back) and `PUBLIC_URL` (the variable: the address a browser
uses) stay two things on purpose, since they are addresses for different audiences. The guide says so (`docs/deployment.md`
already does).

### 2. The prefix and what gets it

**The prefix is `MUSIZEER_`**, as the maintainer asked. Rule: *a variable that only Musizerr defines is prefixed;
a variable whose name and meaning another project defines keeps its name.*

| Group | Decision |
|---|---|
| Musizerr's own (everything in the "operator surface" table not listed below) | `MUSIZEER_` + the old name: `MUSIZEER_AUTH_SECRET`, `MUSIZEER_CREDENTIALS_KEY`, `MUSIZEER_CREDENTIALS_KEY_PREVIOUS`, `MUSIZEER_ADMIN_BOOTSTRAP_EMAIL`, `MUSIZEER_ADMIN_BOOTSTRAP_PASSWORD`, `MUSIZEER_PUBLIC_URL`, `MUSIZEER_APP_BASE_URL`, `MUSIZEER_FRONTEND_URL`, `MUSIZEER_PUBLIC_API_PREFIX`, `MUSIZEER_ALLOWED_ORIGINS`, `MUSIZEER_DATA_DIR`, `MUSIZEER_DB_BUSY_TIMEOUT_MS`, `MUSIZEER_DB_STATEMENT_TIMEOUT_MS`, `MUSIZEER_DB_POOL_SIZE`, `MUSIZEER_DB_MAX_OVERFLOW`, `MUSIZEER_BACKEND_URL`, `MUSIZEER_DNS_RESOLVER`, `MUSIZEER_GATEWAY_SECCOMP_PROFILE` |
| Gateway (`GATEWAY_*`) | `MUSIZEER_GATEWAY_*`: `_STATE_DIR`, `_RUN_DIR`, `_SANDBOX_UID`, `_SANDBOX_GID`, `_MAX_MEMORY_MB`, `_MAX_OPEN_FILES`, `_MAX_PROCESSES`, `_FILESYSTEM_ROOTS` (now an optional ceiling). `PROVIDER_GATEWAY_SOCKET` becomes `MUSIZEER_GATEWAY_SOCKET`. `GATEWAY_WORKSPACE_DIR` and `WORKSPACE_DIR` are retired (ADR-0008) |
| The old product-named ones | `MUSIZERR_VERSION` / `_IMAGES` / `_UPDATE_REPOSITORY` / `_COMMIT` become `MUSIZEER_VERSION` / `_IMAGES` / `_UPDATE_REPOSITORY` / `_COMMIT` |
| Port | `APP_PORT` becomes `MUSIZEER_PORT` (the Kubernetes clash above) |
| Development compose | `MUSIC_DIR` becomes `MUSIZEER_LIBRARY_DIR`, with `MUSIZEER_STAGING_DIR` for the download folder; `FRONTEND_PORT`, `OCI_REGISTRY_PORT` become `MUSIZEER_DEV_*` |
| **Keep their standard names** | `DATABASE_URL`, `REDIS_URL` (platforms inject them), `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (the stock `postgres` image defines them, and they go only to that container), `TZ`, `NGINX_*` (the nginx image's), `VITE_*` (Vite exposes only these to the page), `HTTP_PROXY` and friends |
| Internal and tooling | untouched in this ADR: `VITE_MUSIZERR_VERSION`, `E2E_*`, `TEST_DATABASE_URL`, the `scripts/*.sh` variables. They are not an operator surface; they change with the product name or not at all |

**The standard names get a prefixed override.** `DATABASE_URL` and `REDIS_URL` stay valid forever, with no deprecation
warning, and `MUSIZEER_DATABASE_URL` / `MUSIZEER_REDIS_URL` take precedence when set. This is the answer to a cluster where
`DATABASE_URL` is already used by another application of the same environment: Musizerr can be pointed elsewhere without
touching the other one's name.

### 3. Aliases, precedence and deprecation (ADR-0007)

ADR-0007 §1 makes "a required setting or variable added or removed" a MAJOR. Adding the new names while the old ones keep
working is a MINOR (`feature`); **reading the old names stops in the next MAJOR**, and only there.

Precedence, implemented once in the registry (rule 2):

1. The prefixed name, if set and not empty.
2. Else the legacy names of that entry in their listed order, if set and not empty. If the entry is deprecated this logs
   once per process: `AUTH_SECRET is deprecated, rename it to MUSIZEER_AUTH_SECRET (legacy names stop working in Musizerr N.0)`.
3. Else the default.

Edge cases:

- **Empty counts as unset**, as the code does today (`os.environ.get(...) or default`) and as compose does with `${X:-}`.
- **Both set and equal**: silent (compose does this on purpose, below).
- **Both set and different**: for a **secret** (`AUTH_SECRET`, `CREDENTIALS_KEY`, `CREDENTIALS_KEY_PREVIOUS`) the process
  **refuses to start**, naming both variables but not their values: silently picking one could unseal nothing or
  sign sessions with the wrong key. For any other variable the prefixed name wins and the log says both were set.
- **Typos.** A variable that starts with `MUSIZEER_` or `MUSIZERR_` and is in no registry entry (new or legacy) is logged
  at start ("unknown variable MUSIZEER_AUTH_SECERT; did you mean MUSIZEER_AUTH_SECRET?"). The two prefixes differ by one letter, so
  this guard stays useful for as long as both exist (§5).
- **Internal variables** (baked version metadata) take the new name in the same change that renames the build argument; the
  code still reads the old one for a deployed image built before. No warning: nobody sets them.

**The admin sees it.** The registry keeps what it found; Administration shows "Deprecated variables in use: `AUTH_SECRET` →
`MUSIZEER_AUTH_SECRET`" next to the update notice (admins only; names, never values), so the change does not wait for a
log line to be read.

**Duplicated code, on purpose.** The gateway imports only the standard library and `musizerr_sdk` (ADR-0006), and the SDK is
the providers' contract: a module for deployment names does not belong in it. The registry therefore exists twice, `app/core/environment.py` and
the gateway's, and a test compares the entries both declare (names, aliases, defaults) so they cannot diverge.

### 4. Compose and `.env.example`

- **Compose accepts both names in `.env`, passes both to the containers.** For every renamed variable `docker-compose.yaml` forwards
  the new name and keeps forwarding the old one, filled from whichever the operator wrote, for example
  ```yaml
  MUSIZEER_AUTH_SECRET: ${MUSIZEER_AUTH_SECRET:-}
  AUTH_SECRET: ${AUTH_SECRET:-${MUSIZEER_AUTH_SECRET:?set MUSIZEER_AUTH_SECRET in .env}}
  ```
  Three cases, all intended:
  - `.env` with the old name only: the container has `AUTH_SECRET` only (the new one is empty), so the application
    warns that it is deprecated and the administrator is told;
  - `.env` with the new name only: the container has both, equal, so there is no warning, and **the previous release, which
    knows only `AUTH_SECRET`, still starts** (ADR-0007 §5: going back one minor is a version change and nothing else; a new compose file
    must not break the old image);
  - `.env` with both and different: the secret rule above.
  The second line of the example is also how a missing secret keeps stopping `docker compose up` with a message that names the **new** variable.
- **The old images keep working with the new compose file and the new image with the old compose file** (the registry reads
  legacy names). This is the compatibility contract of the alias window.
- **Variables the code reads but compose never passed are now passed** (`MUSIZEER_PUBLIC_API_PREFIX`,
  `MUSIZEER_ALLOWED_ORIGINS`, `MUSIZEER_DB_*`, `MUSIZEER_UPDATE_REPOSITORY`, the sandbox ceilings of the gateway), each with an empty default so
  the application default applies. Fixes the `.env.example` entries that did nothing.
- **One definition of defaults**: the registry's. Compose does not repeat a default that the code has (`APP_BASE_URL`'s
  two defaults disappear: compose passes `MUSIZEER_PUBLIC_URL` and the application derives the callback and the
  frontend address from it; the three derived forms remain overridable).
- **`.env.example`** lists only the new names, grouped (required / address / database / images / advanced), with one
  line at the top: "Older names (`AUTH_SECRET`, ...) still work until Musizerr N.0; the table is in docs/deployment.md".
  `docs/deployment.md` gets the inventory above as its reference table (generated from the registry by a test that
  fails when the two differ).
- **The frontend image** stops declaring `ENV BACKEND_URL=...`/`DNS_RESOLVER=...` (a default set by the image is
  indistinguishable from the operator's choice); `15-backend-url.envsh` takes `MUSIZEER_BACKEND_URL`, falls back to
  `BACKEND_URL` with the same stderr warning, and defaults to `http://app:8000`. `NGINX_ENVSUBST_FILTER` lists the new names.
- **Container names and volumes do not change with this ADR.** The compose project name, the volumes (`musizerr_data`,
  `musizerr_downloads`...) and the `musizerr-*` container names are not environment variables; changing them risks orphaned
  data volumes and belongs to the rename (§5).

### 5. The relation to the postponed rename

The variables are the first, smallest step of a staged rename and commit to nothing after it:

| Stage | What | Status |
|---|---|---|
| 0 | environment variables (`MUSIZEER_*`), with aliases | **this ADR** |
| 1 | display name only: UI strings, page title, About, OpenAPI title; one constant for the name | later, no technical risk (the AGPL attribution text in `NOTICE.md` needs a decision) |
| 2 | distribution names: new image names published beside the old ones for one MAJOR, compose project name and volume names pinned so an upgrade finds its data | later, with deprecation notes |
| 3 | contract identifiers | **recommended never** |

What stays technical whatever happens, because a user never sees it and a third party's published image depends on it:
the Python package and import `musizerr_sdk`; the runner command `python -m musizerr_sdk.runner`; the image labels
`org.musizerr.provider.*` and the base image label; the header `X-Musizerr-Egress`; the call protocol; the GHCR package
names of the providers and the base image; the default PostgreSQL user and database name `musizerr`; Redis keys
`musizerr:*`; `/run/musizerr`, `/var/lib/musizerr-gateway`; the working folder `.musizerr-work` (a name on users' disks, ADR-0008).

If the product is **not** renamed, `MUSIZEER_` stays as the prefix; it is then a prefix that looks like a misspelling
of the product, which is the price of deciding it before the rename. The alias mechanism makes a later change of prefix
possible (a second alias list), and the typo guard in §3 is why it is not dangerous in the meantime. The one-letter
difference with the old `MUSIZERR_VERSION` is the reason that guard exists.

### 6. Two meanings of one name

`MUSIZERR_VERSION` is both the compose selector of the image tag and the version baked into the image. Renamed, they stay two things:
`MUSIZEER_VERSION` in `.env` selects the tag (`${MUSIZEER_VERSION:-${MUSIZERR_VERSION:-latest}}` in compose), and the
image's baked value is the build argument of the same name, read by `app_version.py`. They never meet at run time
(compose does not forward the selector). The registry documents both under one entry with the two roles.

## Implementation plan

Every step is a pull request into `dev`.

1. **Registry and single readers** (`chore`): `app/core/environment.py`, `gateway/musizerr_gateway/environment.py`, every
   `os.environ` read goes through them (`REDIS_URL` once instead of four times), the layering test, the typo guard,
   the secret-conflict rule. No name changes, no behaviour change.
2. **New names with aliases** (`feature`, MINOR): the table of §2 in both registries, the deprecation log, the Administration notice, tests
   for precedence, conflicts and old-image/new-compose rollback. The `docs/deployment.md` reference table and its test.
3. **Compose, `.env.example`, frontend image** (`feature`, with Upgrade notes saying "rename at your pace; nothing is
   required"): §4, the forwarded-but-missing variables, `docker-compose.dev.yml`, `docker-compose.build.yml`,
   `scripts/package-release-assets.sh` (it rewrites a line of the compose file and checks it), CI workflows
   (`MUSIZERR_VERSION` build arguments), `docs/*.md`, `CLAUDE.md`.
4. **ADR-0008 retires `WORKSPACE_DIR` and `GATEWAY_WORKSPACE_DIR`** and makes `GATEWAY_FILESYSTEM_ROOTS` optional (their steps 1-2).
5. **Next MAJOR** (`breaking`): stop reading legacy names, stop forwarding them in compose, remove the registry's legacy lists
   and the notice, with an Upgrade note listing the renames. Needs ADR-0007 step 3 (the database guard) only through ADR-0008's contract step; this
   step has no migration.

## Alternatives considered

- **A clean break now (one MAJOR).** The tidiest result and no alias code; but every install must rewrite its `.env`
  and compose file at once, two of the variables are the ones whose loss costs the credentials, and the release
  rules (ADR-0007) would put it in a MAJOR on its own. Rejected: aliases cost one small module.
- **Prefix everything, including `DATABASE_URL`, `REDIS_URL`, `POSTGRES_*`, `TZ`.** Consistent on paper. `POSTGRES_*` would need a
  second name for the same value to reach the `postgres` image, platforms that inject `DATABASE_URL` would stop working
  untouched, and `TZ` is read by the C library. Rejected for the standards; accepted as an *override* (`MUSIZEER_DATABASE_URL`,
  `MUSIZEER_REDIS_URL`).
- **Prefix `MUSIZERR_` now, `MUSIZEER_` with the rename.** Zero typo risk, one alias cycle fewer if the rename never
  happens, and six names already use it. Rejected because the maintainer asked for `MUSIZEER_` now; kept as the answer to the first question
  below if he changes his mind.
- **Read the new names only in compose and translate to the old ones.** No code change in the application, but then an operator
  of Kubernetes or `docker run` gets nothing and the rename is invisible to the code. Rejected.
- **A configuration file instead of variables.** A bigger change (a second format to document and secure). Not
  needed to fix what was observed.
- **Move more into the database (the public URL, the database tuning).** The public URL is needed before login
  to build the OAuth redirect and is registered at third parties; database tuning is needed to open the database. Both fail rule 1 of the boundary.
- **One shared registry module in `musizerr_sdk`.** Avoids the duplicate, but puts deployment knowledge in the providers'
  contract package. Rejected; the equality test replaces it.

## Consequences

- Names stop clashing in a shared environment; `MUSIZEER_` is greppable and documented in one table.
- The boundary has a rule that a reviewer can apply, and the first case it settles (the workspace) is gone as a source of drift.
- Seven variables that did nothing from `.env` now work.
- For one MAJOR the code has two names for every variable and compose passes both; secrets fail closed when
  they disagree.
- The rollback promise of ADR-0007 §5 holds across the rename (new compose with the previous image).
- Documentation and scripts churn once (`CLAUDE.md`, `docs/`, `scripts/`, CI).
- The prefix is a commitment: either the product becomes "Musizeer" or the prefix stays as is.

## Questions for the maintainer

1. **Prefix spelling.** `MUSIZEER_` as asked, knowing it differs by one letter from the six existing `MUSIZERR_` names and
   from the product as written today? *Recommended:* go ahead with `MUSIZEER_`, with the typo guard of §3; if the rename is
   dropped later, accept the prefix rather than renaming twice.
2. **Standard names.** Keep `DATABASE_URL`, `REDIS_URL` (with the prefixed override), `POSTGRES_*`, `TZ`, `NGINX_*`? *Recommended:* yes.
3. **Secrets that disagree.** Refuse to start when the old and the new name hold different secrets? *Recommended:* yes; it is
   the only case where a silent choice can lock the owner out of his credentials.
4. **Compose passes both names for one MAJOR.** Needed for the one-minor rollback; the price is a slightly noisy compose file.
   *Recommended:* yes.
5. **`APP_PORT` becomes `MUSIZEER_PORT`.** *Recommended:* yes (it is the variable that collides in Kubernetes).
6. **Dev-compose names** (`MUSIC_DIR` to `MUSIZEER_LIBRARY_DIR`/`MUSIZEER_STAGING_DIR`, the dev ports). *Recommended:* yes, in the same
   pull request as ADR-0008's compose changes, so the developer stack gets two folders like production.
7. **The Administration notice for deprecated variables.** *Recommended:* yes, admins only, names only.
8. **When is "next MAJOR"?** The legacy names go with the first `breaking` release after both this ADR and ADR-0008's
   contract step are implemented. *Recommended:* bundle them so operators rewrite their files once.

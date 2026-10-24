# ADR-0010: Rename the product from Musizerr to Musizeer, in one breaking release

**Status:** Accepted by the maintainer on 2026-10-25
**Date:** 2026-10-25 (drafted 2026-10-24)
**Deciders:** @sossiogll (maintainer)
**Related:** ADR-0007 (what a MAJOR is; the provider contract is the SDK major), ADR-0009 (the `MUSIZEER_` environment
names; its former §5, a staged rename, is superseded by this record), ADR-0008 (root folders and the `folder` setting
type, which lands in the same SDK major), ADR-0006 (images, labels, the gateway)

Records written before this one (ADR-0001 to ADR-0007) keep the name they were written under: they are history and are
not edited for the rename. Their paths and identifiers (`musizerr_sdk`, `MUSIZERR_VERSION`...) are the ones of the code
before this release. ADR-0008 and ADR-0009 cite code as it is today (file and line) and name things as they will be
called after it.

## Context

The maintainer decided on 2026-10-24 that the product is called **Musizeer**. Three spellings follow from it:

| Form | Used for | Example |
|---|---|---|
| `Musizeer` | what a person reads: UI, documentation, the licence notice | "About Musizeer" |
| `musizeer` | identifiers: packages, repositories, images, paths, keys | `musizeer_sdk`, `ghcr.io/sossiogll/musizeer` |
| `MUSIZEER` | environment variables and constants | `MUSIZEER_AUTH_SECRET` |

It differs from the old name by one letter (`Musizerr`), which is the reason for the typo guard of ADR-0009.

**Nobody uses the product yet**, except the owner's home server, which is the only install. That changes what the rename
costs. The first draft of ADR-0009 (§5) staged it for people who do not exist: aliases, a window where both names work,
"never rename the contract identifiers". Every one of those protections is code to write, test and later remove, and the
price of a rename only grows with each new install, third-party provider and image built on SDK 5. The decision is the
opposite: rename everything once, now, and accept that what was built before stops working.

## Decision

### 1. One break, no window

- The application is renamed **everywhere** and switches directly to the new names.
- **No aliases, no legacy names read, no dual writes, no deprecation notices, no expand-then-contract window.** The code
  that reads, writes or answers to an old name is deleted in the same change, not left behind.
- **Old provider images may stop working; that is accepted.** The provider contract changes (§3), so every image built on
  SDK 5 is refused by the new gateway and replaced by a rebuilt one.
- It ships as **one release labelled `breaking`** (ADR-0007 §6: a MAJOR). The release notes carry one "Upgrade notes"
  section that is the procedure of §6, and the release is marked `manual_steps`.
- The one cheap protection kept is a start-time message for an old environment variable (ADR-0009 §3), because a variable
  that is silently ignored can lock the owner out of the credentials.

### 2. The inventory: every place the name appears

Found by `git grep -i musizerr` at commit `6769ec2`, excluding `docs/adr/` (the records, above): **1438 lines in 381 files**
of the main repository, **522 / 87** in `musizerr-providers`, **382 / 64** in `musizerr-providers-private` and **67 / 12**
in `musizerr-provider-template`. By area of the main repository: `app/` 625 / 227 (356 / 124 of them in tests), `gateway/`
166 / 39, `musizerr-ui/` 159 / 42, repository root files 144 / 9, `sdk/` 123 / 30, `docs/` 105 / 12, `scripts/` 79 / 10,
`.github/` 25 / 4, `e2e/` 8 / 7, `provider-base/` 4 / 1. The first number is lines, the second files. A line can be in
several rows below. The new form is the old one with `musizeer` for `musizerr`, unless the row says otherwise.

| # | Category | Today | New | Count (lines / files) |
|---|---|---|---|---|
| 1 | Display name in prose: UI strings (both dictionaries, `index.html` title, About, logo, the canvas text), OpenAPI `info.title` and descriptions, README, `docs/`, `CLAUDE.md`, log lines, error messages, comments and docstrings; the Italian setting labels that say "musizerr" in lower case | `Musizerr` | `Musizeer` | 251 / 85 (capitalised), 39 / 17 (`MUSIZERR`), `messages.ts` alone 61 |
| 2 | Licence attribution: `NOTICE.md` (the owner's additional terms under AGPL section 7, including the notice "Based on Musizerr — https://github.com/sossiogll/musizerr"), the About page text that repeats them, the LGPL line of the SDK | `Musizerr` | `Musizeer`, notice "Based on Musizeer — https://github.com/sossiogll/musizeer" | `NOTICE.md` 9, About 6, SDK `pyproject.toml` 2 |
| 3 | GitHub repositories | `sossiogll/musizerr`, `musizerr-providers`, `musizerr-providers-private`, `musizerr-provider-template` | `sossiogll/musizeer`, `musizeer-providers`, `musizeer-providers-private`, `musizeer-provider-template` | `sossiogll/musizerr` 31 / 19, `musizerr-providers*` and the template 14 / 7 in the main repository; 45 + 24 + 4 in the others |
| 4 | GHCR images, 16 packages: `musizerr`, `musizerr-gateway`, `musizerr-frontend`, `musizerr-provider-base` (tag `:5` becomes `:6`) and one `musizerr-provider-<name>` per provider (the public providers and the private ones; each keeps its visibility). The local tags `musizerr:local`, `musizerr-gateway:local`, `musizerr-frontend:local`, the local provider tag prefix, the build `LABEL org.opencontainers.image.source` | `ghcr.io/sossiogll/musizerr*` | `ghcr.io/sossiogll/musizeer*` | `ghcr.io/sossiogll` 26 / 19, `musizerr-provider*` 74 / 32 in the main repository; 25 + 14 + 2 in the others |
| 5 | Catalogs: the `image` of every entry, the catalog `name` ("Musizerr providers", "Musizerr development catalog"), the digests (they change with the rebuild) | as left | `musizeer-provider-<name>`, "Musizeer providers" | `catalog.json` of the three provider repositories, the dev catalog written by `scripts/dev-catalog.sh` |
| 6 | Python packages and imports: the SDK distribution and package (`musizerr-sdk`, `musizerr_sdk`, directory `sdk/src/musizerr_sdk/`, 23 files), the gateway (`musizerr-gateway`, `musizerr_gateway`, directory `gateway/musizerr_gateway/`, 14 files), every `import`/`from` of them, the provider packages (`musizerr_provider_<name>`: 8 + 7 + 1 directories in the provider repositories) | `musizerr_sdk` | `musizeer_sdk` | `musizerr_sdk` 647 / 249, `musizerr_gateway` 62 / 26, distribution names 27 / 19; in the provider repositories 231 + 167 + 16 lines of `musizerr_sdk` and 71 + 71 + 5 of `musizerr_provider_`. Three old Alembic migrations import or cite it (0025, 0026, 0037) and are edited, their ids and file names do not change |
| 7 | Runner and CLI: `python -m musizerr_sdk.runner` (the command the gateway starts in a sandbox), `python -m musizerr_gateway`, `python -m musizerr_gateway.ctl` | as left | `python -m musizeer_sdk.runner`, `musizeer_gateway`, `musizeer_gateway.ctl` | `musizerr_sdk.runner` 25 lines, `ctl` 17 |
| 8 | Image labels | `org.musizerr.provider.*` (`name`, `roles`, `api-version`, `sdk-version`, `label`...), `org.musizerr.provider-base.api-version` | `org.musizeer.provider.*`, `org.musizeer.provider-base.api-version` | 28 / 15 (`sdk/src/musizerr_sdk/labels.py:22`, `provider-base/Dockerfile:8`, `scripts/build-provider-image.sh`) |
| 9 | Wire-level names: the egress header, the egress proxy and sockets | `X-Musizerr-Egress` (`sdk/src/musizerr_sdk/failures.py:60`) | `X-Musizeer-Egress` | 8 / 4 |
| 10 | Redis keys and channels | `musizerr:progress`, `musizerr:providers`, `musizerr:provider-login:`, `musizerr:provider-slot:`, `musizerr:<scope>-errors:` and `-cooldown:`, `musizerr:worker:heartbeat:`, `musizerr:delegation-reconcile-lock:` | `musizeer:*` | 21 / 14. Transient data; the new stack has its own Redis volume, nothing to carry over |
| 11 | Paths inside the containers | `/run/musizerr` (sockets, `gateway.sock`), `/var/lib/musizerr-gateway` (the layer cache), `.musizerr-work` (the working folder name), `/data/musizerr.db` (the SQLite file) | `/run/musizeer`, `/var/lib/musizeer-gateway`, `.musizeer-work`, `musizeer.db` | 27 / 16, 11 / 10, 22 / 15, 16 / 10. The working folder name is data of ADR-0008 (a setting value), not a fixed name any more |
| 12 | Container and OS users, uid unchanged | OS user `musizerr` (uid 1000) in `Dockerfile` and `gateway/Dockerfile` | `musizeer`, **still uid 1000**, so the ownership of the volumes and of the folders the owner `chown`ed does not change | 7 / 2 |
| 13 | Compose: container names (`musizerr-app-1`... in the release compose; `container_name: musizerr-*` in a shared file), the project name (the release compose has no `name:`, so it is the folder's) and with it the network `musizerr_default`, and the six named volumes (`data`, `downloads`, `redis_data`, `postgres_data`, `provider_sockets`, `gateway_state`; the owner's shared file has `homeserver_musizerr_<volume>`), the dev and build compose files | `musizerr_<volume>` | in the **release compose** `name: musizeer` is pinned, so volumes are `musizeer_<volume>` whatever the folder is called. A **shared file keeps its own project name**: its volumes are renamed by declaring new volume keys `musizeer_<volume>` and its containers `musizeer-*` | 17 / 6 |
| 14 | PostgreSQL defaults: user and database | `POSTGRES_USER`, `POSTGRES_DB` default `musizerr`; the same in `DATABASE_URL`, docs and `.env.example` | default `musizeer` | 33 / 18 |
| 15 | Seccomp profile and release assets | `gateway/seccomp/musizerr-gateway.json`; the release asset `musizerr-gateway.seccomp.json`; `docker-compose.yaml` and `env.example` of the release | `gateway/seccomp/musizeer-gateway.json`, `musizeer-gateway.seccomp.json` | 38 / 16 (compose files, `scripts/package-release-assets.sh`, `.github/workflows/images.yml`, `ci.yml:163`, docs) |
| 16 | Environment variables | `MUSIZERR_VERSION`, `MUSIZERR_IMAGES`, `MUSIZERR_COMMIT`, `MUSIZERR_UPDATE_REPOSITORY`, `VITE_MUSIZERR_VERSION` and every unprefixed one of ADR-0009 | `MUSIZEER_*`, `VITE_MUSIZEER_VERSION` | 38 / 16 for the six that already carry the name; the rest is the table of ADR-0009 |
| 17 | Workflows, scripts, tooling: `.github/workflows/images.yml` (image matrix, job `musizerr-provider-base`, release title, tag message, `MUSIZERR_*` build arguments, asset names), `ci.yml`, `dependabot.yml`, `pull_request_template.md`; `scripts/*.sh` (`build-provider-image.sh`, `dev-catalog.sh`, `package-release-assets.sh`, `publish-provider-base.sh`, `publish-provider-image.sh`, `test-*-integration.sh`) with the buildx builder `musizerr-publish`, the default `PROVIDERS_REPO=../musizerr-providers`, catalog name defaults | as left | `musizeer-*`, `../musizeer-providers` | `.github/` 25 / 4, `scripts/` 79 / 10 |
| 18 | Frontend browser storage and cookies. Local storage: `musizerr-theme`, `musizerr-lang`, `musizerr-play-mode`, `musizerr.refresh_token`, `musizerr.query-cache`, `musizerr.login-redirect`; the Web Lock `musizerr.refresh`; cookies `musizerr_stream_token` and `musizerr_login_nonce` (also named in `app/openapi.yaml:89,3212`) | `musizerr…` | `musizeer…` | 35 / 21 + 7 / 4. **Consequence: everyone is signed out and signs in again** (the refresh token is stored under a key the new page does not read), and the theme, language, play mode and cached queries are forgotten once |
| 19 | The `ui` package and folder | `musizerr-ui/` (294 files), package name `musizerr-ui`, `e2e` package `musizerr-e2e`, Dockerfile contexts in compose, CI and dependabot | `musizeer-ui/`, `musizeer-e2e` | 24 / 14 references to the folder |
| 20 | End-to-end tests | `E2E_APP_CONTAINER=musizerr-app-1`, `E2E_NETWORK=musizerr_default`, the storage keys of row 18 in the specs | `musizeer-app-1`, `musizeer_default` | 8 / 7 + `e2e/.env.example` |
| 21 | A value stored in the database: `duplicate_members.origin = "musizerr"` (the file was put in the library by this application), read by `routes/library_duplicates.py:66,142`, written by `db/duplicate_store.py:236`, returned by the API and compared in the UI (`routes/admin/duplicates.tsx:238`; dictionary key `origin_musizerr`) | `"musizerr"` | `"musizeer"`, with a data migration `UPDATE duplicate_members SET origin = 'musizeer' WHERE origin = 'musizerr'` | 19 / 15 |
| 22 | Other values in the database that carry the name: `installed_providers.image`, `provider_sources.url`/`name` (re-created by the procedure, §6), the setting `workspace_dir` (`.musizerr-work`, converted by ADR-0008) | as left | re-created / converted | rows, not text |
| 23 | Small ones: the Celery application name (`workers/celery_app.py:26`), the HTTP `User-Agent` (`services/sources.py:104`, `services/update_check.py:91`), the probe folder and file of the full-sync check (`Musizerr Probe/musizerr-probe-<token>.ogg`, `workers/maintenance.py:89-95`), `app_version`, the default `MUSIZEER_UPDATE_REPOSITORY` (`sossiogll/musizeer`) | as left | `musizeer…` | a few lines each |

**What does not change, and why.** Nothing in the call protocol carries the name (the frames, the `wire` type tags, the
manifest field names), so `PROTOCOL_VERSION` stays 1. The Alembic revision ids and file names, the table and column
names, the `enc:v1:` sealing format, the `hostref:` prefix and the `PUBLIC_URL`-based OAuth redirect addresses
(`<PUBLIC_URL>/api/auth/<provider>/callback`) contain no name: every OAuth application registered at a third party keeps
working, and so does every webhook registered in a delegate.

**What is not renamed on purpose.** The two private archive repositories (the remotes called `archive`) are history, not
product. The local folder names of the owner's checkouts are the owner's business.

**The licence attribution (row 2)** is the owner's own additional terms, so it changes with the name: from this release on
the notice reads "Based on Musizeer — https://github.com/sossiogll/musizeer", in the README and on the About page as
the terms require. Copies already conveyed keep the notice they were conveyed with. **Decided: the name is Musizeer,
the `NOTICE.md` and attribution text included** (the maintainer confirmed it on 2026-10-25); this record does not give
legal advice.

### 3. The contract: SDK 6

The package rename changes the import every provider writes and the label every image carries, so **every existing
provider image is incompatible by construction**. ADR-0007 §1 makes the SDK major the contract version, therefore:

- `PROVIDER_API_VERSION` and the SDK major go to **6** (`sdk/pyproject.toml` 6.0.0, `SDK_VERSION = "6.0.0"`); the base
  image is `ghcr.io/sossiogll/musizeer-provider-base:6`; its label is `org.musizeer.provider-base.api-version="6"`.
- The gateway refuses an image whose labels do not say 6 (`gateway/musizerr_gateway/images.py:56`, an equality test that
  stays), and an image built on SDK 5 has the old label prefix, so it does not even describe itself to the new gateway. The
  catalog check lists an entry of another contract and does not install it (`app/core/catalog.py:97-105`, as today).
- **ADR-0007 §3.2 (two majors overlap, an adapter, catalog format 3) is not used for this major.** The adapter would have to
  read the old labels and the old import path, and its only beneficiary would be an install that does not exist. The
  supported set is `{6}`. The previous major stays "supported until the next MAJOR" as a rule for the future, with this one
  recorded exception.
- The providers are **rebuilt on SDK 6 and republished** (their own major version), and the catalogs are regenerated with
  the new image names and digests.
- **Nothing else in the contract changes in 6.0, with one possible exception: the `folder` setting type of ADR-0008**
  (`ProviderSetting(type="folder")`, its conformance rule, and the declaration `StorageProvider.library_folder_setting()`).
  A new member of a type is not an addition ADR-0007 §3.1 allows within a major, which is why it can only land in a major.
  **Recommended: bundle it.** Both changes break every provider, and a second break one release later would make the
  owner's providers go through this procedure twice.
- Because no peer of major 5 is supported, the `wire.SINCE` markers `"5.5"` can be folded into the base of 6.0 and
  `BASELINE` becomes 6.0. This is a simplification of the codec, not a requirement of the rename.

### 4. What the release contains

The main repository in **one pull request into `dev`** (`breaking`), so that no intermediate state is ever released:

1. the rename of every row of §2 (directory moves with `git mv`, so history follows);
2. SDK 6 (§3), including the `folder` type, the conformance rule and the documentation that `StorageProvider.store`
   must make the file appear atomically (ADR-0008 §6);
3. the gateway: package rename, the roots and the derived workspace of ADR-0008 (steps 1 to 4 of its plan);
4. the environment names, the registries, the safety net, compose, `.env.example` (ADR-0009);
5. the data migration of row 21 and the conversion of the working folder and folder settings (ADR-0008);
6. the documentation (`README.md`, `CLAUDE.md`, `docs/`, the ADR index), which now says Musizeer.

What is **not** in this release and follows as ordinary minors (non-breaking): the delegate mapping and the full-sync check
attached to the library folder (ADR-0008 steps 5 and 6).

### 5. Order of work and who does what

| Step | What | Who |
|---|---|---|
| 1 | The pull request of §4 into `dev`, reviewed and merged; it publishes nothing (images are built on a merge into master) | the maintainer reviews; the work is done in the repository |
| 2 | The three provider repositories rebuilt on SDK 6: `musizeer_provider_<name>` packages, imports, `FROM …/musizeer-provider-base:6`, the local provider on the `folder` type and an atomic `store`, versions raised, tests, and a trial through `scripts/dev-catalog.sh` against the checkout of step 1. One pull request per repository | the work is done in the repositories; the maintainer reviews |
| 3 | **Renaming on GitHub and GHCR (below).** The maintainer delegated these: we rename the four repositories, publish the new packages with the visibility of the old ones, with the maintainer's standing permission (no confirmation per action) | us |
| 4 | The release: merge `dev` into `master` (the workflow tags the MAJOR, pushes `musizeer`, `musizeer-gateway`, `musizeer-frontend` and the base image `:6`, creates the release with its assets); publish the providers (`scripts/publish.sh` of each providers repository) and pin the digests in the catalogs | the maintainer merges `dev` into `master`; the scripts do the rest |
| 5 | **The maintainer's server (§6)**: the new block is added, the data moved and verified. Deleting the old block, volumes, `.env` lines and images on the server is done by the maintainer, or by us with the maintainer's explicit go | the maintainer, with us |
| 6 | **After the maintainer says the server is verified:** the old GHCR packages are deleted (by us, under the standing permission of step 3) | us |

**Step 3 in detail.**

- Rename the four repositories. GitHub redirects the old web, `git` and API addresses to the new ones, so local checkouts,
  open pull requests (#54 included), issues and stars keep working; `git remote set-url` is still advisable. The redirect
  lasts until someone creates a repository under the old name.
- **The redirect does not cover GHCR, and a container package cannot be renamed at all.** Renaming a repository does not
  rename its packages, and GitHub offers no rename for a package, so `ghcr.io/sossiogll/musizerr` never becomes
  `ghcr.io/sossiogll/musizeer`. **The new names are published as new packages**, created by the first push of the release
  (step 4). **There is no retention period**: the old packages are deleted once the maintainer has verified the server
  (step 6 of the table), by us, under the standing permission. A new package is **private** (nothing sets its visibility,
  and GitHub has no API for it): we give each new package the visibility of the old one (the app, gateway, frontend, base and public providers public; the private providers private) in the GitHub package settings, and check that the
  repository is linked to each one (the `org.opencontainers.image.source` label names it). Because the old packages go,
  a rollback does not pull old images: it rebuilds them from the last tag before the rename (§7).
- **The redirect does not cover the catalog fetch either**: the host fetches a catalog without following redirects
  (`app/services/sources.py:106`, `follow_redirects=False`, on purpose). The catalog URL of the old source stops answering.
  The procedure adds the new URL as a new source.
- Do this **before** step 4. The release pushes images whose source label names the renamed repository, and the first push
  of a package is what links it to that repository and gives the workflow its right to push.
- Secrets, variables, branch protection and the required checks of the repositories survive a rename; the owner checks that
  no required check is named after a renamed job (`images.yml:175`, the matrix jobs named after the images, are not
  required checks today).

### 6. The owner's server: step by step

**Two shapes of install.** The release compose file (`docker-compose.yaml`) runs the product alone in a folder of its own,
and its project name is pinned to `musizeer` (row 13). The owner's server is the other shape: the product is **a block of
services inside a shared compose file**, next to a media server, Lidarr and others, and the file keeps its own project
name. The procedure is written for the shared file, because it is the harder case; with the release compose in a folder of
its own it is the same steps on the whole file (and `down` is then harmless, because nothing else is in it).

**Rules for the procedure.**

- **Never stop, recreate or touch the other services of a shared file.** Every command names the services it acts on:
  `docker compose stop <old services>`, `docker compose up -d <new services>`. Never a bare `docker compose up -d` (it
  reconciles every service of the file), never `down`, never `--remove-orphans`.
- **The `name: musizeer` pin belongs to the release compose only.** A shared file keeps its project name. Its volumes are
  renamed by **declaring new volume keys** (`musizeer_data`, `musizeer_downloads`, ...), which Compose prefixes with the
  project name like the old ones; its services and `container_name`s get the new names in the same way.
- **Nothing assumes a folder or a project name.** Step 0 finds them.
- The old block stays in the file, stopped, only until the maintainer has verified the new one (step 9); then it is
  deleted (step 10). There is no retention period. After that the rollback is the old code and the dump (§7).
- **The credentials key does not change: sealed credentials stay readable** (the `enc:v1:` rows do not carry the name, and
  `MUSIZEER_CREDENTIALS_KEY` takes the same value). New volume keys mean **new volumes**.

**Recommended: move the database with `pg_dump` and restore it into the new database and user.** The alternative, copying
the old postgres volume into the new one, keeps the PostgreSQL role and database called `musizerr` (they are created once,
at the first start of an empty volume; changing `POSTGRES_USER` later does nothing), so the new block would carry the old
name for ever. A dump is also the backup that makes the rollback possible, and the migrations of this release run on the
copy, never on the old volume.

**The owner's case as the worked example.** The compose file is `/home/sossiogll/docker/docker-compose.yml` with its `.env`
beside it; the project is `homeserver`; the volumes are `homeserver_musizerr_data`, `homeserver_musizerr_downloads`,
`homeserver_musizerr_gateway_state`, `homeserver_musizerr_postgres_data`, `homeserver_musizerr_provider_sockets` and
`homeserver_musizerr_redis_data`, so the volume keys in the file are `musizerr_data`, `musizerr_downloads`... and the new
ones are `musizeer_data`... giving `homeserver_musizeer_data`...; the containers are named `musizerr-*` by `container_name`.

0. **Find your names.**
   ```sh
   cd <the folder of your compose file>
   docker compose ls                                                   # your project name (homeserver)
   docker compose ps --format '{{.Service}}  {{.Name}}' | grep -i musizerr   # the services and containers of the block
   docker volume ls | grep musizerr                                    # its volumes (the six above)
   ```
   Write down the list of the old services (the init job, the application, the three workers or the worker service, beat, the
   web app, Redis, PostgreSQL, the gateway) and the new names you will give them. The commands below use `<old …>` and `<new …>`
   for these lists.
1. **Prepare.** Copy the compose file and `.env` aside (`cp docker-compose.yml docker-compose.yml.pre-musizeer`, the same for
   `.env`), create `/home/sossiogll/docker/backups` (mode 700) for the dump, and note the version the old block runs. Check that the release is published, that the 16 new packages are
   public and that the catalogs are published.
2. **Add the new block beside the old one** in the same file, from the release's `docker-compose.yaml` as the template: new
   service keys and `container_name`s (`musizeer-*`), new volume keys (`musizeer_*`), the **same bind mounts as today** for
   the music (staging at `/downloads` in the application, the workers, beat and the gateway; the library at `/library` in the
   gateway only; ADR-0008), no `GATEWAY_FILESYSTEM_ROOTS` and no `WORKSPACE_DIR`, the new image names, and the seccomp
   profile `musizeer-gateway.seccomp.json` from the release placed next to the file. Give the web app the port or the proxy
   entry the old one had; the old one is stopped before the new one starts, so the same port is fine.
   Check it without starting anything: `docker compose config -q`.
3. **Add the new environment names to `.env`, keeping the old lines** (the old block, stopped, still refers to them). The
   table is ADR-0009's. In a shared `.env` some old names (`PUBLIC_URL`, `DATA_DIR`, `APP_PORT`, `POSTGRES_*`) may belong to
   other services: copy the values of the ones the Musizerr block used into their new names by hand (`MUSIZEER_AUTH_SECRET`,
   `MUSIZEER_CREDENTIALS_KEY` **with the same value**, `MUSIZEER_ADMIN_BOOTSTRAP_*`, `MUSIZEER_PUBLIC_URL`, `MUSIZEER_PORT`,
   `POSTGRES_PASSWORD` for the new PostgreSQL), and leave every other line alone. (A dedicated `.env` can be converted with one
   `sed`: `sed -E 's/^(AUTH_SECRET|CREDENTIALS_KEY|CREDENTIALS_KEY_PREVIOUS|ADMIN_BOOTSTRAP_EMAIL|ADMIN_BOOTSTRAP_PASSWORD|PUBLIC_URL|APP_BASE_URL|FRONTEND_URL|DATA_DIR|PUBLIC_API_PREFIX|ALLOWED_ORIGINS|DB_[A-Z_]+|GATEWAY_[A-Z_]+)=/MUSIZEER_\1=/; s/^APP_PORT=/MUSIZEER_PORT=/; s/^MUSIZERR_/MUSIZEER_/'`
   and the removed variables deleted.) Do not set `POSTGRES_USER` or `POSTGRES_DB` for the new block: its defaults are
   `musizeer`.
4. **Stop the old writers, dump the database, stop the rest of the old block.** Only the old services, by name:
   ```sh
   docker compose stop <old web app> <old application> <old workers> <old beat>
   docker exec <old postgres container> pg_dump -U musizerr -Fc musizerr > /home/sossiogll/docker/backups/musizerr-pre-musizeer-$(date +%F).dump
   pg_restore --list /home/sossiogll/docker/backups/musizerr-pre-musizeer-$(date +%F).dump | head          # it reads: the dump is not empty
   docker compose stop <old gateway> <old redis> <old postgres>  # stop, not rm: the containers and volumes stay
   ```
   Look inside the old data volume once (`docker run --rm -v homeserver_musizerr_data:/d alpine ls -la /d`): with PostgreSQL it
   holds nothing the database does not, so it is not copied.
5. **Create the new database and restore it.**
   ```sh
   docker compose up -d --no-deps <new postgres>
   docker exec -i <new postgres container> pg_restore -U musizeer -d musizeer --no-owner --no-acl < /home/sossiogll/docker/backups/musizerr-pre-musizeer-YYYY-MM-DD.dump
   ```
6. **Start the new block**, listing its services: `docker compose pull <new services>` then `docker compose up -d <new services>`.
   `docker compose logs <new init>` shows the migrations (the `origin` value, the roots). A variable of the old table that
   a new service still receives is reported by name (ADR-0009 §3). `docker exec <new gateway container> python -m
   musizeer_gateway.ctl health` must say `"available": true`. If Lidarr (or another program) reaches the web app by its
   old container or service name, change the setting `public_base_url` and register the webhook again (Administration,
   delegation webhook); the same for a reverse proxy that names the old container.
7. **Providers: add the new source and install again.** The installed rows point at images that no longer run. In
   Administration › Sources add the URL of the new catalog, remove the old source (this uninstalls its providers: **their
   settings and their credentials are stored by provider name and stay**), then install each provider from the new one
   (or, from the shell, `docker exec <new application container> python -m scripts.provider_sources add-source <url>`,
   `uninstall <name>` and `install <source> <name> --yes` for the twelve; removing the old source is a page action). The
   permissions are the same ones; the admin sees and approves them as at any install. The new gateway-state volume is
   empty, so it pulls the images again. `auto_update_providers` is not relied on (ADR-0007 §5.1 plans it; the code has no
   such setting yet).
8. **Folders.** Administration › Root folders shows what the conversion made of the old values (ADR-0008): Downloads
   `/downloads`, Library `/library`, the working folder Downloads + `.musizeer-work`, local storage Library + empty path.
   Run the checks; map Lidarr's `/music` to the library when that step exists; run the full-sync verification for Navidrome.
9. **Verify before anything is deleted.** Everyone signs in again (expected, row 18); the library page shows the same
   counts; a provider that needs an account still says "connected"; one download completes and appears in the library; a
   track streams; a delegated download is accepted by Lidarr and its webhook arrives; the full-sync verification passes; the
   update notice at the top of Administration shows the new version; the other services of the file were never restarted
   (`docker compose ps` shows their uptime). The maintainer says when this is done.
10. **Delete the old, with the maintainer's go** (or by the maintainer): remove the old block and the old variables from the
    file and `.env`, `docker compose rm <old services>`, `docker volume rm` the six old `homeserver_musizerr_*` volumes, remove
    the old images. Then the old GHCR packages are deleted (they cannot be renamed; the new ones are separate packages).
    **There is no retention period.** Keep the dump (below).

**The dump is the one thing that is kept.** It is taken in step 4 to
`/home/sossiogll/docker/backups/musizerr-pre-musizeer-YYYY-MM-DD.dump` on the server (mode 600), and copied to a second
machine of the maintainer, together with the value of `CREDENTIALS_KEY` (the dump holds the credentials sealed, so it is
useless for the accounts without the key; the key goes where the maintainer keeps secrets, never next to the dump in
clear). It is not deleted by the clean-up.

### 7. Rollback

**Before step 10** the old block is intact and the rollback is two commands: its volumes were never written to (the dump
only read them), its containers are stopped and not removed, its lines are still in the file and `.env`.

1. `docker compose stop <new services>` (never run both blocks at the same time: same ports, same library, same Navidrome,
   same delegate webhook).
2. `docker compose start <old services>`; if a delegate or a proxy was pointed at the new name in step 6, point it back.

**After step 10** the old images, volumes and packages are gone on purpose, and the rollback is **the old code and the
dump**: check out the last tag before the rename (the repository keeps its history under the new name), build the images
from it (`docker-compose.build.yml`), put the old compose block back, start a PostgreSQL, restore the dump
(`pg_restore`, role and database `musizerr`) and start the services. This takes the database back to the moment of step 4.

What is lost in either case: whatever the new block did in the database after that (downloads, favorites, new accounts).
The files it put in the library stay, and the library scan and reconciliation find them again.

## Alternatives considered

- **Keep the technical identifiers and rename only what users see** (the staged plan of the first draft of ADR-0009, stages
  1 to 3 with "recommended never" for the contract). It leaves `musizerr_sdk` forever in every provider's code, labels
  and logs and the product with two names. The reason to keep them, third-party images, does not exist yet.
- **Rename with aliases for one MAJOR** (old and new package names importable, both label prefixes accepted, both Redis
  prefixes read). A real compatibility window for an install base of one. Rejected by the maintainer's decision.
- **Keep SDK 5 and rename in a minor.** A renamed import is a break for every provider, so it cannot be a minor of the
  contract (ADR-0007 §3.1). The honest number is 6.
- **Overlap SDK 5 and 6 (ADR-0007 §3.2).** Costs an adapter and catalog format 3 for nobody. Rejected.
- **Migrate the PostgreSQL volume in place** (rename the role and the database in the running cluster, keep the volume
  under a pinned old name). Fewer steps, but the migrations then run on the only copy of the data, and the old volume
  names live on in the compose files. Rejected for the dump and restore above.
- **Rename the repositories after the release.** The first images would be linked to the old repository name and the
  workflows would need the old name until then. The order of §5 avoids the double work.

## Consequences

- One name everywhere, greppable: `git grep -i musizerr` returns only the history in the old ADRs (and the migrations'
  one-off statement of row 21).
- Every provider image, catalog entry and compose file built before the release stops working; the owner's server is
  rebuilt following §6, which is also the first complete test of the deployment documentation.
- Everyone signs in again once, and the browsers forget their local preferences once.
- No alias code, deprecation notice or removal question is left behind (the removal question of ADR-0009 disappears).
- The 16 GHCR packages are new (packages cannot be renamed): they start private and with no pull history, and the old
  ones are deleted once the server is verified, with no retention period. After that the way back is the old code and the
  dump, not old images.
- ADR-0007's overlap machinery is not built; the next contract major will need it if there is a user base by then.
- The database guard of ADR-0007 step 3 is not a prerequisite of this release: the migrations run on a restored copy, and
  the dump taken before them is the rollback. The guard stays necessary before any later contract migration on a live database.

## Decisions of the maintainer (2026-10-25)

Every question of the draft is answered; none is open.

1. **The name is Musizeer**, the `NOTICE.md` and attribution text included (§2).
2. **No retention period.** Only the maintainer uses the product. After the maintainer has verified the new block, the
   old volumes, the old block, the old `.env` lines, the old images and the old GHCR packages are deleted, by the
   maintainer or by us with the maintainer's explicit go. The rollback after that is the old code and the dump (§7). The
   dump is kept, at the place §6 names.
3. **The renames are delegated to us**, under the maintainer's standing permission: the four repositories, the new GHCR
   packages with the visibility of the old ones, and the deletion of the old packages once the server is verified (§5).
4. As recommended in the draft, and not contradicted: ADR-0001 to ADR-0007 keep the old name (the index says so); the
   `folder` setting type is bundled into SDK 6.0 (§3); the release compose pins `name: musizeer` while a shared file keeps
   its own project name and renames its volumes with new volume keys (§6); the database moves by dump and restore; the
   release notes print the commands that reinstall the twelve providers (`scripts/provider_sources.py`).

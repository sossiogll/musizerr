# ADR-0010: Rename the product from Musizerr to Musizeer, in one breaking release

**Status:** Proposed
**Date:** 2026-10-24
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
| 4 | GHCR images, 16 packages: `musizerr`, `musizerr-gateway`, `musizerr-frontend`, `musizerr-provider-base` (tag `:5` becomes `:6`) and one `musizerr-provider-<name>` per provider (8 public: acoustid, ftp, lidarr, local, lrclib, musicbrainz, navidrome, youtube; 4 private: deezer, jamendo, spotify, spotizerr). The local tags `musizerr:local`, `musizerr-gateway:local`, `musizerr-frontend:local`, the local provider tag prefix, the build `LABEL org.opencontainers.image.source` | `ghcr.io/sossiogll/musizerr*` | `ghcr.io/sossiogll/musizeer*` | `ghcr.io/sossiogll` 26 / 19, `musizerr-provider*` 74 / 32 in the main repository; 25 + 14 + 2 in the others |
| 5 | Catalogs: the `image` of every entry, the catalog `name` ("Musizerr providers", "Musizerr development catalog"), the digests (they change with the rebuild) | as left | `musizeer-provider-<name>`, "Musizeer providers" | `catalog.json` of the three provider repositories, the dev catalog written by `scripts/dev-catalog.sh` |
| 6 | Python packages and imports: the SDK distribution and package (`musizerr-sdk`, `musizerr_sdk`, directory `sdk/src/musizerr_sdk/`, 23 files), the gateway (`musizerr-gateway`, `musizerr_gateway`, directory `gateway/musizerr_gateway/`, 14 files), every `import`/`from` of them, the provider packages (`musizerr_provider_<name>`: 8 + 7 + 1 directories in the provider repositories) | `musizerr_sdk` | `musizeer_sdk` | `musizerr_sdk` 647 / 249, `musizerr_gateway` 62 / 26, distribution names 27 / 19; in the provider repositories 231 + 167 + 16 lines of `musizerr_sdk` and 71 + 71 + 5 of `musizerr_provider_`. Three old Alembic migrations import or cite it (0025, 0026, 0037) and are edited, their ids and file names do not change |
| 7 | Runner and CLI: `python -m musizerr_sdk.runner` (the command the gateway starts in a sandbox), `python -m musizerr_gateway`, `python -m musizerr_gateway.ctl` | as left | `python -m musizeer_sdk.runner`, `musizeer_gateway`, `musizeer_gateway.ctl` | `musizerr_sdk.runner` 25 lines, `ctl` 17 |
| 8 | Image labels | `org.musizerr.provider.*` (`name`, `roles`, `api-version`, `sdk-version`, `label`...), `org.musizerr.provider-base.api-version` | `org.musizeer.provider.*`, `org.musizeer.provider-base.api-version` | 28 / 15 (`sdk/src/musizerr_sdk/labels.py:22`, `provider-base/Dockerfile:8`, `scripts/build-provider-image.sh`) |
| 9 | Wire-level names: the egress header, the egress proxy and sockets | `X-Musizerr-Egress` (`sdk/src/musizerr_sdk/failures.py:60`) | `X-Musizeer-Egress` | 8 / 4 |
| 10 | Redis keys and channels | `musizerr:progress`, `musizerr:providers`, `musizerr:provider-login:`, `musizerr:provider-slot:`, `musizerr:<scope>-errors:` and `-cooldown:`, `musizerr:worker:heartbeat:`, `musizerr:delegation-reconcile-lock:` | `musizeer:*` | 21 / 14. Transient data; the new stack has its own Redis volume, nothing to carry over |
| 11 | Paths inside the containers | `/run/musizerr` (sockets, `gateway.sock`), `/var/lib/musizerr-gateway` (the layer cache), `.musizerr-work` (the working folder name), `/data/musizerr.db` (the SQLite file) | `/run/musizeer`, `/var/lib/musizeer-gateway`, `.musizeer-work`, `musizeer.db` | 27 / 16, 11 / 10, 22 / 15, 16 / 10. The working folder name is data of ADR-0008 (a setting value), not a fixed name any more |
| 12 | Container and OS users, uid unchanged | OS user `musizerr` (uid 1000) in `Dockerfile` and `gateway/Dockerfile` | `musizeer`, **still uid 1000**, so the ownership of the volumes and of the folders the owner `chown`ed does not change | 7 / 2 |
| 13 | Compose: container names `musizerr-app-1`..., the project name (there is no `name:` in the compose files, so it is the name of the folder: `musizerr`) and with it the network `musizerr_default` and the six named volumes (`data`, `downloads`, `redis_data`, `postgres_data`, `provider_sockets`, `gateway_state`), the dev and build compose files | `musizerr_<volume>` | `name: musizeer` pinned in the compose files, so volumes are `musizeer_<volume>` whatever the folder is called | 17 / 6 |
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
the terms require. Copies already conveyed keep the notice they were conveyed with. The owner confirms the wording of `NOTICE.md` before
the release; this record does not give legal advice.

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
| 3 | **Renaming on GitHub and GHCR (below).** These are actions on the owner's account: the owner does them, or an agent does them only with the owner's explicit confirmation of each one | the owner |
| 4 | The release: merge `dev` into `master` (the workflow tags the MAJOR, pushes `musizeer`, `musizeer-gateway`, `musizeer-frontend` and the base image `:6`, creates the release with its assets); publish the providers (`scripts/publish.sh` of each providers repository) and pin the digests in the catalogs | the owner triggers; the scripts do the rest |
| 5 | **The owner's server (§6)** | the owner |

**Step 3 in detail.**

- Rename the four repositories. GitHub redirects the old web, `git` and API addresses to the new ones, so local checkouts,
  open pull requests (#54 included), issues and stars keep working; `git remote set-url` is still advisable. The redirect
  lasts until someone creates a repository under the old name.
- **The redirect does not cover GHCR.** Renaming a repository does not rename its packages, and `ghcr.io/sossiogll/musizerr`
  does not become `ghcr.io/sossiogll/musizeer`. The new packages are created by the first push of the release (step 4).
  A new package is **private** (nothing sets its visibility): the owner makes the 16 packages public in the GitHub
  package settings, and checks that the repository is linked to each one (the `org.opencontainers.image.source` label
  names it). The old packages stay where they are and stay pullable until the owner deletes them, which is what keeps the
  rollback of §7 possible.
- **The redirect does not cover the catalog fetch either**: the host fetches a catalog without following redirects
  (`app/services/sources.py:106`, `follow_redirects=False`, on purpose). The catalog URL of the old source stops answering.
  The procedure adds the new URL as a new source.
- Do this **before** step 4. The release pushes images whose source label names the renamed repository, and the first push
  of a package is what links it to that repository and gives the workflow its right to push.
- Secrets, variables, branch protection and the required checks of the repositories survive a rename; the owner checks that
  no required check is named after a renamed job (`images.yml:175`, the matrix jobs named after the images, are not
  required checks today).

### 6. The owner's server: step by step

The install is one compose stack with PostgreSQL, the music is on bind mounts of `/mnt/hdd2` and `CREDENTIALS_KEY` is in
`.env`. The compose files have no `name:`, so the project name is the folder's: the commands below assume the folder
`~/musizerr` and the volumes `musizerr_*`; `docker volume ls` shows the real ones. **The credentials
key does not change: sealed credentials stay readable** (the `enc:v1:` rows do not carry the name, and
`MUSIZEER_CREDENTIALS_KEY` takes the same value). New compose names mean **new volumes**.

**Recommended: move the database with `pg_dump` and restore it into the new database and user.** The alternative, copying
the old `postgres_data` volume into the new one, keeps the PostgreSQL role and database called `musizerr` (they are created
once, at the first start of an empty volume; changing `POSTGRES_USER` later does nothing), so the new stack would carry the
old name for ever. A dump is also the backup that makes the rollback possible, and the migrations of this release run on
the copy, never on the old volume.

1. **Before the release is installed, with the old stack running** (note the old version for the rollback):
   `docker compose images`, and copy `.env` and `docker-compose.override.yml` somewhere safe.
2. **Stop the writers and take the dump.**
   ```sh
   cd ~/musizerr
   docker compose stop frontend app worker beat
   docker compose exec -T postgres pg_dump -U musizerr -Fc musizerr > ~/musizerr-$(date +%F).dump
   pg_restore --list ~/musizerr-$(date +%F).dump | head      # it reads: the dump is not empty
   docker compose down                                        # no -v: the old volumes stay
   ```
   Look inside the old `data` volume once (`docker run --rm -v musizerr_data:/d alpine ls -la /d`): with PostgreSQL it holds
   nothing the database does not, so it is not copied.
3. **Fetch the new files into a new folder** (the project name is pinned to `musizeer` by the compose file):
   ```sh
   mkdir ~/musizeer && cd ~/musizeer
   BASE=https://github.com/sossiogll/musizeer/releases/latest/download
   curl -fsSLO $BASE/docker-compose.yaml
   curl -fsSLO $BASE/musizeer-gateway.seccomp.json
   ```
4. **Rewrite the environment with the new names** (ADR-0009 has the whole table). From the old file:
   ```sh
   sed -E 's/^(AUTH_SECRET|CREDENTIALS_KEY|CREDENTIALS_KEY_PREVIOUS|ADMIN_BOOTSTRAP_EMAIL|ADMIN_BOOTSTRAP_PASSWORD|PUBLIC_URL|APP_BASE_URL|FRONTEND_URL|DATA_DIR|PUBLIC_API_PREFIX|ALLOWED_ORIGINS|DB_[A-Z_]+|GATEWAY_[A-Z_]+)=/MUSIZEER_\1=/; s/^APP_PORT=/MUSIZEER_PORT=/; s/^MUSIZERR_/MUSIZEER_/' ~/musizerr/.env \
     | grep -Ev '^(MUSIZEER_GATEWAY_WORKSPACE_DIR|MUSIZEER_GATEWAY_FILESYSTEM_ROOTS|WORKSPACE_DIR|POSTGRES_USER|POSTGRES_DB)=' > .env
   ```
   Read the result: the three required values (`POSTGRES_PASSWORD`, `MUSIZEER_AUTH_SECRET`, `MUSIZEER_CREDENTIALS_KEY`) must
   be there, the last one **unchanged**. `POSTGRES_USER` and `POSTGRES_DB` are dropped on purpose: the new defaults are
   `musizeer`. Carry the override file of the mounts over, with the new layout of ADR-0008 (staging at `/downloads`
   in the application, the workers, beat and the gateway; the library at `/library` in the gateway) and no
   `GATEWAY_FILESYSTEM_ROOTS`.
5. **Create the database and restore it.**
   ```sh
   docker compose up -d postgres
   docker compose exec -T postgres pg_restore -U musizeer -d musizeer --no-owner --no-acl < ~/musizerr-YYYY-MM-DD.dump
   ```
6. **Start everything.** `docker compose pull && docker compose up -d`; `docker compose logs init` shows the migrations
   (the `origin` value, the roots). A variable of the old table still in `.env` is reported by name (ADR-0009 §3).
   `docker compose exec provider-gateway python -m musizeer_gateway.ctl health` must say `"available": true`.
7. **Providers: add the new source and install again.** The installed rows point at images that no longer run. In
   Administration › Sources add the URL of the new catalog, remove the old source (this uninstalls its providers: **their
   settings and their credentials are stored by provider name and stay**), then install each provider from the new one
   (or, from the shell, `docker compose exec app python -m scripts.provider_sources add-source <url>`, `uninstall <name>`
   and `install <source> <name> --yes` for the twelve; removing the old source is a page action). The permissions are the same ones, the
   admin sees and approves them as at any install. The gateway's state volume is new, so it pulls the images again.
   `auto_update_providers` is not relied on (ADR-0007 §5.1 plans it; the code has no such setting yet).
8. **Folders.** Administration › Root folders shows what the conversion made of the old values (ADR-0008): Downloads
   `/downloads`, Library `/library`, the working folder Downloads + `.musizeer-work`, local storage Library + empty path.
   Run the checks; map Lidarr's `/music` to the library when that step exists; run the full-sync verification for Navidrome.
9. **Verify before anything is deleted.** Everyone signs in again (expected, row 18); the library page shows the same
   counts; a provider that needs an account still says "connected"; one download completes and appears in the library; a
   track streams; a delegated download is accepted by Lidarr and its webhook arrives; the full-sync verification passes; the
   update notice at the top of Administration shows the new version.
10. **Clean up only after a few weeks of use:** `docker volume rm` the old `musizerr_*` volumes, remove the old images, delete the old
    GHCR packages and the old `~/musizerr` folder. Keep the dump.

### 7. Rollback

Until step 10 the old stack is intact: its volumes were never written to (the dump only read them), its images are still
published under the old names, its folder and `.env` are where they were.

1. `cd ~/musizeer && docker compose down` (never run both stacks at the same time: same ports, same library, same
   Navidrome, same delegate webhook).
2. `cd ~/musizerr && docker compose up -d`.

What is lost: whatever the new stack did meanwhile in the database (downloads, favorites, new accounts). The files it put
in the library stay, and the library scan and reconciliation find them again. The new stack's volumes can stay for a
second attempt. After step 10 the way back is the dump: restore it into a PostgreSQL of the old stack and run the old
images, which only exist while the old GHCR packages are kept.

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
- The 16 GHCR packages are new: they start private and with no pull history.
- ADR-0007's overlap machinery is not built; the next contract major will need it if there is a user base by then.
- The database guard of ADR-0007 step 3 is not a prerequisite of this release: the migrations run on a restored copy, the
  old volume is the rollback. The guard stays necessary before any later contract migration on a live database.

## Questions for the maintainer

1. **Old records keep the old name.** ADR-0001 to ADR-0007 are history and say Musizerr. *Recommended:* yes; the index says
   so.
2. **Bundle the `folder` setting type into SDK 6.0.** *Recommended:* yes (§3): a second break would repeat this
   procedure.
3. **Pin `name: musizeer` in the compose files** so that volumes and containers do not depend on the folder name.
   *Recommended:* yes. The cost is that two stacks of the same compose file on one host need `-p`.
4. **PostgreSQL by dump and restore, not by copying the volume.** *Recommended:* yes (§6).
5. **Keep the old GHCR packages and the old volumes for how long?** *Recommended:* at least two weeks of normal use of the
   new stack, and the dump for good.
6. **The wording of the licence notice** (`NOTICE.md`, the About page): the owner confirms the text of row 2 before the
   release. *Recommended:* the replacement above, nothing else.
7. **Providers reinstalled by hand or by `scripts/provider_sources.py`.** The release notes can print the twelve commands.
   *Recommended:* print them.

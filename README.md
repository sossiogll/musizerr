# Musizerr

> Based on Musizerr — https://github.com/sossiogll/musizerr

Musizerr is a self-hosted, provider-agnostic music manager. You search catalogs, keep your favorites and
playlists, download what you want into your own library, play it, and keep it in sync with the media server
you already use. Every service it talks to — a catalog, a downloader, the storage, a media server, lyrics, audio
identification — is a **provider**: a separate program you install from a catalog, remove or replace. Musizerr
itself knows none of them by name.

## How it is built

- **The host**: a FastAPI application, Celery workers (downloads, library jobs, maintenance) and a beat,
  PostgreSQL and Redis. It holds your library, users, settings and credentials (encrypted at rest), and decides
  everything: matching, retries, approvals, what is in the library.
- **The provider gateway**: runs each installed provider in its own sandbox (bubblewrap, no network but the
  destinations the provider declares, no environment, no file but its own workspace). The host talks to providers
  only through it.
- **Providers**: OCI images built on the base image of this repository and its SDK (`musizerr-sdk`, the provider
  contract), published on GHCR and listed in catalogs. An admin adds a catalog as a source, sees what each provider
  asks for (its network destinations, folders, limits) and installs it.
- **The web app**: React, made for phones as much as for desktops; administration included.

[`docs/architecture.md`](docs/architecture.md) is the map: the processes, the provider gateway and its sandboxes, and the
main flows. The architecture, its rules and its patterns are described in the guide at the root of the repository and in
[`docs/`](docs/); the decisions in [`docs/adr/`](docs/adr/).

## Run it

You need Docker with Compose, on a kernel with user namespaces (the gateway's sandboxes). The complete guide, with the
files to download, the secrets, AppArmor and seccomp, the public URL, folders and upgrades, is
**[`docs/deployment.md`](docs/deployment.md)**. In short:

1. Put `docker-compose.yaml`, `musizerr-gateway.seccomp.json` and `.env` (from `env.example`) in one folder: all three are
   attached to every [release](https://github.com/sossiogll/musizerr/releases/latest), no clone needed (a clone has
   them too: `docker-compose.yaml`, `gateway/seccomp/`, `.env.example`). Then set, in `.env`:

   ```sh
   POSTGRES_PASSWORD=...        # openssl rand -hex 24
   AUTH_SECRET=...              # openssl rand -hex 32
   CREDENTIALS_KEY=...          # openssl rand -base64 32 | tr '+/' '-_'
   ADMIN_BOOTSTRAP_EMAIL=you@example.com
   ADMIN_BOOTSTRAP_PASSWORD=...
   PUBLIC_URL=http://your-server:8080   # the address people type; OAuth callbacks and sign-in redirects use it
   APP_PORT=8080
   # MUSIZERR_VERSION=1.2.0     # optional: pin a release (the tags of the GitHub releases); default latest
   ```

   Keep `CREDENTIALS_KEY` with your database backups: without it the stored credentials cannot be read and every
   account has to be connected again ([`docs/credentials.md`](docs/credentials.md)).

2. Start it: Compose pulls the published images (`ghcr.io/sossiogll/musizerr`, `musizerr-gateway`, `musizerr-frontend`,
   for linux/amd64 and linux/arm64), nothing is built.

   ```sh
   docker compose pull && docker compose up -d
   ```

   The web app is on `APP_PORT` (default 80). The first start creates the admin from the bootstrap variables (only
   while no account exists). A new install has authentication off: you are the one owner and there is nothing to
   sign in to. Turn it on in Administration › System; the admin account is what lets you sign in afterwards, and if
   you set no bootstrap variables the settings page asks you to create one at that moment (what you have in
   Musizerr by then becomes that account's, [`docs/permissions.md`](docs/permissions.md)). To update, pull
   again (or raise `MUSIZERR_VERSION`) and `up -d`: the init container migrates the database before the API and the workers
   start. The gateway container is never `privileged`: it needs the seccomp profile, `apparmor=unconfined` and
   `systempaths=unconfined`, which the compose file already sets ([why](docs/deployment.md#apparmor-and-seccomp)).

   To build the images from the checkout instead (a fork, unreleased changes):

   ```sh
   docker compose -f docker-compose.yaml -f docker-compose.build.yml up -d --build
   ```

3. In Administration › Sources, add a catalog, then install the providers you want from it:

   ```
   https://raw.githubusercontent.com/sossiogll/musizerr-providers/master/catalog.json
   ```

   A private catalog is added with a token that can read it. Then configure the providers in Administration ›
   Providers and connect your accounts in Settings.

More: [the architecture overview](docs/architecture.md), [the deployment guide](docs/deployment.md), [PostgreSQL and moving from SQLite](docs/postgresql.md), [workers](docs/workers.md),
[users and permissions](docs/permissions.md), [signing in through a provider](docs/provider-login.md),
[playlists](docs/playlists.md), [sources and catalogs](docs/catalog.md), [the gateway](docs/gateway.md),
[migrations](docs/migrations.md), [the push callback of delegated downloads](docs/delegation-webhook.md).

## Write a provider

Start from the template repository `sossiogll/musizerr-provider-template` and read
[`docs/provider-authors.md`](docs/provider-authors.md). A provider is a package with its tests and its image;
it imports the SDK and nothing else of Musizerr, and you publish it in a catalog of your own.

## Develop

Everything runs in containers: nothing but Docker on the host.

```sh
docker compose -f docker-compose.dev.yml up -d
```

The development stack mounts the sources (the API and the workers reload on change, the frontend runs Vite), adds
a local OCI registry and a catalog server for providers under development (`scripts/dev-catalog.sh
<provider-dir>...`, [`docs/catalog.md`](docs/catalog.md#development)).

Tests:

```sh
docker exec musizerr-app-1 python -m pytest                                   # the backend, on SQLite
docker exec musizerr-app-1 sh -c 'cd /sdk && python -m pytest'                # the SDK
docker exec musizerr-frontend-1 npx tsc -p tsconfig.app.json --noEmit         # the frontend's types
docker exec musizerr-frontend-1 pnpm test                                     # the frontend
```

The backend suite also runs on PostgreSQL (`TEST_DATABASE_URL`), and both must pass; the gateway has its own
suite and integration tests with real sandboxes (`scripts/test-*-integration.sh`).

Changes go through `dev`: a branch, a pull request into `dev` with exactly one release label (`breaking`, `feature`, `fix`
or `chore`) and, when operators must do something, its "Upgrade notes"; then a pull request from `dev` into `master`.
Both branches are protected: every check of `ci.yml` must pass. Releases are automatic: a merge into `master` publishes the
images as `:latest` and, when the pull requests it brings make it one (`breaking` → major, `feature` → minor, `fix` →
patch; `chore` alone → none), tags `vX.Y.Z`, publishes the images as `:X.Y.Z` and creates the GitHub release.

## License

Musizerr is free software, released under the GNU Affero General Public License, version 3 or later
([`LICENSE`](LICENSE)), with additional terms on attribution ([`NOTICE.md`](NOTICE.md)): every copy and every
work based on it keeps the notice at the top of this file, here and on a page of its interface reachable from the
main navigation (Musizerr's own is About), and a modified version offers its source to its users.

The provider SDK in [`sdk/`](sdk/) is released under the GNU Lesser General Public License, version 3 or later:
a provider built on it may use the license its author chooses. Providers are separate programs, each under its
own license.

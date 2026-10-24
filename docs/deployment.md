# Deploying Musizeer

How to run Musizeer on a server with the published images: one folder, one compose file, one `.env`. Nothing is
built and no clone of the repository is needed. (Developing is another page of the README; this one is about running it.)

```
 browser ──▶ frontend (nginx, :80) ──/api/──▶ app (FastAPI) ──▶ postgres, redis
                                                │  ▲
                                  sockets       ▼  │ control socket
                    workers, beat ───▶ provider-gateway ──▶ one sandbox per provider
```

## What you need

- A Linux host (amd64 or arm64) with **Docker Engine 24 or later and the Compose plugin** (`docker compose version`).
  Docker Desktop works for trying it out.
- A kernel with **unprivileged user namespaces**: the provider gateway builds a sandbox for every provider with them
  ([AppArmor and seccomp](#apparmor-and-seccomp) below says what that takes on Debian and Ubuntu). Without sandboxes Musizeer
  runs but no provider does.
- 1 GB of memory for the stack, more for a big library; a folder of the host for the music if you keep it on the host.

## 1. The files

Download the three files of the latest release into an empty folder (a release's `docker-compose.yaml` already
points at the profile next to it):

```sh
mkdir musizeer && cd musizeer
BASE=https://github.com/sossiogll/musizeer/releases/latest/download
curl -fsSLO $BASE/docker-compose.yaml
curl -fsSLO $BASE/musizeer-gateway.seccomp.json
curl -fsSL  $BASE/env.example -o .env
```

(`musizeer-gateway.seccomp.json` is the gateway's seccomp profile. A Docker `security_opt` names a file **on the host**, so it
cannot come from inside an image; that is why it is a file next to the compose file. To pin a version instead of
`latest`, use `.../releases/download/v1.4.0/...`.) A clone of the repository works too: `docker-compose.yaml` and
`gateway/seccomp/musizeer-gateway.json` are in it, and the paths already match.

## 2. The `.env` file

`.env` sits next to `docker-compose.yaml` and is **never committed or shared**: it holds the secrets. Three variables are
required (compose refuses to start without them). Generate each once:

```sh
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"              >> .env
echo "AUTH_SECRET=$(openssl rand -hex 32)"                    >> .env
echo "CREDENTIALS_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .env
```

| Variable | What it is | If you lose or change it |
|---|---|---|
| `POSTGRES_PASSWORD` | The database password (letters and digits: it goes into a URL). Used the first time the database is created. | Changing it later does not change the database's: change both, or keep it. |
| `AUTH_SECRET` | Signs the session tokens. At least 32 characters; the app refuses a short or placeholder one. | Everyone is signed out and signs in again. |
| `CREDENTIALS_KEY` | Encrypts every stored credential (OAuth tokens, logins, API keys, secret settings): a Fernet key, 44 characters. | **The stored credentials cannot be read any more and every account must be connected again.** Back it up with the database ([docs/credentials.md](credentials.md), which also has the rotation). |

Then, in the same file, the things you want to change (all are listed, commented, in `env.example`):

```sh
ADMIN_BOOTSTRAP_EMAIL=you@example.org        # the first administrator, created at the first start
ADMIN_BOOTSTRAP_PASSWORD=choose-a-long-one
PUBLIC_URL=http://192.168.1.20:8080          # the address people type, see "The public URL"
APP_PORT=8080                                # the port the web app is published on (default 80)
```

## 3. Start

```sh
docker compose pull
docker compose up -d
docker compose ps
```

The order is enforced, not hoped for: `init` applies the database migrations and exits; the API, the workers and
beat start after it succeeded and when PostgreSQL, Redis and the gateway answer; the web app starts when the API is
healthy (`/health` answers 200 with `"db": "ok"`). `docker compose ps` shows `app` as `healthy` after a half minute and `init` as
`Exited (0)`. Open `PUBLIC_URL` and sign in with the bootstrap administrator.

Check that the gateway can build sandboxes (it must say `"available": true`):

```sh
docker compose exec provider-gateway python -m musizeer_gateway.ctl health
```

If it says `sandbox unavailable: ...`, the message ends with what to change, see [AppArmor and seccomp](#apparmor-and-seccomp)
and the [troubleshooting table](gateway.md#troubleshooting).

Then, as the administrator: **Administration › Sources**, add a catalog and install providers from it (a storage
provider, a download provider, ...), and configure them. The library the storage already holds is imported by the routine
*Import the existing library* a few minutes after a storage provider answers (it waits until then; the *System routines* page shows it, "Run now" does it at once).

## AppArmor and seccomp

The gateway container is **not privileged** and drops every capability. For bubblewrap to build sandboxes inside it
needs three options, already in `docker-compose.yaml`:

```yaml
provider-gateway:
  cap_drop: [ALL]
  security_opt:
    - seccomp=${GATEWAY_SECCOMP_PROFILE:-./gateway/seccomp/musizeer-gateway.json}   # a release's file: ./musizeer-gateway.seccomp.json
    - apparmor=unconfined                                                           # Debian, Ubuntu: Docker's docker-default profile denies the namespace calls
    - systempaths=unconfined                                                        # a sandbox mounts its own /proc
```

- **seccomp**: Docker's default profile plus the five syscalls bubblewrap needs. The path is read from the
  host when the container is created (relative to the compose file); if the file is not found, `docker compose up` fails
  with `opening seccomp profile ... failed`, and a file that exists but is not the gateway's profile gives `No permissions
  to create a new namespace` at run time. `GATEWAY_SECCOMP_PROFILE` in `.env` changes where it is.
- **AppArmor**: on Debian and Ubuntu Docker confines containers with `docker-default`, and the gateway fails with
  `bwrap: Creating new namespace failed: Permission denied` without `apparmor=unconfined`. On a host without AppArmor it
  does nothing. Only this container is affected.
- **Never `privileged: true`.** It would "fix" the error above because it also switches AppArmor and seccomp off and gives the
  container the host's devices and capabilities: a sandbox escape would then be a host takeover. If the options above are not
  enough, [the troubleshooting table](gateway.md#troubleshooting) says which host setting is.

`EACCES` ("Permission denied") means AppArmor or another security module said no; `EPERM` ("No permissions", "Operation
not permitted") means seccomp or a sysctl (`kernel.unprivileged_userns_clone`, `user.max_user_namespaces`).

## Processes and threads of the providers

The gateway container carries one more option, `pids_limit`, also in `docker-compose.yaml`:

```yaml
provider-gateway:
  user: "1000:1000"
  pids_limit: ${GATEWAY_PIDS_LIMIT:-4096}
```

It caps the processes and threads of the whole gateway container (the gateway and every provider's sandbox): the kernel counts
them for this container alone, so however busy the rest of the host is, a provider can neither be starved of threads by it nor
exhaust the host's process table. On top of it the gateway holds each sandbox to the `limits.processes` of its provider
(default 512), and reports both with their numbers in `ctl list` and `ctl health` and in the log
(`process/thread limit reached (RLIMIT_NPROC / pids)`). [gateway.md](gateway.md#processes-and-threads) has the details and why
the gateway does not use `RLIMIT_NPROC`.

**Upgrading from 1.2.0 or earlier.** Those versions started every sandbox with `--nproc=512`. The kernel counts that over
**every process of the same uid on the host**, other containers included, and `user: "1000:1000"` is usually the main account of
the machine: with a few hundred threads of a media server and other services already under uid 1000, a provider that held two
threads failed with `can't start new thread` (the application saw `Connection reset by peer` from it) at the first burst of
requests, and bubblewrap itself with `Resource temporarily unavailable`. Nothing you have has to change to keep running; to get
the fix:

1. Pull the new images and start them: `docker compose pull && docker compose up -d`. The sandboxes no longer get an
   `RLIMIT_NPROC`, so the failure is gone with the new gateway image, whatever else runs as the uid.
2. If you use the release's `docker-compose.yaml` as it is, download the new one (the compose file changed: it adds
   `pids_limit`). If you keep your own compose file or an override, add `pids_limit: 4096` to the `provider-gateway` service.
   It is recommended, not required: without it the gateway logs a warning at start (`the container has no pids limit`) and each
   sandbox is still held to its `limits.processes` by the gateway's own count, but nothing stops a burst between two counts.
3. **Keep `user: "1000:1000"`.** You do not have to change the user, and nothing has to be `chown`ed: the gateway's uid is the
   owner of the files its providers write into your library (see [Folders](#folders)), and it is the one your media server
   probably reads them as. If you had changed `user:` to get around the error, you can put it back; a different uid works as it
   did, with the ownership consequences of [gateway.md](gateway.md#sockets-volumes-and-permissions).
4. Remove any `ulimits: nproc:` on `provider-gateway`, any `GATEWAY_MAX_PROCESSES` you raised to get around the error, and a
   `default-ulimits` `nproc` of the Docker daemon, if you set one: a limit of that kind counts over the whole uid and brings the
   failure back. The pids limit does the job.

## The public URL

Set `PUBLIC_URL` to the address **as the browser sees it**, without a trailing slash: `http://192.168.1.20:8080`,
`https://music.example.org`. From it the compose file derives:

- `APP_BASE_URL` = `PUBLIC_URL/api`: the base of the OAuth2 callback. The redirect URI to register at the service is
  `PUBLIC_URL/api/auth/<provider>/callback`;
- `FRONTEND_URL` = `PUBLIC_URL`: where the browser is sent when a sign-in ends.

Without it both point at `http://localhost`, which works only in a browser on the server itself and only on port 80. Set
`APP_BASE_URL` / `FRONTEND_URL` themselves only when they must differ. Behind a reverse proxy that terminates TLS, forward
`/` to the web app (port `APP_PORT`), keep the `Host` header, send `X-Forwarded-Proto` and `X-Forwarded-Host`, and set
`PUBLIC_URL` to the `https://` address; the web app's nginx passes those headers on to the API.

### `public_base_url`, the setting, is another address

Download providers that hand the work to another program (a delegate, such as a downloader with its own queue) ask it to
call Musizeer back when it is done. The address it is given is the **setting `public_base_url`**
(the system settings), and it must be reachable **from the delegate**, not from your browser:
the delegate may be another container, another host, a VPN. Typical mistakes:

- a name that only the browser's machine resolves (a Tailscale MagicDNS name, `.local`, a name in your router's DNS) while the
  delegate runs in a container whose DNS does not know it: use an address the delegate can reach, an IP or a name *it* resolves
  (the web app's address, `http://192.168.1.20:8080`; on the same compose network `http://frontend`);
- `localhost` (inside the delegate that is the delegate);
- the API's port 8000, which is not published: the callback goes to the web app, which forwards `/api/` to the API.

## Folders

```
 downloads (named volume)  ─ mounted at /downloads in app, the three workers, beat and the gateway
    └── .musizeer-work/    ─ the workspace: WORKSPACE_DIR
 gateway_state             ─ the gateway's layer cache (not shared)
```

- **The workspace.** The host and the gateway exchange files in `WORKSPACE_DIR` (default `/downloads/.musizeer-work`): the host
  creates a folder per call, the gateway shows each provider its own part as `/workspace`. **One variable sets both
  sides**: compose gives it to the gateway as `GATEWAY_WORKSPACE_DIR` and to the API and workers, where it is the default of the
  setting `workspace_dir`. Put it inside a volume that all of them mount at the same path. If you change the setting
  `workspace_dir` in the interface without changing `WORKSPACE_DIR`, the two disagree and providers cannot exchange files; the
  API logs `The workspace folder of the host ... is not the gateway's` when that happens.
- **The folders a provider may use.** A provider that works with a folder of the machine (a local storage provider's
  library, say) is given the folder its setting names, but only if it lies inside one of the gateway's **roots**:
  `GATEWAY_FILESYSTEM_ROOTS`, absolute paths separated by `:`, default `/downloads`. A root has to be mounted in the
  gateway container to hold anything.

  *Example: a staging folder (the `downloads` volume) and a music library of the host at `/srv/music`, seen as `/library`.*
  Create `docker-compose.override.yml` next to the compose file (Compose reads it by itself):

  ```yaml
  services:
    provider-gateway:
      volumes:
        - /srv/music:/library
  ```

  and in `.env`:

  ```sh
  GATEWAY_FILESYSTEM_ROOTS=/downloads:/library
  ```

  Then `docker compose up -d provider-gateway`, and give the storage provider `/library` (or a folder inside it) as its folder in
  Administration. The gateway runs as uid 1000 and the sandboxes' files belong to it: `chown -R 1000:1000 /srv/music` (or
  make it writable for that uid) if the provider has to write. A folder outside every root, a link that leads out of
  one, `/`, and the gateway's own folders are refused, with the reason in the provider's page and in
  `docker compose exec provider-gateway python -m musizeer_gateway.ctl list` (`folders.refused`).
- **The music itself** is wherever your storage provider puts it: in `/library` above, or in the `downloads` volume. A named
  volume lives in `/var/lib/docker/volumes`; replace `downloads` with a bind mount in the override file for all services that
  mount it if you want a folder of your own for it.

## Upgrading

`MUSIZERR_VERSION` in `.env` chooses the images (`musizeer`, `musizeer-gateway`, `musizeer-frontend`):

- unset: `latest`, the last merge into the master branch. `docker compose pull && docker compose up -d` updates;
- `MUSIZERR_VERSION=1.4.0`: that release and nothing else, until you change it (the tags of the GitHub releases, without the `v`).
  **Recommended on a server you do not watch.**

```sh
# edit MUSIZERR_VERSION in .env, then:
docker compose pull
docker compose up -d
```

`init` migrates the database before the API and the workers start, so an upgrade is a restart. Read the release's
*Upgrade notes* first (the releases page, and the notice at the top of Administration once the daily update check has seen the release): they say what to
change in your compose file or `.env`. When a release changes `docker-compose.yaml` or the seccomp profile, download the two files of
that release again (the profile and the compose file always travel together).

### The backup `init` takes, and going back

Before it applies a migration `init` **backs the database up** (only when something is pending: a restart with nothing
new takes none) into `/data/backups`, the `data` volume, and does not migrate if the backup fails:

```sh
docker compose run --rm --no-deps init ls -l /data/backups
# premigration-20261024T101500Z-from-0042_open_mode_accounts-to-0043_schema_info.dump
```

The name says when it was taken and which revisions the migration went from and to. The last 5 are kept
(`MIGRATION_BACKUPS_KEEP`); `MIGRATION_BACKUP=off` in `.env` skips the backup if you back the database up yourself. PostgreSQL is
dumped with `pg_dump` (the image carries the client of the `postgres:17` the compose file runs), SQLite with its online backup.
The files are readable by the owner only and hold the whole database, so keep a copy off the machine, together with
`CREDENTIALS_KEY` ([credentials.md](credentials.md)), which the backup does not contain. Keep `.env` too.

- **Going back one minor or patch** (for example `1.5.2` to `1.5.0`): set the previous `MUSIZERR_VERSION` and
  `docker compose up -d`. Releases within a major only *add* to the schema, so the previous one runs on it and nothing is
  restored. The previous release then says in its log that the database is ahead of it and starts. (The first release that has
  this guard is the exception: a release before it does not know the table it adds, see
  [migrations.md](migrations.md#the-start-check).)
- **Going back across a MAJOR, or after a migration that went wrong**: the release notes list the *contract migrations* of a
  MAJOR, which the previous release cannot run on. Restore the backup `init` took and start the previous version:

  ```sh
  docker compose stop app worker worker-jobs worker-maintenance beat
  docker compose run --rm init python -m scripts.restore_backup /data/backups/<file> --apply
  # set the previous MUSIZERR_VERSION in .env, then
  docker compose up -d
  ```

  `scripts.restore_backup` without `--apply` only checks the file. With it, the database as it is now is copied to
  `/data/backups/pre-restore-<time>` first, and what was written after the backup is lost. The commands for PostgreSQL and
  SQLite by hand are in [migrations.md](migrations.md#restoring-a-backup).
- An app that finds a database it cannot use (behind it, or migrated by a later MAJOR) **refuses to start** and logs why and
  what to do (`docker compose logs app`); the same for the workers.

## Do not override `dns:` on Musizeer's services

Docker gives every container a resolver that knows the other services by name and forwards the rest to the **host's** resolvers.
That is how `app` finds `postgres`, and how the gateway's egress proxy finds the server a provider is set up to use:
your media server by its Tailscale name, a NAS by `.local`, an address that only your router's DNS knows.

A `dns: [1.1.1.1]` (or any public resolver) on a Musizeer service replaces the host's resolvers. Service names keep working and
**every private name stops resolving**: a provider cannot reach a server named `nas.tailnet.ts.net`, an OAuth callback to a
split-horizon name fails, and the symptom is a provider "unreachable" while the same URL works from the host. Leave `dns:` out; if
the host's own DNS is the problem, fix it on the host.

The same goes for the other side of a call: a program that calls Musizeer back (see `public_base_url` above) must resolve the name
it is given with *its* resolver.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `docker compose up` stops with `set POSTGRES_PASSWORD in .env` (or `AUTH_SECRET`, `CREDENTIALS_KEY`) | The variable is missing from `.env` ([2](#2-the-env-file)). |
| `docker compose up` fails with `opening seccomp profile (...) failed ... no such file` | `musizeer-gateway.seccomp.json` is not next to the compose file (the path is relative to it), or `GATEWAY_SECCOMP_PROFILE` is wrong. |
| `ctl health` says `sandbox unavailable: ...` | The hint at the end of the message; [gateway.md, Troubleshooting](gateway.md#troubleshooting). |
| A provider fails with `can't start new thread` or `Connection reset by peer`, or `ctl health` mentions `Resource temporarily unavailable` | The numbers in the gateway's log and `ctl list` (`container`, `tasks`): [Processes and threads](#processes-and-threads-of-the-providers), [gateway.md](gateway.md#troubleshooting). |
| The web app answers 502 | The API is not up: `docker compose logs app`. nginx finds it again by itself (a few seconds) when it comes back. |
| `init` is `Exited (1)` and nothing else starts | `docker compose logs init`: usually `CREDENTIALS_KEY` invalid, the database not reachable, or "The database was NOT migrated because its backup failed" (the message says why: disk full, `/data/backups` not writable; fix it and `docker compose up -d` again). |
| `app` or a worker exits at start with "The database is behind" or "cannot run on this database" | The schema is not one this version can use: [The backup `init` takes, and going back](#the-backup-init-takes-and-going-back). |
| The browser is sent to `localhost` after signing in at a service | `PUBLIC_URL` is not set ([The public URL](#the-public-url)). |
| A provider cannot read or write files | `WORKSPACE_DIR` / `GATEWAY_WORKSPACE_DIR` differ, or the folder is outside a root ([Folders](#folders)). |
| The existing library is not imported | The *System routines* page: *Import the existing library* says `waiting` until a storage provider is installed, configured and answering. |

`BACKEND_URL` (the web app's `frontend` service, default `http://app:8000`) is where nginx reaches the API. Set it only if you
renamed the `app` service or run the API elsewhere: `scheme://host:port`, no path. nginx resolves the name again every few seconds, so a
recreated `app` container needs no restart of the web app.

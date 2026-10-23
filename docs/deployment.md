# Deploying Musizerr

How to run Musizerr on a server with the published images: one folder, one compose file, one `.env`. Nothing is
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
  ([AppArmor and seccomp](#apparmor-and-seccomp) below says what that takes on Debian and Ubuntu). Without sandboxes Musizerr
  runs but no provider does.
- 1 GB of memory for the stack, more for a big library; a folder of the host for the music if you keep it on the host.

## 1. The files

Download the three files of the latest release into an empty folder (a release's `docker-compose.yaml` already
points at the profile next to it):

```sh
mkdir musizerr && cd musizerr
BASE=https://github.com/sossiogll/musizerr/releases/latest/download
curl -fsSLO $BASE/docker-compose.yaml
curl -fsSLO $BASE/musizerr-gateway.seccomp.json
curl -fsSL  $BASE/env.example -o .env
```

(`musizerr-gateway.seccomp.json` is the gateway's seccomp profile. A Docker `security_opt` names a file **on the host**, so it
cannot come from inside an image; that is why it is a file next to the compose file. To pin a version instead of
`latest`, use `.../releases/download/v1.4.0/...`.) A clone of the repository works too: `docker-compose.yaml` and
`gateway/seccomp/musizerr-gateway.json` are in it, and the paths already match.

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
docker compose exec provider-gateway python -m musizerr_gateway.ctl health
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
    - seccomp=${GATEWAY_SECCOMP_PROFILE:-./gateway/seccomp/musizerr-gateway.json}   # a release's file: ./musizerr-gateway.seccomp.json
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
call Musizerr back when it is done. The address it is given is the **setting `public_base_url`**
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
    └── .musizerr-work/    ─ the workspace: WORKSPACE_DIR
 gateway_state             ─ the gateway's layer cache (not shared)
```

- **The workspace.** The host and the gateway exchange files in `WORKSPACE_DIR` (default `/downloads/.musizerr-work`): the host
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
  `docker compose exec provider-gateway python -m musizerr_gateway.ctl list` (`folders.refused`).
- **The music itself** is wherever your storage provider puts it: in `/library` above, or in the `downloads` volume. A named
  volume lives in `/var/lib/docker/volumes`; replace `downloads` with a bind mount in the override file for all services that
  mount it if you want a folder of your own for it.

## Upgrading

`MUSIZERR_VERSION` in `.env` chooses the images (`musizerr`, `musizerr-gateway`, `musizerr-frontend`):

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
that release again (the profile and the compose file always travel together). Back up the database and `.env` before a major upgrade:

```sh
docker compose exec postgres pg_dump -U musizerr musizerr > musizerr-$(date +%F).sql
```

## Do not override `dns:` on Musizerr's services

Docker gives every container a resolver that knows the other services by name and forwards the rest to the **host's** resolvers.
That is how `app` finds `postgres`, and how the gateway's egress proxy finds the server a provider is set up to use:
your media server by its Tailscale name, a NAS by `.local`, an address that only your router's DNS knows.

A `dns: [1.1.1.1]` (or any public resolver) on a Musizerr service replaces the host's resolvers. Service names keep working and
**every private name stops resolving**: a provider cannot reach a server named `nas.tailnet.ts.net`, an OAuth callback to a
split-horizon name fails, and the symptom is a provider "unreachable" while the same URL works from the host. Leave `dns:` out; if
the host's own DNS is the problem, fix it on the host.

The same goes for the other side of a call: a program that calls Musizerr back (see `public_base_url` above) must resolve the name
it is given with *its* resolver.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `docker compose up` stops with `set POSTGRES_PASSWORD in .env` (or `AUTH_SECRET`, `CREDENTIALS_KEY`) | The variable is missing from `.env` ([2](#2-the-env-file)). |
| `docker compose up` fails with `opening seccomp profile (...) failed ... no such file` | `musizerr-gateway.seccomp.json` is not next to the compose file (the path is relative to it), or `GATEWAY_SECCOMP_PROFILE` is wrong. |
| `ctl health` says `sandbox unavailable: ...` | The hint at the end of the message; [gateway.md, Troubleshooting](gateway.md#troubleshooting). |
| The web app answers 502 | The API is not up: `docker compose logs app`. nginx finds it again by itself (a few seconds) when it comes back. |
| `init` is `Exited (1)` and nothing else starts | `docker compose logs init`: usually `CREDENTIALS_KEY` invalid, or the database not reachable. |
| The browser is sent to `localhost` after signing in at a service | `PUBLIC_URL` is not set ([The public URL](#the-public-url)). |
| A provider cannot read or write files | `WORKSPACE_DIR` / `GATEWAY_WORKSPACE_DIR` differ, or the folder is outside a root ([Folders](#folders)). |
| The existing library is not imported | The *System routines* page: *Import the existing library* says `waiting` until a storage provider is installed, configured and answering. |

`BACKEND_URL` (the web app's `frontend` service, default `http://app:8000`) is where nginx reaches the API. Set it only if you
renamed the `app` service or run the API elsewhere: `scheme://host:port`, no path. nginx resolves the name again every few seconds, so a
recreated `app` container needs no restart of the web app.

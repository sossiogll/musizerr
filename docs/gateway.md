# The provider gateway

`provider-gateway` is the service that runs providers that are not part of the application
(ADR-0006, phase 3). It pulls a provider's OCI image **by digest**, checks it, unpacks it, and runs it in
a [bubblewrap](https://github.com/containers/bubblewrap) sandbox with no network of its own. The
application and the workers talk to the provider over a Unix socket; they never install or start anything.

Every provider lives this way: none is part of the application, and the host registers only what the gateway runs
and declares. The first-party ones are in their own repositories (the public ones in `musizerr-providers`).

```
 app / workers ──(call protocol, Unix socket)──▶ sandbox: provider (runner)
      │                                               │ 127.0.0.1 relay
      │ control socket                                ▼
      └──────────────▶ gateway ◀──(Unix socket)── egress proxy ──▶ the declared hosts only
```

## What it does

1. **Installed providers.** The database of the application is the only source of truth for what is
   installed ([catalog.md](catalog.md)); the gateway keeps no list of its own and **runs nothing until the
   host tells it**: it pushes the desired set over the control socket (`{"op": "install", ...}`) when the API
   starts, after every install, update or removal, and whenever the gateway restarted (the API's watcher
   compares the `desired` identifier of `list` with the database's every few seconds). A push **replaces**
   the previous set. One entry:

   ```json
   {"name": "lrclib",
    "image": "ghcr.io/sossiogll/musizerr-provider-lrclib",
    "digest": "sha256:<64 hex>",
    "roles": ["lyrics"],
    "permissions": {"egress": ["lrclib.net:443"], "egress_from_settings": [],
                    "filesystem_from_settings": [], "limits": {}},
    "insecure": false,
    "credential": "<token of the source, optional>"}
   ```

   The digest is mandatory: an image is never pulled by tag. `roles` and `permissions` are what the admin
   **approved**: the image is refused unless its labels declare exactly those. `insecure` allows plain HTTP to
   that registry (a local registry of images under test) and nothing else, and an entry that says so cannot
   carry a `credential`. An entry that cannot be used is refused with a reason (`rejected`), the others are
   applied. `generation` is an identifier of the set the host chooses (a digest of the entries without their
   credentials); `list` gives it back as `desired`. A provider whose image was refused is tried again at the
   next push (its token or the registry may have been fixed).
2. **Pull and verify.** The OCI distribution API with a bearer token (the flow GHCR and Docker Hub use),
   plain `http.client`. Anonymous unless the entry brings a **credential**: then the token service is asked
   with HTTP Basic (`user:token`; a bare token is sent with a placeholder user name), or the registry gets
   Basic directly if that is what it challenges with. The credential goes to the registry's host and its
   token realm (the same host, or under the registry's parent domain, such as `auth.docker.io` for
   `registry-1.docker.io`) and nowhere else, never over plain HTTP (an `insecure` entry carries none),
   and not to the CDN a blob redirects to. It is held **in memory** until the image is unpacked and then
   dropped: nothing is written to the state directory, and no message repeats it. The manifest must hash to
   the pinned digest; an index is resolved to the
   `linux/<arch>` manifest of the host, which is verified in turn; the config and every layer must hash to
   the digest and size their descriptor names, and the uncompressed layer to the config's `diff_id`. The
   `Authorization` header is dropped when a redirect leaves the registry's host.
3. **Labels.** The image describes itself with `org.musizerr.provider.*` labels (name, roles, contract
   version, permissions), generated from the provider by `python -m musizerr_sdk.runner --print-labels`.
   They are read from the config **before any layer is downloaded** and the image is refused, with the
   reason, if the name differs from the entry's, the roles or the permissions differ from the ones the entry says the
   admin approved (as sets: the order is nothing; an image cannot gain a permission nobody approved), the
   provider API version is not the SDK's, or the labels are missing or malformed. The host checks again,
   at run time, that what the provider says of itself is what its image declared.
4. **Unpack.** Layers (OCI and Docker media types, gzip or plain tar) are unpacked by hand, never with
   `extractall`: absolute paths, `..`, NUL bytes, device nodes and FIFOs, hard links to anything but a
   regular file of the same layer, relative symlinks that climb above the root, and anything written
   through a link are refused; setuid/setgid/sticky bits are dropped; size and member count are bounded.
   Each layer is unpacked once into `layers/<sha256>/`; an image's root filesystem
   (`images/<digest>/rootfs`) is made of hard links to those files, with whiteouts and opaque directories
   applied, so images that share base layers store them once. Nothing is downloaded again for an image that is
   already unpacked.
5. **Run.** One long-lived process per provider, restarted with a doubling pause (1 s to 60 s, started over
   after five stable minutes) when it dies. See [the sandbox](#the-sandbox).
6. **Report.** The control socket says what runs, what does not and why.

## The sandbox

```
prlimit --as=… --nofile=… --nproc=… --core=0 --
bwrap --unshare-all --unshare-user --disable-userns --die-with-parent --new-session --clearenv
      --uid 10001 --gid 10001 --cap-drop ALL
      --ro-bind <image rootfs> /  --proc /proc  --dev /dev  --size … --tmpfs /tmp
      --bind  <run>/providers/<name>   /run/provider      # its socket
      --ro-bind <state>/egress/<name>  /run/egress         # the egress proxy's socket, read-only
      --bind  <workspace_dir>/<name>   /workspace          # its subtree of the workspace directory
      [--bind|--ro-bind <resolved folder> <the same path> [--tmpfs <workspace directory inside it>]]  # granted by its settings
      --seccomp <fd> --chdir / --setenv … -- <entry point of the image> --socket … --egress-socket …
```

- The root is the image, read-only. `/tmp` is a private tmpfs. `/proc` and `/dev` are fresh.
- Every namespace is unshared, the network one included, and the user namespace is mandatory (never
  "try"). No capability, `no_new_privs`, a non-root uid, a new session, and the process dies with the gateway.
- The environment is empty except `PATH`, `HOME=/tmp`, `LANG`/`TZ` and certificate variables from the image,
  and the proxy variables. Nothing of the gateway's environment.
- Limits (`prlimit`): address space, open files and processes from the manifest, capped at the gateway's
  ceilings (`GATEWAY_MAX_MEMORY_MB`, `GATEWAY_MAX_OPEN_FILES`, `GATEWAY_MAX_PROCESSES`); defaults when the
  manifest asks for nothing. `RLIMIT_NPROC` counts processes of the whole uid, which all sandboxes and the
  gateway share: the process ceiling is a ceiling for the lot, not a quota for each.
- A provider that declares `filesystem_from_settings` gets the folders its settings name, when the gateway
  grants them ([below](#folders-named-by-settings)); until then it runs without them.
- **Inner seccomp filter** (`sandbox_filter.py`): the gateway container needs `unshare`, `clone` with
  namespace flags, `mount`, `umount2` and `pivot_root` to build sandboxes (below), and a seccomp filter is
  inherited, so without more the provider would hold them too: a nested user namespace is one `unshare` away.
  Bubblewrap therefore loads a second filter just before it executes the provider that answers `EPERM` to
  those, to `setns`, `chroot` and the new mount API, to `clone` with a namespace flag, `ENOSYS` to `clone3`
  (so libc falls back to `clone`, whose flags can be read), and denies the x32 range and every architecture
  but x86-64 and AArch64. It is written by hand in classic BPF (no libseccomp) and its logic is tested against
  a small BPF interpreter; the integration test checks it in a real sandbox.
- **Fail closed.** If a sandbox cannot be made (no bubblewrap, no user namespaces, the container's
  seccomp profile or `/proc` in the way) the provider does **not** run: `health` says
  `sandbox unavailable: <reason>`, and the sandbox is checked again every 30 s. There is no unconfined
  fallback.

### Egress

The sandbox has **no network**. The only thing it can reach is the Unix socket of its provider's egress
proxy (in the gateway), through a relay the runner starts on the sandbox's loopback (`127.0.0.1:<port>`
forwarding to that socket) and the standard proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
upper and lower case, `NO_PROXY` empty). A provider that ignores the variables has no route at all.

The proxy understands `CONNECT host:port` and absolute-form `http://` requests (rewritten to origin form,
one request per connection) and refuses everything else. It allows:

- the **fixed destinations** of the manifest (`egress`, from the verified image labels), which must resolve to
  **public** addresses. The host of an entry may be a **wildcard**, see below;
- the destinations the **current values of the provider's `egress_from_settings` settings** name (a URL gives
  its host and port, a bare host the ports 80 and 443). These may be private (a LAN server, a Tailscale
  address), because the user typed them;
- for the settings of **`tcp_from_settings`** (a protocol that is not HTTP), the host their current value
  names on any port, or on the range the manifest declares, see [Raw TCP](#raw-tcp-ftp).

Loopback, link-local (cloud metadata), unspecified, multicast and reserved addresses are never reachable. The
proxy resolves the name itself and connects to the address it checked, so a name cannot change meaning
between the check and the connection. Every refusal is logged
(`egress refused: provider=… target=… reason=…`) and kept in the provider's `egress.refused` of `ctl list`.

The answers the proxy gives itself carry `X-Musizerr-Egress`:
- `refused` on a 403, when the destination is not allowed;
- `unreachable` on a 502, when it did not answer.

A provider's HTTP library sees an ordinary response, and `musizerr_sdk.failures` classifies it as what it is. A destination that does not answer is `unreachable`, so a 503 and a retry, never the `upstream_error` of a server that answered with a 502; a refusal is `failed`. An HTTPS tunnel the proxy does not open reaches httpx as a `ProxyError` whose text starts with the proxy's status, and is classified the same way. A provider does not have to know any of this.

**How the setting-named destinations reach the gateway.** Only the host knows the settings and the sandbox
must not be able to influence them, so the host tells the gateway: `GatewayProviderContext` (the context of a
provider behind the gateway) takes the call's snapshot and, if the manifest has `egress_from_settings`,
`tcp_from_settings` or `filesystem_from_settings`, sends
`{"op": "egress", "provider", "targets", "ttl", "tcp", "paths"}` over the control socket **before** the call, when
the values changed or the grant is about to expire. A grant replaces the previous one; the destinations expire
(120 s), so a host that stops asking withdraws them; it is renewed after 60 s. (Folders do not expire, see
below.) `tcp` and `paths` are per SETTING: the gateway applies the ports and the mode the VERIFIED image declares
for that setting, never ones the host sent, and ignores a setting the manifest does not declare. Calls do not go
through the gateway; the cost is one local round trip per change. The alternative of relaying the values
through the runner was rejected: the runner is the provider's code. Pushing on every settings write was
rejected too: there are four processes reading the same table and a restarted gateway would have lost the push.

### Wildcards

An `egress` entry may be `*.example.com:443`: any **subdomain** of `example.com` on that port (`a.example.com`,
`a.b.example.com`), for a service that lives on a CDN. It never matches `example.com` itself (list it too if it
is needed), another port, or an address. The SDK refuses `*`, `*.com` (at least two labels must follow `*.`),
`a.*.example.com`, a wildcard on an IP address, and a wildcard in a name derived from a setting
(`egress_from_settings` and `tcp_from_settings` read hosts from values, and `*` is no host name). The gateway
resolves the name itself and a wildcard match is held to the rule of every fixed destination: **public addresses
only**, so `evil.example.com` pointing at `10.0.0.7` or `127.0.0.1` is refused. A wildcard cannot tell that
`example.co.uk` is a public suffix (that takes a list of them): the admin sees the pattern in words
("any subdomain of …") when installing and updating, and an update that adds a wildcard asks for more.

### Raw TCP (FTP)

A protocol that is not HTTP opens its own sockets, and FTP's passive mode opens data connections on **ports the
server chooses at run time**. The manifest declares `tcp_from_settings: [{"setting": "host", "ports": "any"}]`
(or `"2121"`, or a range `"30000-30100"`): the host the setting names, reachable on those ports, private
addresses included (the user typed the host), still never loopback or link-local. The admin sees "Can open
connections on any port to the host you set in “host”". The sandbox has no network, so the library's sockets go
through the same proxy with `CONNECT host:port`: `musizerr_sdk.net` is the helper.

```python
from musizerr_sdk import net

sock = net.create_connection(("files.example.org", 2121), timeout=10)   # a normal socket; same signature as socket.create_connection
```

The socket's `getpeername()` is the destination (not the proxy's loopback). Outside a sandbox
(`net.use_proxy` was never called, which the runner does) it is `socket.create_connection` unchanged, so the
provider needs no branch. `ProxyRefused` (a `PermissionError`: the manifest does not allow it, or no setting
names it now) and `ProxyUnreachable` (a `ConnectionError`) are `OSError`s, which libraries already handle.

For `ftplib`, which calls `socket.create_connection` for the control connection and for every passive data
connection, `net.route_module(ftplib)` gives that module a view of `socket` in which only that function goes
through the proxy (the rest of the process keeps the real `socket`):

```python
import ftplib
from musizerr_sdk import net

net.route_module(ftplib)                      # once, at import
ftp = ftplib.FTP()
ftp.connect(host, 2121, timeout=10)           # control connection: CONNECT host:2121
ftp.login(user, password)
ftp.retrbinary("RETR song.flac", handle)      # data connection: CONNECT host:<port from the 227 reply>
```

Two settings that name the same host are judged by the host: the proxy cannot know which setting a connection is for, so the wider range applies to it.

Only passive mode works (`ftplib`'s default): active mode asks the server to connect back, and a sandbox accepts
nothing. `FTP` connects the data connection to the control socket's peer address, which is the destination
host, so the (often private, NAT-confused) address in the `227` reply is never used. A server's data ports
can be narrowed by declaring the range instead of `any`.

### Folders named by settings

A provider that declares `filesystem_from_settings: [{"setting": "root_dir", "mode": "rw"}]` (local storage) gets
the folder that setting holds, bound into its sandbox **at the same absolute path**, read-write or read-only as
declared. The host sends the CURRENT value with the grant (`paths`), and the gateway accepts it only if:

- it is absolute and normalized (no `..`, `//` or trailing `/`) and is not `/`;
- it lies inside one of the roots of **`GATEWAY_FILESYSTEM_ROOTS`** (`:`-separated; empty: no folder is ever
  granted; the compose files set `/downloads`, the library) **and so does what it resolves to**: a link inside the root
  that leads out of it is refused. A root that is `/`, relative, inside or holding the gateway's own state or run
  directory, or one of `/proc`, `/dev`, `/tmp`, `/run`, `/workspace` stops the gateway from starting;
- it exists and is a directory, is not inside the workspace directory, and does not collide with what every sandbox
  mounts itself. When it **holds** the workspace directory (the library root holds `.musizerr-work`), that
  directory is covered by an empty `tmpfs`: another provider's files are not for this one;
- no other setting of the same provider already claimed it.

The mode is the one the verified image declares; `ro` is a read-only bind (a write fails with `EROFS`). What is
mounted is the **resolved** path, checked again at every start of the process. A refused folder is not mounted,
and the reason is in `ctl list` (`folders.refused`) and the gateway's log; the provider keeps running.

*Lifecycle.* Mounts cannot change under a running process, so when the set of binds a grant leads to differs from
the one the process was started with, **the process is restarted** (at once, no backoff, not counted as a crash)
and the grant's reply waits (up to 10 s) for the new one, so the call that carried the grant reaches it. The same
grant again changes nothing. Until a grant arrives a provider runs **without** the folder: its root is read-only
and the path does not exist, so a call that needs it fails cleanly. Not starting it until the grant came would
deadlock: the host connects to, and so grants for, only a provider that runs. Folders do not expire with the
destinations (a mount lapsing would restart the provider every two minutes); they stay until a grant replaces
them, and a restarted gateway forgets them until the host's next grant (every call that finds the grant older than 60 s, or changed, sends it again): for that time the provider runs without its folders.

*Residual details.* The image's root filesystem needs a mount point at the path: the gateway makes empty
directories there (never following a link of the image) and leaves them when a setting changes. Between the
check and `bwrap` there is a small window in which someone with write access under the root could swap a
directory for a link; the source of the mount is the resolved path, re-checked at every start, and a sandbox with
a read-write folder can only create links inside it. Files created in a read-write folder belong to the
gateway's uid (the sandbox's uid is mapped to it): the folder must be writable by it.

### The seccomp profile of the container

`gateway/seccomp/musizerr-gateway.json` is Docker's default profile (`moby-default.json`, copied verbatim
from the `github.com/moby/profiles/seccomp` vendored in moby tag `docker-v29.4.0`, sha256
`01536f1d…01e28`) plus **one appended, unconditional entry** allowing `clone`, `mount`, `pivot_root`,
`umount2` and `unshare`. Docker's default profile allows those only to a container with `CAP_SYS_ADMIN`
(`clone` only without namespace flags) and does not know `pivot_root` at all. All five are needed and
sufficient (each was checked by removing it). Nothing else changes: `clone3` still answers `ENOSYS`,
`setns`, `bpf`, `perf_event_open`, `fsopen`/`fsmount`/`move_mount`/`open_tree`/`mount_setattr`, `keyctl`,
`userfaultfd`, `io_uring` stay denied. `gateway/seccomp/derive.py` makes the file from the default; a test
checks that the shipped file is exactly that and the copy of the default is the one it says it is. To update:
fetch the new default, update `MOBY_SHA256` in `gateway/tests/test_seccomp_profile.py`, run `derive.py`.

`systempaths=unconfined` is also required: a sandbox mounts its own `/proc`, which the kernel refuses while
the container's `/proc` has Docker's masked paths mounted over it (`bwrap: Can't mount proc`). The sandboxes
get their own fresh `/proc`; the container is not privileged and has no added capability (`cap_drop: [ALL]`).

Neither is optional. With Docker's default profile bubblewrap fails with `No permissions to create a new
namespace`, and the gateway reports it and runs nothing (integration test, check c).

**Host requirements.** A kernel with unprivileged user namespaces (`kernel.unprivileged_userns_clone`,
`user.max_user_namespaces` > 0), and no AppArmor/SELinux policy that denies them to containers (recent Ubuntu
has `kernel.apparmor_restrict_unprivileged_userns`; a profile for the gateway container or setting it to 0 is
needed there). Where that is impossible the providers outside the application do not run.

## Control socket

`<run_dir>/gateway.sock` (default `/run/musizerr/gateway.sock`), mode 0600; one JSON object per line in, one
out. `python -m musizerr_gateway.ctl [list|health|install|grant]` is a client (`install` and `grant` read their request from standard input, as the host would send it: for tests and for looking at a gateway on its own).

| request | answer |
|---|---|
| `{"op": "list"}` | `epoch` (changes when the gateway restarts), `desired` (the identifier of the set the host last pushed, null before the first push), `generation` (changes when the set of running providers does), `workspace_root`, `sandbox` (`available`, `reason`), `rejected` (entries of the file that are unusable, with why), and per provider: `state` (`pulling`, `starting`, `running`, `restarting`, `refused`, `unavailable`, `failed`, `stopped`), `reason`, `restarts`, `socket` (relative to the run directory, when running), `declared` (name, roles, API version, permissions of its image), `egress` (`fixed`, wildcards included; `granted`; `tcp`, the hosts of `tcp_from_settings` with their ports; `refused`), `folders` (`granted`: path and mode of what is bound into the running process; `refused`: setting -> why its path is not) |
| `{"op": "health"}` | `status`: `ok`, `degraded` (a provider is down) or `unavailable` (no sandbox can be made: nothing runs), the sandbox verdict and the state of each provider |
| `{"op": "egress", "provider", "targets": ["host:port"], "ttl", "tcp": {setting: [host]}, "paths": {setting: path}}` | the host's grant, see above: `granted` (destinations understood), `paths` (`granted`, `refused`), `restarted` (the process was restarted to change its mounts; the reply came after the new one ran) |
| `{"op": "install", "providers": [...], "generation": "..."}` | the desired set, see above: `accepted` (names) and `rejected` (name: reason) |

## Sockets, volumes and permissions

- `provider_sockets` (named volume) is mounted at `/run/musizerr` in the gateway, the app and the three
  workers (not `init`, `beat` or the frontend). It holds `gateway.sock` and `providers/<name>/provider.sock`.
- The directories are 0700 and the sockets 0600, owned by uid 1000. **Whoever can open a provider's socket can
  call it with any session**, credentials included, so only the services that need it mount the volume and
  nobody else can read it. The gateway runs as 1000, the application's user in production (the `Dockerfile` creates
  `/run/musizerr` owned by it, so the volume is initialised right whoever starts first). In development the app
  runs as root, which reaches the sockets anyway.
- The workspace directory (`workspace_dir`, default `/downloads/.musizerr-work`) must be the same path in the
  gateway (`GATEWAY_WORKSPACE_DIR`) and the host. A call's workspace is
  `<workspace_dir>/<provider>/<random>`; the sandbox of `<provider>` sees `<workspace_dir>/<provider>` as
  `/workspace` and nothing else. The provider folder is 0711 (crossed, not listed), the call folder 0777: the
  sandbox's uid is mapped to the gateway's uid, which may differ from the host's. A file that moves from one
  provider to another (downloaded by one, stored by another; a file held for a later upload) is **staged**
  by the transport: hard-linked (copied when it cannot be) into a folder of the second provider's tree for
  the duration of the call. A file outside the workspace tree is never staged.
- `gateway_state` holds the layer cache, the unpacked images and the egress sockets (not shared).

## Operations

- Logs: the provider's output is forwarded, prefixed with `[name]`, and so are the restarts and the refused egress.
- Add a provider in development: `scripts/dev-catalog.sh <provider-dir>...` ([catalog.md](catalog.md#development)) builds
  provider directories (anywhere on the machine) with `scripts/build-provider-image.sh`, pushes them to the
  `oci-registry` service of `docker-compose.dev.yml` (bound to the loopback) and writes their entries in
  `data/catalogs/registry.json`, which the `registry` service serves as `http://registry:9000/registry.json`. Install from that
  catalog in Administration or with `python -m scripts.provider_sources`; the source is added with "allow insecure
  registry", because the image name is the one the gateway reaches (`oci-registry:5000`) over plain HTTP. The build
  script builds the base image locally (`ghcr.io/sossiogll/musizerr-provider-base:5`, never pushed), has the provider print
  its labels, and refuses to push to ghcr.io or Docker Hub. Published images are pushed by other scripts
  ([catalog.md](catalog.md#publishing-to-ghcr)): for linux/amd64 and linux/arm64, pinned by the digest of their index.
- Images that are no longer installed are not removed from the cache yet.
- `scripts/test-gateway-integration.sh`, `scripts/test-sources-integration.sh` and
  `scripts/test-provider-infra-integration.sh` (wildcards, raw TCP, folders from settings, and the dev catalog script) run the
  whole thing for real (see their headers).

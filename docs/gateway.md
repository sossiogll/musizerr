# The provider gateway

`provider-gateway` is the service that runs providers that are not part of the application
(ADR-0006, phase 3). It pulls a provider's OCI image **by digest**, checks it, unpacks it, and runs it in
a [bubblewrap](https://github.com/containers/bubblewrap) sandbox with no network of its own. The
application and the workers talk to the provider over a Unix socket; they never install or start anything.

Every provider lives this way: none is part of the application, and the host registers only what the gateway runs
and declares. The first-party ones are in their own repositories (the public ones in `musizeer-providers`).

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
    "image": "ghcr.io/sossiogll/musizeer-provider-lrclib",
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
3. **Labels.** The image describes itself with `org.musizeer.provider.*` labels (name, roles, contract
   version, permissions), generated from the provider by `python -m musizeer_sdk.runner --print-labels`.
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
prlimit --as=… --nofile=… --core=0 --
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
- `MALLOC_ARENA_MAX=2`, the same for every provider, unless the image declares its own. glibc gives each thread that
  allocates a malloc arena of its own (up to 8 per CPU) and reserves **64 MB of address space** for each; the sandbox's address
  space is limited (`limits.memory_mb`, 1024 MB by default), so a runner, which serves one thread per concurrent call, ran out
  of it at 14-18 threads (`can't start new thread`) with next to no memory in use. With two arenas the same sandbox
  starts about 113 threads at the default 8 MB stacks (measured by `scripts/test-gateway-tasks-integration.sh`). The arenas are
  address space, not memory: the limit on real memory is not raised.
- Limits: address space and open files (`prlimit`, limits of each process) and processes and threads (counted
  by the gateway, [below](#processes-and-threads)) from the manifest, capped at the gateway's ceilings
  (`GATEWAY_MAX_MEMORY_MB`, `GATEWAY_MAX_OPEN_FILES`, `GATEWAY_MAX_PROCESSES`); defaults when the manifest asks for
  nothing (1024 MB, 1024 files, 512 tasks). **No `RLIMIT_NPROC` is set**, see below.
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
  seccomp profile, AppArmor or `/proc` in the way) the provider does **not** run: `health` says
  `sandbox unavailable: <reason>` (with a hint on what to change when the reason is a known one, see
  [Troubleshooting](#troubleshooting)), and the sandbox is checked again every 30 s. There is no unconfined
  fallback.

### Processes and threads

`limits.processes` of a manifest (default 512, at most `GATEWAY_MAX_PROCESSES`, 1024) is **the number of tasks, processes and
threads together, that the whole sandbox of a provider may hold**. Two things enforce it:

- **The gateway counts.** Every 2 seconds it adds up the threads of each sandbox's process tree (`/proc`, `tasks.py`). A
  provider above its limit in two samples in a row (one sample over is a burst) is stopped and started again with the usual
  pause; `ctl list` says `process/thread limit reached: the sandbox held N processes and threads, over its limit of M`
  (`reason`) and the log has the same, with the numbers of the container. It is sampling: it catches a provider that leaks or
  runs away, not a burst between two samples.
- **The container's pids limit is the hard cap.** `pids_limit` of the gateway service (`GATEWAY_PIDS_LIMIT`, default 4096) is the
  cgroup `pids` controller: it counts the tasks of THIS container only, the gateway and every sandbox in it, and nothing else
  on the host. A fork bomb ends there and never reaches the host's process table. The gateway logs the limit at start (a
  warning when there is none) and shows it in `ctl list` and `ctl health` (`container`: `pids`, `pids_limit`). The limit is
  shared by the whole container: while a provider's burst holds every task of it, the gateway cannot start the thread that
  answers its control socket either (the host sees the gateway as unreachable) until the burst ends or the count above stops
  the provider. That is why the default is far above what the sandboxes hold at rest (a few tasks each) and a provider is
  stopped at its own limit long before the container's.

**Why there is no `RLIMIT_NPROC`.** It looks like the obvious tool (`--nproc` was set on every sandbox up to 1.2.0) and is the wrong one:
the kernel does not check it against the process or its sandbox but against the number of tasks of the same **real uid in
the whole machine**, in every container and outside them. The gateway runs as uid 1000, and on a typical host that is the main
account, which owns the tasks of other containers (a media server, a Node service, Celery...). On a server with 481 of them
already, `--nproc=512` left 31 tasks for every provider in the world: a runner holding two threads got `RuntimeError: can't
start new thread` when two requests came together, and the host saw `Connection reset by peer` from the provider. A uid of its
own for the gateway (a dedicated `10001`, say) would fix the neighbours but not the meaning: all the sandboxes and the gateway
share that uid, so the limit would be one cap for all of them (a provider that asks for 64 would fail as soon as the others held
64) and every file a sandbox writes into the library would belong to a uid that your media server does not know (the sandbox's
uid is mapped to the gateway's, see [Sockets, volumes and permissions](#sockets-volumes-and-permissions)). The pids
controller and the gateway's own count give the per-sandbox and the per-container limits that `RLIMIT_NPROC` cannot, whatever
uid the gateway has, so the uid stays what it was and nothing needs to change ownership.

A provider that cannot create a thread says so (`RuntimeError: can't start new thread`, Go's `failed to create new OS
thread`, `fork: retry: Resource temporarily unavailable`), and bubblewrap says `Resource temporarily unavailable` when it cannot
even start. The gateway recognises both, once a minute at most writes to its log

```
tasks: process/thread limit reached (RLIMIT_NPROC / pids): the provider reported "RuntimeError: can't start new thread" (3 so far).
EAGAIN: the kernel would not create a process or a thread. ... [container pids: 4096 of 4096 (pids_limit); RLIMIT_NPROC of the
gateway: 32137, counted over every process of uid 1000 on the whole host; sandbox tasks: 61 of 64 (limits.processes)]
```

and counts it in the provider's `task_limit_hits` (`ctl list`). A bubblewrap `EAGAIN` becomes `sandbox unavailable: ... (hint: EAGAIN: ...)`
with the same numbers. If `container pids` is at its limit, raise `GATEWAY_PIDS_LIMIT`; if it is far below it, something outside the
gateway limits `RLIMIT_NPROC` for the uid (see the [troubleshooting table](#troubleshooting)).

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

The answers the proxy gives itself carry `X-Musizeer-Egress`:
- `refused` on a 403, when the destination is not allowed;
- `unreachable` on a 502, when it did not answer.

A provider's HTTP library sees an ordinary response, and `musizeer_sdk.failures` classifies it as what it is. A destination that does not answer is `unreachable`, so a 503 and a retry, never the `upstream_error` of a server that answered with a 502; a refusal is `failed`. An HTTPS tunnel the proxy does not open reaches httpx as a `ProxyError` whose text starts with the proxy's status, and is classified the same way. A provider does not have to know any of this.

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
through the same proxy with `CONNECT host:port`: `musizeer_sdk.net` is the helper.

```python
from musizeer_sdk import net

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
from musizeer_sdk import net

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
  granted; the compose files set `/downloads`, the library volume, unless `.env` says otherwise: several roots are
  `GATEWAY_FILESYSTEM_ROOTS=/downloads:/library`, [deployment.md](deployment.md#folders)) **and so does what it resolves to**: a link inside the root
  that leads out of it is refused. A root that is `/`, relative, inside or holding the gateway's own state or run
  directory, or one of `/proc`, `/dev`, `/tmp`, `/run`, `/workspace` stops the gateway from starting;
- it exists and is a directory, is not inside the workspace directory, and does not collide with what every sandbox
  mounts itself. When it **holds** the workspace directory (the library root holds `.musizeer-work`), that
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

`gateway/seccomp/musizeer-gateway.json` is Docker's default profile (`moby-default.json`, copied verbatim
from the `github.com/moby/profiles/seccomp` vendored in moby tag `docker-v29.4.0`, sha256
`01536f1d…01e28`) plus **one appended, unconditional entry** allowing `clone`, `mount`, `pivot_root`,
`umount2` and `unshare`. Docker's default profile allows those only to a container with `CAP_SYS_ADMIN`
(`clone` only without namespace flags) and does not know `pivot_root` at all. All five are needed and
sufficient (each was checked by removing it). Nothing else changes: `clone3` still answers `ENOSYS`,
`setns`, `bpf`, `perf_event_open`, `fsopen`/`fsmount`/`move_mount`/`open_tree`/`mount_setattr`, `keyctl`,
`userfaultfd`, `io_uring` stay denied. `gateway/seccomp/derive.py` makes the file from the default; a test
checks that the shipped file is exactly that and the copy of the default is the one it says it is. To update:
fetch the new default, update `MOBY_SHA256` in `gateway/tests/test_seccomp_profile.py`, run `derive.py`.

Two more settings of the container are required, and neither is a capability:

- `systempaths=unconfined`: a sandbox mounts its own `/proc`, which the kernel refuses while the container's `/proc`
  has Docker's masked paths mounted over it (`bwrap: Can't mount proc`). The sandboxes get their own fresh `/proc`.
- `apparmor=unconfined`: on a host with AppArmor (Debian, Ubuntu, ...) Docker confines every container with the
  `docker-default` profile, which denies the namespace calls bubblewrap makes (`bwrap: Creating new namespace failed:
  Permission denied`, an `EACCES`; seccomp, which the profile above takes care of, answers `EPERM` instead). On a
  host without AppArmor the option does nothing. Only the gateway container loses the profile; it stays
  unprivileged, with `cap_drop: [ALL]`, the seccomp profile above and no network of its own to reach.

All of them go in `security_opt`, in every compose file that runs the gateway:

```yaml
provider-gateway:
  user: "1000:1000"
  pids_limit: ${GATEWAY_PIDS_LIMIT:-4096}                 # the processes and threads of the whole container: see "Processes and threads"
  cap_drop: [ALL]
  security_opt:
    - seccomp=./gateway/seccomp/musizeer-gateway.json    # or musizeer-gateway.seccomp.json next to a release's compose file
    - apparmor=unconfined
    - systempaths=unconfined
```

**Never `privileged: true`**, and never a `cap_add`. `privileged` "fixes" this by switching AppArmor and
seccomp off and handing the container every device and capability of the host: the sandbox exists to keep a provider away from
all of that, and a gateway that runs privileged is the host to anything that gets out of a sandbox. If the three options
above are not enough on your host, the answer is one of the host settings in the table below, not `privileged`.

Neither is optional. With Docker's default profile bubblewrap fails with `No permissions to create a new
namespace`, and the gateway reports it and runs nothing (integration test, check c).

**Host requirements.** A kernel with unprivileged user namespaces (`kernel.unprivileged_userns_clone`,
`user.max_user_namespaces` > 0), and no AppArmor/SELinux policy that denies them to containers (recent Ubuntu
has `kernel.apparmor_restrict_unprivileged_userns`: `apparmor=unconfined` covers a container, or set the sysctl to 0).
Where that is impossible the providers outside the application do not run.

## Troubleshooting

`docker compose exec provider-gateway python -m musizeer_gateway.ctl health` (or `docker compose logs provider-gateway`)
says `sandbox unavailable: <bubblewrap's error> (hint: ...)` when no sandbox can be made; the gateway adds the hint
for the errors below (`SANDBOX_HINTS` in `sandbox.py`) and checks again every 30 s, so fixing the container and
`docker compose up -d provider-gateway` is enough. Nothing runs unconfined in the meantime.

| What bubblewrap says | Errno | Cause | Fix |
|---|---|---|---|
| `Creating new namespace failed: Permission denied` | `EACCES` | AppArmor: Docker's `docker-default` profile denies the namespace calls. This is what a stock Debian or Ubuntu host shows. | `apparmor=unconfined` in the gateway's `security_opt` (not `privileged`). |
| `setting up uid map: Permission denied` | `EACCES` | An AppArmor policy that restricts unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`, Ubuntu 24.04+). | `apparmor=unconfined` for the container, or `sysctl kernel.apparmor_restrict_unprivileged_userns=0` on the host. |
| `No permissions to create a new namespace` (older: `...to creating new namespace, likely because the kernel does not allow non-privileged user namespaces`) | `EPERM` | Docker's default seccomp profile is in use (the `seccomp=` file was not found or not given), or the kernel forbids unprivileged user namespaces. | The `seccomp=` option must name `musizeer-gateway.json` as a path that exists **on the Docker host** (`GATEWAY_SECCOMP_PROFILE`, [deployment.md](deployment.md#apparmor-and-seccomp)); on Debian `sysctl kernel.unprivileged_userns_clone=1`; check `sysctl user.max_user_namespaces` is not 0. |
| `Creating new namespace failed, likely because the kernel does not support user namespaces` | `EINVAL` | The kernel has no user namespaces, or `user.max_user_namespaces` is 0. | `sysctl user.max_user_namespaces=15000` (persist it in `/etc/sysctl.d/`); a kernel built without `CONFIG_USER_NS` cannot run the gateway. |
| `Can't mount proc on /newroot/proc: Operation not permitted` | `EPERM` | The container's `/proc` is masked. | `systempaths=unconfined` in `security_opt`. |
| `pivot_root: Operation not permitted`, `Can't mount tmpfs ...: Operation not permitted` | `EPERM` | The seccomp profile in use does not allow `mount`, `pivot_root`, `unshare`, `clone`, `umount2`. | The gateway's profile, not Docker's default (above). |
| `bwrap: execvp ...: No such file or directory`, `bubblewrap is not installed` | | A gateway image that is not the published one. | Pull `musizeer-gateway`. |
| `Creating new namespace failed: Resource temporarily unavailable`, `fork: Resource temporarily unavailable`; or, in a provider's output and the gateway's log, `process/thread limit reached (RLIMIT_NPROC / pids)` and `can't start new thread` | `EAGAIN` | The kernel would not create a process or a thread. The message carries the numbers: `container pids: N of M (pids_limit)` and `RLIMIT_NPROC of the gateway`. Not a permission problem: no AppArmor or seccomp change helps. | `N` close to `M`: raise `GATEWAY_PIDS_LIMIT` (and look at `tasks` of each provider in `ctl list`: a provider that leaks threads is stopped at its `limits.processes`). `N` far below `M`: an `RLIMIT_NPROC` is set on the gateway from outside (`ulimits: nproc` in a compose override, `default-ulimits` in `/etc/docker/daemon.json`, `LimitNPROC` of the Docker unit) and the kernel counts it over every process of the uid on the host; remove it, the pids limit does the job. |

The same table, from the host's side: `EACCES` is a **Linux security module** (AppArmor, SELinux) saying no; `EPERM` is
**seccomp** or a **sysctl**. On an SELinux host (Fedora, RHEL) the container may need `label=disable` in
`security_opt` for the same reason; that is the equivalent of `apparmor=unconfined` and has not been tested here.
A profile of its own for the container, allowing what bubblewrap needs instead of unconfining it, is an alternative that has not been written or tested here.

A provider that is `refused` or `failed` (not `unavailable`) is another matter: `ctl list` gives its `reason`.

## Control socket

`<run_dir>/gateway.sock` (default `/run/musizeer/gateway.sock`), mode 0600; one JSON object per line in, one
out. `python -m musizeer_gateway.ctl [list|health|install|grant]` is a client (`install` and `grant` read their request from standard input, as the host would send it: for tests and for looking at a gateway on its own).

| request | answer |
|---|---|
| `{"op": "list"}` | `epoch` (changes when the gateway restarts), `desired` (the identifier of the set the host last pushed, null before the first push), `generation` (changes when the set of running providers does), `workspace_root`, `sandbox` (`available`, `reason`), `rejected` (entries of the file that are unusable, with why), and per provider: `state` (`pulling`, `starting`, `running`, `restarting`, `refused`, `unavailable`, `failed`, `stopped`), `reason`, `restarts`, `socket` (relative to the run directory, when running), `declared` (name, roles, API version, permissions of its image), `egress` (`fixed`, wildcards included; `granted`; `tcp`, the hosts of `tcp_from_settings` with their ports; `refused`), `folders` (`granted`: path and mode of what is bound into the running process; `refused`: setting -> why its path is not), `tasks` (the processes and threads of its sandbox: `current`, `peak`, `limit` = its `limits.processes`) and `task_limit_hits` (`count` and `last` words of the times it reported that the kernel would not create a thread); and `container` (the gateway container's `pids` and `pids_limit`, null when there is none or it cannot be read) |
| `{"op": "health"}` | `status`: `ok`, `degraded` (a provider is down) or `unavailable` (no sandbox can be made: nothing runs), the sandbox verdict, `container` (as in `list`) and the state of each provider |
| `{"op": "egress", "provider", "targets": ["host:port"], "ttl", "tcp": {setting: [host]}, "paths": {setting: path}}` | the host's grant, see above: `granted` (destinations understood), `paths` (`granted`, `refused`), `restarted` (the process was restarted to change its mounts; the reply came after the new one ran) |
| `{"op": "install", "providers": [...], "generation": "..."}` | the desired set, see above: `accepted` (names) and `rejected` (name: reason) |

## Sockets, volumes and permissions

- `provider_sockets` (named volume) is mounted at `/run/musizeer` in the gateway, the app and the three
  workers (not `init`, `beat` or the frontend). It holds `gateway.sock` and `providers/<name>/provider.sock`.
- The directories are 0700 and the sockets 0600, owned by uid 1000. **Whoever can open a provider's socket can
  call it with any session**, credentials included, so only the services that need it mount the volume and
  nobody else can read it. The gateway runs as 1000, the application's user in production (the `Dockerfile` creates
  `/run/musizeer` owned by it, so the volume is initialised right whoever starts first). In development the app
  runs as root, which reaches the sockets anyway.
- The workspace directory (`workspace_dir`, default `/downloads/.musizeer-work`) must be the same path in the
  gateway (`GATEWAY_WORKSPACE_DIR`) and the host, and lie inside a volume all of them mount. Compose sets both from
  ONE variable, `WORKSPACE_DIR` (in `.env`): the gateway gets it as `GATEWAY_WORKSPACE_DIR`, the host as the default of its
  `workspace_dir` setting. A value saved in the settings wins over that default, so changing the setting in the
  interface without changing `WORKSPACE_DIR` desyncs them; the API compares its setting with the gateway's
  `workspace_root` every few seconds and logs an error (once per pair of values) when they differ. A call's workspace is
  `<workspace_dir>/<provider>/<random>`; the sandbox of `<provider>` sees `<workspace_dir>/<provider>` as
  `/workspace` and nothing else. The provider folder is 0711 (crossed, not listed), the call folder 0777: the
  sandbox's uid is mapped to the gateway's uid, which may differ from the host's. A file that moves from one
  provider to another (downloaded by one, stored by another; a file held for a later upload) is **staged**
  by the transport: hard-linked (copied when it cannot be) into a folder of the second provider's tree for
  the duration of the call. A file outside the workspace tree is never staged.
- `gateway_state` holds the layer cache, the unpacked images and the egress sockets (not shared).
- **The gateway's uid is about files, not about limits.** The sandbox's uid (10001 inside) is mapped to the gateway's, so what a
  provider writes into a granted library folder belongs to the gateway's uid (1000 by default) and to its group. That is why it
  is the application's uid: the library, the sockets and the workspaces then share an owner, and a media server that reads the
  library as uid 1000 sees the files as its own. Nothing about the number of processes depends on it (see
  [Processes and threads](#processes-and-threads)): change `user:` only for the files' sake, and then `chown` the library, the
  `gateway_state` and `provider_sockets` volumes to match.

## Operations

- Logs: the provider's output is forwarded, prefixed with `[name]`, and so are the restarts and the refused egress.
- Add a provider in development: `scripts/dev-catalog.sh <provider-dir>...` ([catalog.md](catalog.md#development)) builds
  provider directories (anywhere on the machine) with `scripts/build-provider-image.sh`, pushes them to the
  `oci-registry` service of `docker-compose.dev.yml` (bound to the loopback) and writes their entries in
  `data/catalogs/registry.json`, which the `registry` service serves as `http://registry:9000/registry.json`. Install from that
  catalog in Administration or with `python -m scripts.provider_sources`; the source is added with "allow insecure
  registry", because the image name is the one the gateway reaches (`oci-registry:5000`) over plain HTTP. The build
  script builds the base image locally (`ghcr.io/sossiogll/musizeer-provider-base:5`, never pushed), has the provider print
  its labels, and refuses to push to ghcr.io or Docker Hub. Published images are pushed by other scripts
  ([catalog.md](catalog.md#publishing-to-ghcr)): for linux/amd64 and linux/arm64, pinned by the digest of their index.
- Images that are no longer installed are not removed from the cache yet.
- `scripts/test-gateway-integration.sh`, `scripts/test-sources-integration.sh`,
  `scripts/test-provider-infra-integration.sh` (wildcards, raw TCP, folders from settings, and the dev catalog script) and
  `scripts/test-gateway-tasks-integration.sh` (processes and threads: a second container holding 700 threads as the gateway's
  uid, a provider stopped over its `limits.processes`, a container with a small pids limit) run the whole thing for real (see
  their headers).

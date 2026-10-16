# ADR-0006: Distributing providers as OCI images on GHCR and running them in a sandbox

**Status:** Proposed. Distribution on GHCR was decided by the maintainer on 2026-10-12;
the rest awaits review.
**Date:** 2026-10-12
**Deciders:** @sossiogll (maintainer)
**Supersedes:** the Harbor decision of August 2026 (never recorded as an ADR)

## Context

Musizerr's promise is that every provider is a plugin anyone can write, publish and
install, and that the core never knows which providers exist. The contract side of that
promise is largely in place:

- providers reach the host only through `ProviderContext`, the `AuthSession` passed to
  each call and the contract's return values (CLAUDE.md, "The Host Boundary");
- files cross the boundary through host-owned workspaces (ADR-0003);
- `test_layering.py` forbids providers from importing Redis, Celery, SQLModel/SQLAlchemy
  or any host package, and from reading `os.environ`;
- `PROVIDER_API_VERSION` (currently 4) versions the contract, and the registry refuses an
  external package that declares another version.

The distribution and execution side is not. As of 2026-10-12:

1. **Every provider ships inside the core.** All 13 providers live in `app/providers/`:
   two OAuth2 catalog providers, MusicBrainz, a third catalog provider (metadata and
   download), YouTube, Lidarr, a download provider that drives a streaming library, FTP,
   local storage, Navidrome, LRCLIB and AcoustID. The application image installs their
   dependencies too: `requirements-acoustid.txt` in every stage and a requirements file
   for the streaming-library provider in the development stage (kept out of production
   for licensing reasons). A route of the host serves that provider's external utility.
2. **There is no SDK.** The example external package
   (`provider-packages/` holds one for the third catalog provider) imports `providers.base`,
   `providers.auth`, `providers.models` and `core.identifiers`, which are modules of the
   host's own source tree. It works only because it is imported into the host's
   interpreter. The four `providers/{base,auth,models,identifiers}.py` re-export modules
   act as an undeclared SDK. In-repo providers also import pure helpers from `core/`
   (`core.utils.parse_year`, `core.matching.normalize`/`strip_video_noise`,
   `core.audio_quality.parse_bitrate`, `core.capabilities` constants,
   `core.download_resolver.DownloadDelegated`/`DownloadDeclined`).
3. **Runtime installation does not work with the current deployment.**
   `services/sources.py` runs `pip install` in the process that handles the admin's
   request, which is the `app` container:
   - the three Celery workers (`downloads`, `jobs`, `maintenance`) never see the package,
     so an installed download provider is unknown exactly where downloads run;
   - no volume holds the installed package, so recreating the container loses it;
   - `ProviderRegistry.reload()` reloads only the process that received the request.
4. **There is no isolation.** An installed package runs in the host's process with the
   database credentials, `AUTH_SECRET`, the environment and the whole filesystem. Its
   dependencies resolve in the same environment as the core's, and `pip --dry-run` is
   the only conflict check. The catalog entry declares neither network destinations nor
   a content hash. `requires_core` is not checked, and `provider_api_version` is read
   only after the package is installed.
5. **Parts of the contract cannot cross a process boundary.**
   - OAuth2 auth strategies (`BasicClientAuth`, `BearerHeaderRequestAuth`,
     `QueryParamRequestAuth`...) are classes in `providers/auth_strategies.py` whose code
     the host's `core/auth_manager.py` executes.
   - The host classifies provider failures by exception type, e.g. the
     `httpx.HTTPStatusError` status (`services/errors.classify_error`).
   - Some results are iterators (`iter_saved_tracks`, `StorageStreamResult.chunks`,
     `list_files`).
   - The registry finds roles with `isinstance` (a sync provider that is also a metadata
     provider).
   - `core/serialization.py` renders API views. It adds `kind` and drops what the API
     does not show, so it is not a lossless wire format.

The threat model has not changed since August: providers are written by strangers,
published on registries we do not control and installed by end users. A provider must
not be able to harm the server it runs on, and first-party providers get no exemption.
Signing proves provenance, not harmlessness. Isolation has to come from the runtime.

In August the distribution channel was going to be a self-hosted Harbor. On 2026-10-12
the maintainer chose GitHub instead, specifically the GitHub Container Registry, with
providers published as OCI images.

## Decision

### 1. A provider is an OCI image on GHCR

A provider is published as a multi-architecture OCI image (`linux/amd64`,
`linux/arm64`), for example `ghcr.io/<owner>/musizerr-provider-<name>`. The image
contains its own Python, the SDK (§3), the provider's code and dependencies, and the
SDK's runner as entry point. Image labels (`org.musizerr.provider.*`) carry the
manifest (§4), so the image describes itself.

The gateway (§6) speaks the plain OCI distribution API with an anonymous token, so any
public OCI registry works. GHCR is the one we publish to and test against. Nothing in
the host is specific to GHCR.

### 2. A source is a catalog URL, and images are pinned by digest

Trust stays "by URL": the admin adds a source, which is the URL of a catalog file. It is
typically a raw file or a GitHub Pages page in the publisher's repository. A catalog
lists, for every provider, its image reference **and its digest**
(`ghcr.io/owner/x@sha256:…`) along with the manifest. The gateway pulls by digest only
and refuses an image whose labels disagree with the catalog. Installing pins the digest.
An update is a new digest that the admin approves, and the admin sees the difference in
declared permissions before approving. There is no image signing, consistent with the
August decision.

A catalog is still needed even though GHCR exists: listing an owner's packages through
the GitHub API requires a token even for public packages, and a catalog is also what
lets a publisher curate (and pin) what it offers.

### 3. The contract becomes an SDK: `musizerr-sdk`, contract version 5

The provider contract moves out of `app/core/` into a package, `musizerr-sdk`. It lives
in this repository under `sdk/`, is published to PyPI and is baked into the base image
`ghcr.io/sossiogll/musizerr-provider-base:<api-version>`. The SDK contains:

- the provider ABCs and the contract's data types (`WorkspaceFile`,
  `StorageStreamResult`, `SyncAvailability`, `Identity`, the delegation types);
- the domain models and `Identifier`;
- the declarative auth types;
- `ProviderContext` and `StaticProviderContext`;
- the exceptions a provider raises (`ProviderRateLimited`, `RemotePlaylistGone`,
  `DownloadDelegated`, `DownloadDeclined`). `ProviderNotConfigured` stays in the host,
  which raises it before any call;
- the subset of `SettingDef` a provider declares;
- the capability constants;
- the pure helpers providers need today (`parse_year`, `normalize`,
  `strip_video_noise`, `parse_bitrate`).

The SDK owns these modules and the host imports them from it, so nothing is duplicated.
Matching and every other policy stay in the host.

The SDK also contains the runner (§5) and a conformance kit, so a provider author can
test against `StaticProviderContext` and the wire codec without running Musizerr.

The break is deliberate and bumps `PROVIDER_API_VERSION` to 5:

- **Auth strategies become declarative.** They form a closed set of data types (e.g.
  `token_client_auth="basic" | "request_body"`, `request_auth="bearer" |
  "query_param(...)"`, `fallback="client_credentials" | "public_client"`). The host's
  `AuthManager` implements them. A new strategy is an SDK change, never code a provider
  ships to the host.
- **`ConfigField` and `configfield_to_settingdef` are removed.** They were kept only for
  external pip packages, which this ADR retires.
- **The four `providers/*.py` re-export modules are removed.**

### 4. The manifest declares what the host used to infer

Everything the host currently reads from a provider's class becomes manifest data,
fetched once when the provider is loaded. That covers:

- `name`, `label` and the roles it fills (replacing `isinstance` and the sync-to-metadata
  cross-registration);
- `auth`;
- `config_schema()`;
- `supports_*()` and the other capability declarations;
- `supported_identifier_types`, `max_concurrent_downloads`, `supports_fuzzy` and
  `streams_full_tracks`;
- `provider_api_version`.

The manifest also declares **permissions**, the only resources the sandbox will grant:

- `egress`: fixed hostnames and ports (e.g. `api.catalog.example:443`);
- `egress_from_settings`: setting keys whose value is a host the user configures (a
  Navidrome URL, a Lidarr URL, an FTP host), including private and Tailscale addresses
  when the user enters one;
- `filesystem_from_settings`: setting keys whose value is a host path the provider may
  use, with `ro` or `rw` (local storage's `root_dir`);
- resource limits (memory, open files), within the ceilings the host sets.

The admin sees the permissions when installing and sees any change when updating.

### 5. One call protocol, with no callbacks into the host

Host and provider exchange length-prefixed frames over a Unix socket. The protocol uses
the standard library only: JSON frames for calls and results, and binary frames for byte
streams. A call carries:

- the method and its arguments;
- the `AuthSession`;
- a **snapshot of the context**: the provider's settings, the four timeouts and the
  public URL prefix that `cover_url`/`stream_url`/`preview_url` are built from.

The runner gives the provider a `ProviderContext` built from that snapshot, so the
provider never calls back into the host. Setting changes take effect on the next call,
as they do today.

- **Codec.** The wire codec is a separate, lossless codec for every contract type.
  `core/serialization.py` stays the API view. Round trips are property-tested
  (Hypothesis is already a dev dependency).
- **Iterators.** These are streamed as frames: pages for `iter_saved_tracks`, names for
  `list_files` and bytes for `StorageStreamResult.chunks`. A stream that has started
  keeps its first byte deadline, `stream_start_timeout`.
- **Errors.** Errors are classified in the runner, where the exception types exist, by the
  SDK's classifier, which is the type-based part of today's `classify_error`. They travel
  as `{kind, message, retry_after}`, and the host raises a typed `ProviderCallError`. The
  contract's own exceptions (`DownloadDelegated`, `DownloadDeclined(code)`,
  `RemotePlaylistGone`, `PermissionError`) travel as themselves. A crashed, unreachable
  or timed-out provider process is `unreachable` or `timeout`. The existing failure model
  therefore holds unchanged: never a 500, retries, cooldowns.
- **Files.** These still cross only through workspaces (ADR-0003). The gateway mounts
  the single workspace directory of the call into the sandbox, so a provider sees
  nothing else on disk.

In the host, `ProviderRegistry` builds proxy objects that implement the existing ABCs on
top of this protocol. Routes, services and workers keep calling providers exactly as
they do today, through the `ProviderRegistry` protocol in `core/interfaces.py`.

### 6. One gateway service runs every provider in its own sandbox

A new compose service, `provider-gateway`, owns provider execution. It:

- pulls images by digest and unpacks their filesystems into a cache keyed by layer
  digest, so the shared base layers are stored once;
- starts **one long-lived sandboxed process per installed provider** from that root
  filesystem (with the runner as entry point), restarts it when it crashes, and exposes
  its socket on a volume shared with `app` and the workers;
- enforces the manifest's permissions:
  - a read-only root;
  - a private `/tmp`;
  - only the call's workspace, plus any `filesystem_from_settings` path, mounted;
  - no host environment and no credentials besides the call's `AuthSession`;
  - network egress filtered at the network level to the declared and configured
    destinations, not left to the provider honouring a proxy variable;
  - rlimits;
- never mounts the Docker socket and never runs privileged.

The sandbox is built with **bubblewrap** (user, mount, PID, IPC and network
namespaces, `no_new_privs`, seccomp). This is the August decision. A provider that
cannot be sandboxed **does not run** (fail closed), and the gateway's health reports
why.

The gateway is the only process that pulls images or starts provider processes.
`app` and the workers never install anything. After an install, update or removal, the
host publishes a "providers changed" event on Redis, and every process (the app and the
three workers) rebuilds its registry from the gateway's manifests. This replaces today's
per-process `reload()`.

### 7. First-party providers live in their own repository

The providers maintained by the project move to a separate repository (proposed:
`sossiogll/musizerr-providers`). A GitHub Actions workflow there:

- tests each provider with the SDK's conformance kit;
- builds the multi-architecture image and pushes it to `ghcr.io/sossiogll/`;
- regenerates the catalog with the new digests.

The host image ships **no provider**. On a fresh installation, the first-party catalog
URL is offered as a suggested source, a value in the setup flow rather than a provider
name in code, and the admin chooses what to install.

Which catalog lists which provider is a publishing decision, outside the host. It is
consistent with the project's posture as a neutral tool: providers whose use conflicts
with the terms of the service they talk to (today the download provider that drives a
streaming library, whose dependencies are already excluded from the production image) are never in the public catalog. They are
kept for personal use in a private source (§8).

### 8. Private sources

Some providers are meant only for the maintainer and a few trusted people, never for
public distribution. They live in a **private** repository (proposed:
`sossiogll/musizerr-providers-private`) whose catalog and GHCR images are private. Only
GitHub accounts granted access to that repository can read them.

To the host, a private source is an ordinary source that carries a **credential**. There
is no "private provider" concept, flag or name in the code.

- **The credential.** When adding a source, the admin may give a GitHub token with read
  access to the catalog's repository and its packages. At the time of writing, GHCR
  pulls of private images need a classic token with `read:packages`. The host stores it
  like provider credentials (encrypted at rest with `CREDENTIALS_KEY`, see
  `docs/credentials.md`), never returns it from the
  API (only whether one is set), and uses it only to fetch that source's catalog and,
  through the gateway, to pull its images.
- **Who has access.** Access is managed on GitHub, not in Musizerr. Each person is granted
  access to the private repository and uses **their own** token in their own instance.
  Removing someone's access on GitHub stops their future installs and updates. A token
  is never shared between people.
- **What a revocation does not do.** Revoking access does not remove an image a person
  has already pulled. Private distribution limits who can install a provider, not what
  happens to a copy someone already has.
- **No suggestions.** The host never suggests or lists a private source. It exists in an
  instance only because its admin added it.
- **Code never reaches a public place.** The code of a private provider never appears in
  a public repository, including the history of this one (see Release gating).

## What is removed

By the end of the migration (§ Phases), the following is gone:

- `app/providers/` and the in-process loader in `services/provider_registry.py`;
- the entry-point discovery and `pip install`/`uninstall` in `services/sources.py`;
- the `pypi` service, `pypi-packages/`, `PYPI_EXTRA_INDEX_URL` and `provider-packages/`;
- the provider-specific requirement files and their Dockerfile steps;
- the four `providers/*.py` re-export modules, `providers/auth_strategies.py`,
  `ConfigField` and its adapter;
- the host route that serves the capture utility of the streaming-library provider, since
  providers never serve HTTP routes on the host (see Open questions for the external
  utility it serves);
- the `installed_providers.package` column, replaced by `image`, `digest`, `manifest`
  and `approved_at`;
- the demo `provider-registry/registry.json`, replaced by catalog version 2.

## Phases

Each phase is its own PR, leaves master working and removes what it obsoletes. In-repo
providers keep running in-process only until phase 5 moves them out.

1. **SDK (contract version 5).**
   - Extract `sdk/` and make the host and the in-repo providers import from it.
   - Make the auth strategies declarative, and remove `ConfigField` and the re-export
     modules.
   - Remove the demo external package (`provider-packages/`, its wheels in
     `pypi-packages/` and its catalog entry). It cannot implement version 5 before the
     SDK is published, and it is retired anyway.
   - `test_layering.py`: providers may import only the SDK and their own dependencies.

   *Done when* no provider imports `core.*` or `providers.*` helpers and both backend
   suites are green.
2. **Protocol and loopback.**
   - Build the manifest, the wire codec, the call protocol and the runner, plus host-side
     proxies.
   - Add a *loopback* transport that runs every in-repo provider through the codec and
     the protocol inside one process.

   *Done when* the whole backend suite passes with every provider behind loopback. That
   proves the protocol before any sandbox exists.
3. **Gateway.**
   - Add the `provider-gateway` service: pull by digest, unpack, run under bubblewrap,
     filter egress, broadcast over Redis.
   - Package LRCLIB as the first image.

   *Done when* lyrics work end to end from a GHCR image and a test provider that tries
   to read `/data`, the environment or an undeclared host is refused.
4. **Sources and catalog version 2.**
   - Rewrite `services/sources.py` and `/sources` for catalogs, digests, permissions and
     source credentials (§8), with an Alembic migration of
     `provider_sources`/`installed_providers`.
   - Rebuild Administration › Sources, showing permissions at install and update.
   - Remove pip, entry points and `pypi`.
5. **First-party providers out of the core.** Create the public and the private providers
   repositories (§8) and their CI, then migrate providers in this order:
   - lyrics, then metadata (the OAuth2 and key-based catalog providers, MusicBrainz, YouTube);
   - identification (AcoustID, with `fpcalc` inside its own image);
   - download (a catalog provider, Lidarr, the streaming-library provider);
   - storage (FTP, then local with its `filesystem_from_settings` permission);
   - sync (Navidrome).

   Provider names do not change, so settings, credentials, identifiers and the library
   need no data migration. Remove `app/providers/` and the provider requirements.
6. **Authors.** Publish a guide, a GitHub template repository (SDK, Dockerfile from the
   base image, Actions workflow publishing to GHCR and updating the catalog) and the
   catalog format.
   *Done:* `docs/provider-authors.md` and the template repository
   `sossiogll/musizerr-provider-template` (an example provider, its tests in the base image,
   `scripts/publish.sh` and the `test` and `publish` workflows; the publish workflow pushes
   for amd64 and arm64 and commits `catalog.json`). Its packages, like the first-party ones,
   stay private until the maintainer makes them public.

## Release gating

Decided on 2026-10-12: the code is published only **after phase 5**, when no provider is
left in the core. Two reasons:

- **Sandbox.** Until phase 4, anything installed from a third-party source would run
  without a sandbox in the host's process.
- **History.** The code of the providers meant for private use (§8) is in this
  repository's history today, and moving it out in phase 5 does not remove it from past
  commits.

Rewriting this repository's history and force-pushing it is not enough, because GitHub
keeps every pull request's commits reachable (`refs/pull/*`) and the owner cannot delete
them. So:

- the current repository is renamed and stays private, with its full history and pull
  requests;
- a new public repository takes the name `sossiogll/musizerr` and starts from a fresh
  history (creating it ends the redirect from the old name);
- `master` and `dev` are both public. `master` is protected: it accepts pull requests
  only from `dev`, and only once the checks pass (both backend suites, the SDK tests,
  the frontend type check and tests, and the check of the source branch). A merge into
  `master` rebuilds and publishes the Musizerr images on GHCR.

## License

Decided on 2026-10-12:

- **Musizerr** is licensed under the GNU AGPL v3 or later, with additional terms under
  section 7(b)/(c) that require keeping a visible attribution to Musizerr (`LICENSE`,
  `NOTICE.md`). The AGPL fits a web application: section 13 makes whoever runs a
  modified version on a network offer its source to its users.
- **The SDK** (`sdk/`) is licensed under the GNU LGPL v3 or later, so a provider built on
  it may use any license its author chooses.
- **"Free of charge" cannot be required.** No term forbids charging for copies. Such a
  term would make the license no longer free software and incompatible with `mutagen`
  (GPL-2.0-or-later), which Musizerr depends on.
- **An "About" page is required.** The application must show the attribution and the
  link to the source on a page reachable from its navigation. This is required before
  publication.

## Consequences

- **Security.** A provider can reach only the network destinations, paths and workspace
  it declared, and the admin saw them. Compromising a provider no longer exposes the
  database, `AUTH_SECRET` or other providers' credentials.
- **Portability.** bubblewrap inside a container needs unprivileged user namespaces.
  Docker's default seccomp profile and some AppArmor configurations (recent Ubuntu)
  restrict them, so the gateway ships its own seccomp profile and the installation docs
  cover the host requirements. On a host where this is impossible, providers do not run
  rather than run unconfined.
- **Cost.**
  - Every provider call becomes an IPC round trip plus serialization. That is small next
    to the upstream HTTP call it wraps, but a hot path that calls a provider many times
    per request (cross-enrichment, availability checks) should be measured in phase 2.
  - Each installed provider is a resident process; first-party images share their base
    layers.
- **Operations.** Provider processes have their own logs, which the gateway labels and
  forwards. A gateway outage makes every provider `unreachable`. Requests then degrade
  as they do today when upstream services are down: 503s and retries, never 500s.
- **Contract.** Version 5 breaks every external package. Today that is only the demo
  package, which is removed.
- **Development.** Working on a provider means rebuilding its image, or using the
  loopback transport with the provider installed as an editable package in the dev
  compose. The existing `registry` service in `docker-compose.dev.yml` becomes a local
  OCI registry for images under test.

## Alternatives considered

- **Keep pip packages in the host process** (today). Rejected: no isolation, dependency
  conflicts with the core, and the packages do not reach the workers.
- **One container per provider, started through the Docker socket.** Rejected: whoever
  holds the socket controls the host, and N containers cost more than N processes. This
  was already rejected in August.
- **Static provider services in `docker-compose`.** This gives real isolation for free,
  but there is no installation from the UI and each install needs the user to edit
  compose. It is kept as a possible escape hatch for hosts where user namespaces are
  unavailable (see Open questions).
- **Harbor.** Superseded by GHCR: there is no registry to operate, anonymous pulls are
  free for public images, and CI publishes from the same place the code lives.
- **GitHub Releases with wheels, or `pip install git+https://…`.** Rejected with GHCR's
  choice: they keep the provider in the host's Python environment and leave isolation
  to a later step.
- **gVisor (`runsc`) or Kata as the sandbox.** Stronger isolation, but it requires the
  user to install a container runtime on the host. That is too high a bar for a
  self-hosted app. bubblewrap could be swapped for one of them later behind the
  gateway, without changing the protocol.

## Open questions

1. **Hosts without user namespaces.** Fail closed only, or also support the static
   compose service fallback? Proposed: fail closed in the first release and document the
   fallback later if users need it.
2. **FTP's data connections.** Passive-mode FTP opens data connections on ports the
   server picks at run time. Egress filtering has to allow the declared host on any port,
   or the FTP provider moves to a library that tunnels FTP differently. To settle in
   phase 5.
3. **The capture utility's credential route.** The utility of the streaming-library
   provider writes credentials to the fixed paths `/api/credentials/*`. Proposed: the provider documents the generic
   `/auth/{provider}/credentials` route and the utility is adapted. The host keeps no
   provider-named route.
4. **ADR-0002 (provider instances).** It is still Proposed. The gateway runs one process
   per provider *type*. If instances are accepted, each call's context snapshot already
   carries the instance's own settings, so the two decisions are compatible.

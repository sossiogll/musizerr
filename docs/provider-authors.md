# Writing a provider

A provider is a package of its own, in its own repository, with its tests and
its image. It imports `musizeer_sdk` (the provider contract, `sdk/`) and nothing
else of Musizeer. Musizeer runs it in a sandbox behind the provider gateway
(`docs/gateway.md`) and offers it to instances through a catalog
(`docs/catalog.md`). The contract itself is `sdk/src/musizeer_sdk/interfaces.py`;
this page is the way through it.

## Start from the template

The repository `sossiogll/musizeer-provider-template` (a GitHub template) holds a
working provider: a metadata provider that searches a public API with the SDK and
the standard library only, its tests, its `Dockerfile`, `scripts/test.sh`,
`scripts/publish.sh`, and two workflows (`test` on every push, `publish` by hand).
Create your repository from it, rename the package, write your provider. Its
README lists the steps; the rest of this page is what the steps mean.

## What a provider is made of

A class that extends one or more roles of the contract and declares what it is:

```python
class MyProvider(MetadataProvider):
    name = "myservice"                       # unique, stable: settings, credentials and identifiers are keyed by it
    label = "My Service"                     # what the UI shows
    provider_api_version = PROVIDER_API_VERSION
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    permissions = Permissions(egress=("api.myservice.example:443",))

    def config_schema(self) -> list[ProviderSetting]: ...
    def validate_config(self, config: dict) -> bool: ...
    def test_connection(self, session: AuthSession) -> str | None: ...
    def search_tracks(self, query, session, limit=20) -> list[Track]: ...
```

- **Roles** are the classes it extends: `MetadataProvider` (a catalog: search,
  lookups, discography, a user's library), `DownloadProvider` (a file for a
  track, or a hand-off to an external system), `LyricsProvider`,
  `StorageProvider` (where the library lives), `SyncProvider` (a media server
  that holds the library and streams it), `IdentificationProvider` (an audio
  fingerprint). One provider may have several: it is filed under each.
- **Methods.** Every optional method has a default that means "not supported"
  (an empty list, `None`). Override the ones your service can answer; the host
  sees which ones you implement and asks only those.
- **Capabilities** are declared, never inferred: `supports_isrc_lookup()`,
  `supports_discography()`, `supports_user_library()`, `streams_full_tracks`,
  `supported_identifier_types`, `max_concurrent_downloads`... If your provider
  can do something, say so with the declaration next to the method.

## What it declares

All of this is data: it is in the provider's manifest and in its image's labels,
the admin sees it before installing, and the gateway enforces it.

- **`auth`**: the flow (`NONE`, `API_KEY`, `CREDENTIALS`, `OAUTH2`,
  `EXTERNAL_UTILITY`) and whose account it is used with, `account_model`:
  `NONE` (no account), `PERSONAL` (each user's own), `PERSONAL_WITH_SHARED`
  (the user's own, else the instance's), `SYSTEM` (the instance's). The host runs
  the flow and stores the credentials; each call gets an `AuthSession` with what
  the user connected. OAuth2 is declarative (`OAuth2Config` and the strategies of
  `musizeer_sdk.auth`): a provider ships no auth code to the host.
- **`permissions`**: the only resources the sandbox will grant it. Nothing by
  default: no network, no folder.
  - `egress`: `"host:port"` destinations, `"*.cdn.example:443"` for any subdomain;
  - `egress_from_settings`: setting keys whose value is a URL or host the user
    types (a media server on the LAN);
  - `tcp_from_settings`: `TcpAccess("host", "any")`, raw TCP to the host a
    setting names (a protocol that is not HTTP; use `musizeer_sdk.net`);
  - `filesystem_from_settings`: `FilesystemAccess("root_dir", "rw")`, a folder a
    setting names, inside what the instance allows;
  - `limits`: `memory_mb` (address space of each process, in which every thread reserves its stack: about 8 MB by default),
    `open_files` (of each process) and `processes` (the processes and threads of the whole sandbox together, counted by the
    gateway: a provider that stays above it is stopped and started again), within the gateway's ceilings. The defaults are
    1024 MB, 1024 files and 512 tasks; a provider needs to declare them only when it needs more or fewer.

  An update that asks for more is shown to the admin as a difference before it
  is applied, and an image whose labels say otherwise than its catalog entry is
  refused.
- **`config_schema()`**: its settings, as `ProviderSetting` (`key`, `type`,
  `label`, `default`, `options`, `secret`, `visible_when`, `required`...). The
  host stores them, places them under the provider in the settings, encrypts the
  secret ones, and gives the current values to each call:
  `self.context.setting(key)`.

## What it gets, what it returns

- **The context** (`self.context`): its settings, the HTTP, connect, download
  and stream-start timeouts to honour, and the public URLs the host serves for its
  covers and streams (`cover_url(id)`, `stream_url(id)`). Nothing else: no
  environment variables, no Musizeer database, no other provider.
- **The network** goes through the sandbox's egress proxy, which the standard
  library and httpx pick up from the environment by themselves. A destination you
  did not declare is refused.
- **Files** cross the boundary through a workspace: for a call that produces or
  reads a file, the host passes an empty directory and the provider returns names
  relative to it, never a path of its own (`docs/adr/0003-provider-file-exchange-workspace.md`).
- **Data, not decisions.** Return what the service says (candidates, a raw
  score, the tracks a package has); matching, thresholds and retries are the
  host's.
- **Failures.** Raise: an HTTP error carrying its status (httpx, requests or
  urllib), a timeout or a connection error is classified next to the provider
  (`musizeer_sdk.failures`) and the host decides (retry later, rest the provider,
  ask to reconnect). `None` means "I don't have it". The exceptions of the
  contract say more: `ProviderRateLimited(retry_after=...)`, `DownloadDeclined`,
  `DownloadDelegated`, `RemotePlaylistGone`, `PermissionError` (reconnect).

## Test

Your tests build the provider with a `StaticProviderContext` (its settings and
timeouts) and replace its service with recorded answers: no network and no
Musizeer. The template's `scripts/test.sh` runs them in the base image, which
already holds the SDK.

### The generic rules

The host holds no provider and cannot check, over all of them, the rules that
hold for every provider. They are checks of the SDK that your own test suite
calls on your provider instance: call `check_provider` in your tests.

```python
from musizeer_sdk.conformance import check_provider
from musizeer_sdk.context import StaticProviderContext

from my_provider import MyProvider


def test_my_provider_follows_the_contract():
    check_provider(MyProvider(StaticProviderContext()))
```

It raises one `ConformanceError` (an `AssertionError`) listing every violation:
`config_schema()` must be a list of well formed `ProviderSetting` (known types,
unique keys, `options` for an enum, defaults of their type, `visible_when`
naming settings you declare), and an OAuth2 provider that declares a capability
that writes on the user's account (`unlike_saved_track`, `propagate_favorites`,
`propagate_playlists`) must map it to its scopes in
`OAuth2Config.capability_scopes`. `check_config_schema` and
`check_oauth2_capability_scopes` run one rule, and the `*_violations` functions
return the list instead of raising.

`musizeer_sdk.manifest.build(provider)` gives the manifest the host will read:
assert the roles, the permissions and the methods you implement.

## The image

```dockerfile
FROM ghcr.io/sossiogll/musizeer-provider-base:5
COPY . /tmp/provider
RUN pip install /tmp/provider && rm -rf /tmp/provider
ENTRYPOINT ["python", "-m", "musizeer_sdk.runner", "--provider", "my_provider:MyProvider"]
```

The base image is Python and the SDK; its tag is the provider API version (the
contract, the SDK's major version). The runner builds your provider and speaks
the call protocol with the host; `--print-labels` prints the
`org.musizeer.provider.*` labels the image must carry (name, roles, contract
version, permissions), which the publishing script puts on it. Set
`org.opencontainers.image.description`, `.url` and `.source` in the
`Dockerfile`: the catalog entry takes its description and homepage from them,
and GHCR links the package to the `.source` repository.

## Publish and distribute

`scripts/publish.sh` (or the `publish` workflow) builds the image for
`linux/amd64` and `linux/arm64`, pushes
`ghcr.io/<owner>/musizeer-provider-<name>:<version>` and writes the entry in
`catalog.json`, pinned by the digest of the image: a catalog version 2
(`docs/catalog.md`). Commit `catalog.json`; its raw URL is the source an admin
adds:

```
https://raw.githubusercontent.com/<owner>/<repository>/<branch>/catalog.json
```

A package GHCR creates is private. Make it public to let anyone install it, or
keep the repository and the package private and give access on GitHub: each
person adds the source with their own token (a classic token with `repo` and
`read:packages`), which Musizeer keeps encrypted and sends only to the catalog's
host and the registry. There is no "private provider" in Musizeer: a private
catalog is a source with a token.

## Versions

`provider_api_version` is the contract a provider implements; the SDK's major
version is the same number, and the base image's tag too. A catalog entry for
another version is listed but cannot be installed, and the gateway refuses an
image that implements another one. Within a version, the SDK only adds: optional
methods with defaults, new declarations, fields with a default. Your image says the
SDK it was built on (the label `org.musizeer.provider.sdk-version`, put by the build),
and Musizeer sends it only the fields that SDK knows: a provider built on 5.5 keeps
working with a Musizeer on 5.9, and the other way round, without a rebuild. A new
major version comes with what a provider has to change.

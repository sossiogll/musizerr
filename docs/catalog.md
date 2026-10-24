# Sources, catalogs and installed providers

ADR-0006, phase 4. A **source** is the URL of a **catalog**: a JSON file that lists provider images
pinned by digest. The database is the only source of truth for what is installed; the
[gateway](gateway.md) runs exactly that and nothing else.

```
 admin ──▶ /sources ──▶ catalog (HTTPS, optional Bearer token) ──▶ the admin sees the permissions
                │
                └── install / update ──▶ installed_providers (image@digest + what was approved)
                                              │ push, after every change and when the gateway restarted
                                              ▼
                                         provider-gateway ──▶ pulls the image by digest (optional token)
```

## The catalog, version 2

```json
{
  "name": "My providers",
  "version": 2,
  "providers": [
    {
      "name": "lrclib",
      "image": "ghcr.io/owner/musizeer-provider-lrclib",
      "digest": "sha256:<64 lowercase hex digits>",
      "provider_api_version": 5,
      "roles": ["lyrics"],
      "label": "LRCLIB",
      "description": "Synced and plain lyrics from lrclib.net.",
      "homepage": "https://lrclib.net",
      "permissions": {
        "egress": ["lrclib.net:443"],
        "egress_from_settings": [],
        "filesystem_from_settings": [],
        "limits": {"memory_mb": null, "open_files": null, "processes": null}
      }
    }
  ]
}
```

| field | |
|---|---|
| `name` | the provider's name, how the host knows it: `[a-z][a-z0-9_-]{0,63}`. Unique in the catalog, and among the installed providers of the instance |
| `image` | `registry/repository`, **no tag and no digest** (`registry.example:5000/team/x` is fine) |
| `digest` | `sha256:` and 64 lowercase hex digits: the image is pulled by digest and by nothing else |
| `provider_api_version` | the provider API version the image implements. An entry for another version than this Musizeer's is **listed but not installable** (`incompatible_provider`) |
| `roles` | a non-empty list of `metadata`, `download`, `lyrics`, `storage`, `sync`, `identification` |
| `label`, `description` | what the admin reads (at most 100 and 1000 characters) |
| `homepage` | optional, an http(s) URL |
| `permissions` | the form of the SDK's `Permissions` (the same JSON the image labels carry): `egress` (fixed `host:port`; the host may be a wildcard `*.example.com`, any subdomain and never `example.com` itself), `egress_from_settings` (setting keys whose value is a host the user types), `tcp_from_settings` (`{setting, ports}`: raw TCP, such as FTP, to the host the setting holds on `"any"` port, one port or a range `"30000-30100"`), `filesystem_from_settings` (`{setting, mode: ro|rw}`: the folder the setting holds, bound into the sandbox), `limits` |

Parsing is **strict** (`core/catalog.py`). A document that is not a version 2 catalog (no
`version`, another version, unknown top-level fields, no `name`, `providers` not a list, more than
500 entries) refuses the whole source, with the reason. An entry with a bad field, a missing
field, an unknown field, a tag in `image`, a digest that is not a digest, an unknown role or
malformed permissions is **refused alone**, with the reason, and listed under `rejected`: the rest of the
catalog is usable. A name listed twice keeps the first.

The images must carry the labels `org.musizeer.provider.*` (see `scripts/build-provider-image.sh`):
the gateway refuses an image whose name, roles, API version or permissions are not exactly what the
admin approved.

## What the admin approves

*Install.* The admin sees the catalog entry (label, description, image and digest, roles) and its
permissions in words: the hosts it can reach (a wildcard as "any subdomain of …"), the settings whose value
becomes reachable, the settings whose host it may open raw TCP connections to ("on any port" or the ports), the
folders, the limits; and, if the source has a token, which registry that token is sent to. Confirming
records `image@digest` and **those permissions** (`installed_providers.permissions`, `approved_at`,
`approved_by`). The request carries the digest that was shown: if the catalog offers another one by the
time it arrives it is refused (`catalog_changed`), so what is recorded is what was on screen.

*Update.* A newer digest in the catalog is an update the admin approves. It is read from the catalog
when the page is read, **not stored**: it cannot go stale and nothing has to refresh it. The admin
sees the difference with what they approved (hosts and wildcards, setting-named hosts, raw-TCP hosts and their ports, folders, limits and roles
added or removed; `grants_more` says whether it asks for more) and approving records the new
digest and permissions.

*The gateway checks again.* It pulls the image, reads its labels before any layer, and refuses it if
they differ from the roles and permissions it was sent. An image cannot gain a permission nobody
approved, and one that asks for less than was approved is refused too (the approval must match what
runs).

## Sources with a credential (private catalogs)

A source may carry a token with read access to a private catalog and to its images (for GitHub, a
classic token with `read:packages` and `repo` read, and a raw URL such as
`https://raw.githubusercontent.com/<owner>/<repo>/<branch>/catalog.json`).

- Stored in `provider_sources.credential`, **encrypted at rest** with `CREDENTIALS_KEY`, like the
  provider credentials (`docs/credentials.md`; a token that cannot be read with the configured key is
  a source without one). Never returned by any route (`has_credential`), never logged, never in an
  error message.
- Fetching the catalog: `Authorization: Bearer <token>` to the catalog's host, **https only**, never
  to the host a redirect leads to.
- Pulling the images: the host sends the token to the gateway with the entries of that source. The
  gateway uses it only to pull, **in memory**, and forgets it as soon as the image is unpacked; it
  never writes it. It goes to the image registry's own host and its token service (same host, or
  under the registry's parent domain) with HTTP Basic `username:token` (`user:token` is sent as it
  is, a bare token with a placeholder user name), never over plain HTTP, never to the CDN a blob
  redirects to.
- The registry an image lives on comes from the catalog. The install dialog shows it, and says that the
  token will be sent there, before the admin confirms.
- `allow_insecure_registry` lets the gateway speak plain HTTP to the registry of **that source's**
  images (a local registry of images under test); a source cannot have it and a token.

## Routes (admin only)

`app/openapi.yaml` is the reference. A refusal is `{"detail": {"error_code", "error_message"}}`.

| | |
|---|---|
| `GET /sources`, `POST /sources`, `PATCH /sources/{id}`, `DELETE /sources/{id}` | list (never a token), add (fetches and validates first), enable/disable or change the token or registry mode, remove (uninstalls its providers) |
| `GET /sources/{id}/catalog` | browse: entries with `compatible`, `installed`, `update` (with the diff) and `rejected` |
| `GET /sources/providers/installed` | what is installed, with `update`/`update_error` (`?updates=false` skips the catalogs) |
| `GET /sources/providers/gateway` | what the gateway runs and why a provider does not (running, pulling, refused, unavailable...) |
| `POST /sources/providers/install`, `POST /sources/providers/{name}/update`, `DELETE /sources/providers/{name}` | install/update (both take the digest that was shown), uninstall |

Codes: `invalid_source_url`, `invalid_credential`, `credential_requires_https`,
`credential_with_insecure_registry`, `catalog_invalid` (422); `source_exists`, `source_disabled`,
`already_installed`, `already_current`, `catalog_changed` (409); `source_not_found`,
`provider_not_in_catalog`, `not_installed` (404); `incompatible_provider` (422);
`catalog_unreachable`, `catalog_unauthorized`, `catalog_not_found` (502).

After any change the host pushes the desired set to the gateway and announces "providers changed" on Redis.

## From the host's shell

```sh
docker compose exec app python -m scripts.provider_sources list
docker compose exec app python -m scripts.provider_sources add-source <url> [--insecure] [--credential-env VAR] [--dry-run]
docker compose exec app python -m scripts.provider_sources edit-source <source> [--insecure|--secure] [--credential-env VAR|--clear-credential] [--enable|--disable]
docker compose exec app python -m scripts.provider_sources catalog <source>
docker compose exec app python -m scripts.provider_sources install <source> <name> [--yes] [--dry-run]
docker compose exec app python -m scripts.provider_sources update <name> [--yes] [--dry-run]
docker compose exec app python -m scripts.provider_sources uninstall <name> [--dry-run]
```

`<source>` is an id, an exact URL or a name. A token is read from the environment variable named by
`--credential-env` (`docker compose exec -e MY_TOKEN app ...`); there is no option that takes it as an
argument. `install` and `update` print the permissions (and the difference) and ask, unless `--yes`.

## Publishing to GHCR

The images of a published catalog live on GHCR, for `linux/amd64` and `linux/arm64`, and the catalog pins each one by the
digest of its **multi-architecture index** (the gateway picks its platform's manifest from it, then checks every object and the
labels as for any image). Two scripts publish them, from a machine logged in to GHCR (`docker login ghcr.io` with a token
that may write packages):

```sh
scripts/publish-provider-base.sh                                          # the base image: :5 (the contract) and :<SDK version>
scripts/publish-provider-image.sh <provider-dir> <catalog-file> [catalog-name]
```

`publish-provider-image.sh` builds the provider on the **published** base image (pulled first), has it print its labels,
builds it with them for both platforms, pushes `ghcr.io/<owner>/musizeer-provider-<name>:<version>` (the version of the
provider's `pyproject.toml`) and writes its entry in `<catalog-file>`, the same way as the development catalog below (the
fields come from the labels). The providers repositories call it for every provider with `scripts/publish.sh`, which keeps
their `catalog.json` at the root of the repository: that file, committed, is what a source points at
(`https://raw.githubusercontent.com/<owner>/<repo>/<branch>/catalog.json`, with a token when the repository is private).

Both build with a buildx builder of their own (`musizeer-publish`, the `docker-container` driver: the default driver cannot
push an image for several platforms), created when it is missing. `GHCR_NAMESPACE` changes where they publish (default
`ghcr.io/sossiogll`). A package GHCR creates is **private**; the scripts never change a visibility. Each Dockerfile sets
`org.opencontainers.image.source` to its repository; the scripts copy it, with the description and the license, into the annotations of the image's index, where GHCR reads them for a multi-platform image and links the package to the repository. `scripts/build-provider-image.sh` and
`scripts/dev-catalog.sh`, for images under test, still refuse to push to ghcr.io.

## Development

The repository lists no provider: the development catalog is generated, under `data/catalogs/` (gitignored, like the rest of
`data/`), and the `registry` service of `docker-compose.dev.yml` serves that folder (`http://registry:9000/registry.json` is
`data/catalogs/registry.json`). The images go to the `oci-registry` service, a local registry bound to the loopback
(`127.0.0.1:${OCI_REGISTRY_PORT:-15000}`), so the source is added with `--insecure` ("allow insecure registry"): the gateway
reaches the registry as `oci-registry:5000` over plain HTTP.

```sh
scripts/dev-catalog.sh [-c NAME] [-r HOST:PORT] <provider-dir>...
```

`<provider-dir>` is a directory with a Dockerfile `FROM` the base image (see `scripts/build-provider-image.sh`); it may be
anywhere on the machine, outside this repository included. For each one the script builds the image (the base image too,
locally), has the provider print its labels, pushes it and writes its entry, **in `data/catalogs/NAME.json`** (default `registry`;
created with `"name": "Musizeer development catalog"`, version 2, if it does not exist):

| field of the entry | comes from |
|---|---|
| `name`, `roles`, `provider_api_version`, `permissions` | the `org.musizeer.provider.*` labels of the image: what the gateway checks, so the entry cannot disagree with the image |
| `label` | `org.musizeer.provider.label` (the provider's own `label`) |
| `description`, `homepage` | the standard `org.opencontainers.image.description` and `.url` labels, when the Dockerfile sets them (else an empty description, no homepage) |
| `image`, `digest` | `oci-registry:5000/musizeer-provider-<name>` (`PROVIDER_INSTALL_REGISTRY` for another name) and the digest the registry returned |

Running it again for the same provider rebuilds the image and **updates its entry in place** (a new digest is an update the
admin approves); the entries of other providers in the file are kept, and a file that is not a version 2 catalog is
refused, not overwritten. The catalog is written by a throwaway `python:3.12-slim` container (`scripts/dev_catalog.py`, standard
library only): nothing runs on the host but Docker. `DEV_CATALOG_DIR` changes the folder.

```sh
scripts/dev-catalog.sh ../my-providers/some-provider ../musizeer-providers/lrclib
docker compose -f docker-compose.dev.yml exec app python -m scripts.provider_sources add-source http://registry:9000/registry.json --insecure
```

# ADR-0007: Versions, compatibility and upgrades

**Status:** Accepted by the maintainer on 2026-10-17 (with the update check on, automatic updates for
providers only, a contract supported until the next MAJOR, the last 5 backups kept).
**Date:** 2026-10-17
**Deciders:** @sossiogll (maintainer)

## Context

Musizerr is now released: the images are published on GHCR (`:latest` on every merge into master,
`:X.Y.Z` on a tag `vX.Y.Z`), the production compose runs them at `MUSIZERR_VERSION`, and providers come
from catalogs pinned by digest. From here on, an instance is upgraded by people who did not write the
change, sometimes across several releases at once, with providers built on other versions of the SDK.
What exists today does not protect them:

- **The version of the app is written by hand.** `version="1.0.0"` in `app/main.py`, `"version": "1.0.0"`
  in `musizerr-ui/package.json`; neither follows the tag, nothing shows which version an instance runs.
- **The provider contract matches exactly or not at all.** `PROVIDER_API_VERSION` (5) must be equal in the
  catalog check (`core/catalog.py`), the registry (`services/provider_registry.py`) and the gateway
  (`gateway/musizerr_gateway/images.py`). Contract 6 would stop every provider at once until each one is
  republished.
- **A "minor" change of the SDK already breaks older providers.** The wire codec (`musizerr_sdk.wire`)
  builds a dataclass with `cls(**fields)`: a field it does not know is a `WireError`. A field added to
  `Track` in SDK 5.5 reaches a provider built on 5.4 (the host sends a `Track` to `get_lyrics`) and the
  call fails; the same holds the other way. An image does not even say which SDK it was built on (its
  labels carry the contract version only).
- **A catalog lists one image per provider.** A provider cannot offer a build for contract 5 and one for
  contract 6 side by side, so moving to a new contract has no overlap.
- **Migrations cannot be relied on to go back.** `init` runs `alembic upgrade head` on start; every migration
  has a `downgrade`, but five of them do nothing or refuse (0016, 0017, 0036, 0040, 0041), and nothing
  says whether the previous release still works on the new schema. The backup before a migration is
  manual. An older app started on a newer database stops with Alembic's "Can't locate revision".

We want upgrades that say what they imply before they happen, a way back for an ordinary release, and
providers that keep working while the contract evolves.

## Decision

### 1. What a version number means

Every versioned thing follows semantic versioning, with these meanings:

| | MAJOR | MINOR | PATCH |
|---|---|---|---|
| **Musizerr** (app, gateway, frontend: one version) | contract migrations (§4), support for a provider contract dropped (§3.2), a required setting or variable added or removed, a manual step | features, expand migrations only, new SDK minor supported | fixes; no migration, no contract change |
| **SDK** (`musizerr-sdk`) | = the provider contract (`PROVIDER_API_VERSION`): a provider has to change | additions a provider may ignore: optional methods with a default, fields with a default, declarations | fixes |
| **A provider** | its author's choice; a new contract major is a provider major by convention | | |
| **Call protocol** (`PROTOCOL_VERSION`) | changes only with a contract major | | |
| **Catalog format** (`version`) | the host reads the current and the previous format | | |

### 2. One version, from the tag

- The git tag is the only place a version is written. The images workflow passes it to the builds as
  `MUSIZERR_VERSION` (`1.4.0` for a tag `v1.4.0`; `1.3.2+7.abc1234` for a merge into master seven commits
  after `v1.3.2`: build metadata, so it ranks as that release and the next one counts as newer) with
  `MUSIZERR_COMMIT`. The development stack is `dev`.
- The app and the gateway images carry it as environment variables; the frontend as a build constant.
  The hand-written `1.0.0` go away.
- `GET /health` reports `version` and `commit` to admins only (like the providers: telling everyone which
  version runs would tell who looks for a vulnerable one where to find it), the gateway's health its own,
  and the FastAPI application the same version (the documentation it serves).
- The About page shows everyone the version of the web app, and admins the server's (with its commit); a
  web app and a server of different versions (a cached page, a server not restarted) are pointed out. The
  provider contracts and the schema revision join it with phases 2 and 3.

### 3. The provider contract

#### 3.1 Additions within a major never break anybody

- **Tolerant decoding.** The codec ignores a field it does not know and gives a missing one its default.
  An unknown *type* (wire tag) stays an error: a new type only travels through a new optional method or
  capability, which the host calls only on a provider that declares it (as today).
- **Every field knows its SDK minor.** A field added to a dataclass of the contract has a default and a
  `since` marker (`5.5`); the codec keeps the table.
- **Encoding for the peer.** A provider image declares the SDK it was built on (a new label,
  `org.musizerr.provider.sdk-version`, also in the manifest). The host encodes what it sends for that
  version, leaving out the fields that came after it; a call tells the runner the host's SDK version, and
  the runner does the same in reply. An image without the label (every one built before this) is treated
  as SDK 5.4, so the images already published keep working without a rebuild.
- **A compatibility test.** CI runs the host against a runner of the oldest supported SDK minor of the
  major, and a current runner against the host's oldest supported peer, built from the SDK's tags.

#### 3.2 Majors overlap

- The host runs **two contract majors**: the current one and the previous one
  (`SUPPORTED_API_VERSIONS`). The catalog check, the registry and the gateway test membership instead of
  equality.
- The previous major is served through an adapter in the host (`services/contract_adapters/`), which
  translates its manifest and its values to the current interfaces. The provider's own runner and SDK are
  in its image: nothing of the old SDK lives in the host but the adapter.
- A provider on the previous major keeps running and is flagged in Administration ("contract 5: supported
  until Musizerr 3.0, update it"); the release notes list what is deprecated. The previous contract is
  supported until the next Musizerr MAJOR and dropped only there, never in a MINOR (decided by the
  maintainer on 2026-10-17).
- **Catalog format 3**: an entry may list several builds, each with its contract version, image, digest,
  permissions and an optional `min_musizerr`. The host installs the highest build it supports; an update
  may move a provider to a newer contract, with the difference in permissions as today. Format 2 stays
  readable.
- While two majors overlap, the base image is published for both (`:5`, `:6`) and the SDK's previous major
  gets fixes on a branch of its own.

### 4. Database: expand, then contract

- **Every migration declares its phase** (`phase = "expand"` or `"contract"` in the module); a test fails on
  one that does not.
- **Expand** (any release): new tables and columns (nullable or with a server default), new indexes,
  backfills that leave the old columns as they were. The previous release works on the result, so going
  back one minor needs no downgrade.
- **Contract** (a MAJOR only): drop, rename, tighten, or transform data the previous code cannot read (an
  encryption like 0041). It is listed in the release notes and marked `irreversible` when it is.
- **The database knows which app can use it.** A table `schema_info` records the version that last migrated
  it and the oldest app version that can use it (raised by contract migrations). On start, `init` (and the
  API and the workers) compare: an app too old for the database refuses to start with the versions and the
  backup to restore; an app older than the database but still compatible starts (that is the rollback).
- **A backup before every migration.** When migrations are pending, `init` dumps the database first
  (PostgreSQL: `pg_dump` in custom format; SQLite: a copy of the file) to `/data/backups/`, keeps the last
  `MIGRATION_BACKUPS_KEEP` (default 5, decided by the maintainer on 2026-10-17) and does not migrate if the backup fails. `MIGRATION_BACKUP=off`
  turns it off for whoever backs up otherwise.
- **Upgrade paths.** A MAJOR declares the oldest version it upgrades from (the last minor of the previous
  major); `init` refuses a database last migrated by an older one and names the version to go through.

### 5. What an operator does

- **Upgrade:** raise `MUSIZERR_VERSION` (or pull `latest`), `docker compose pull && docker compose up -d`.
  `init` backs up, checks the path, migrates; the release notes say beforehand what a MAJOR implies.
- **Go back one minor:** set the previous version and `up -d`: the schema is compatible (expand only).
- **Go back across a contract migration:** `scripts/restore_backup.py <backup>` and the previous version.

### 5.1 Checking for updates; providers update automatically when it is safe

Decided by the maintainer on 2026-10-17: the check for updates is on; **only providers** update
automatically, and only when the new version is compatible with what runs; Musizerr itself (the app, the
workers, the gateway, the web app) is always updated by hand.

- **Every release describes itself.** The release workflow attaches `release.json` to each GitHub release:
  the version, the image digests, the provider contracts it runs, the oldest version it upgrades from,
  whether it has contract migrations, and whether its notes ask for a manual step. The check reads that,
  never the prose of the notes.
- **The check** (admin setting `check_for_updates`, **on** by default) is a routine that reads the
  releases of the repository once a day (the request carries nothing about the instance). Administration
  shows the newer versions of Musizerr with their notes and what applying each one implies, computed from
  `release.json`: a MINOR or a PATCH of the same MAJOR, whose contracts cover every installed provider and
  which has no contract migration and no manual step, is a plain update (§5); anything else is listed with
  the reason it needs care (which provider would stop, which migration removes data, which step to take).
  The admin updates by hand (§5); Musizerr never replaces its own containers, and nothing gets access to
  the Docker socket.
- **Providers update automatically** (admin setting `auto_update_providers`, **on** by default, can be turned
  off) when their catalog offers a new digest that is **compatible**: a contract this Musizerr runs, its
  `min_musizerr` met, and **no difference in permissions** (a new destination, folder or limit always
  waits for the admin's approval, as today). The routine applies it through the same path as an approved
  update (the gateway pulls the pinned digest and restarts that provider) and records it as approved by
  the automatic update (what, from, to, when); the admins are notified. An update that is not compatible
  is only announced and waits for the admin, as today. With the setting off, every update waits.

### 6. Releases

- Every pull request into `dev` carries exactly one label: `breaking`, `feature`, `fix` or `chore`; the
  check `release label` (`.github/workflows/labels.yml`) fails otherwise, and `dev` requires it. A pull
  request whose change needs a step from operators writes it in the "Upgrade notes" section of its
  description (`.github/pull_request_template.md`).
- **Releases are automatic** (decided by the maintainer on 2026-10-17): every merge into master runs the
  images workflow, which reads the labels of the pull requests merged into `dev` since the last tag
  (`breaking` → MAJOR, `feature` → MINOR, `fix` → PATCH; `chore` alone, or nothing, → no release, the
  images only) and, for a release, tags the commit `vX.Y.Z` (the first one `1.0.0`), pushes the images as
  `:X.Y.Z` and creates the GitHub release. Its notes have sections for breaking changes, upgrade notes,
  features, fixes and other changes, the images and the provider contract; a release with upgrade notes
  has `manual_steps` in its `release.json`, so the update check says so.
- **Which images a release builds** (decided by the maintainer on 2026-10-17):
  - **Musizerr** (`musizerr`, `musizerr-frontend`, `musizerr-gateway`) is one product: the three images share one
    version and are all rebuilt on every merge into master, even when only one of them changed. They are tested
    and run together, the compose pins them with one `MUSIZERR_VERSION`, and each rebuild picks up the security
    updates of their base images (Python, nginx, Alpine).
  - **The provider base image** is built only when the SDK version changes, and a published version is never
    pushed again: today every merge rebuilds it and pushes it again under the same `:X.Y.Z`, so one version names
    different contents over time and two providers built "on 5.5.0" a day apart differ. The SDK gets tags of its
    own (`sdk-vX.Y.Z`), which publish the base image `:X` and `:X.Y.Z` once; the images workflow skips it when
    `:X.Y.Z` already exists.
  - **Providers** are published only when their own version is raised (their repositories, `provider version`).
- Providers keep releasing through their catalog (a commit of `catalog.json`); a build may declare
  `min_musizerr`.

## Implementation plan

1. **The version from the tag** (§2): build arguments, `/health`, the About page. Small, and needed by
   every later step.
2. **Additive contract** (§3.1): tolerant codec, `since` markers, the SDK version label, encoding for the
   peer, the compatibility test. SDK 5.5.0. Closes the latent break of minor SDK changes.
   *Done* (2026-10-17): the compatibility test simulates an older peer's strict codec; a run against real
   images built on 5.4 passed. A test against a runner of an older SDK release joins CI once the SDK has
   release tags of its own (§6).
3. **Database guard** (§4): backup in `init`, `schema_info` and the start check, the `phase` of every
   migration and its test (the existing ones are classified once).
   *Interactions (added 2026-10-24):* ADR-0008 (root folders) and ADR-0009 (environment names) are written as
   expand in a MINOR and contract in a MAJOR; their expand steps are safe without this step, their contract steps
   need it.
4. **Releases** (§6): labels and their check, automatic releases with `release.json` (*done*); still to do: SDK
   tags, and the provider base image published once per SDK version, never pushed again under a published tag.
5. **Updates** (§5.1): the check and its page in Administration, and the automatic updates of providers.
6. **Overlapping majors** (§3.2): the contract window, the adapter, catalog format 3. Needed when contract 6
   is planned, not before.

## Alternatives considered

- **No overlap between contract majors.** The simplest: every major stops every provider until it is
  republished. Rejected: third-party providers make that a breaking day for every instance.
- **A schema language for the wire (protobuf, JSON Schema).** Evolution rules for free, but a rewrite of the
  codec and of every type of the contract. Rejected for now: the `since` markers give the same guarantee
  for what the contract needs.
- **Relying on Alembic downgrades for rollbacks.** Rejected: data migrations cannot always be undone (five
  already cannot), and a downgrade run under pressure is the riskiest moment. Expand/contract plus
  backups gives a way back that does not depend on them.
- **Watchtower (or any tool that follows an image tag).** It updates whatever a tag points at, with no idea
  of contracts, migrations or the providers that run: exactly the updates §5.1 must refuse. Rejected; the
  automatic updates apply only what `release.json` and the catalog say is compatible, and only to
  providers.
- **release-please or semantic-release.** Viable; a small workflow of our own over the PR labels keeps the
  rules (labels, upgrade notes, contract support) in one place we control. Can be revisited.

## Consequences

- An upgrade within a major is reversible by changing the version; a MAJOR says what it implies and is
  backed up before it touches the database.
- Adding to the contract becomes routine; changing it costs an adapter and a deprecation period.
- Schema changes that remove things take two releases instead of one.
- The About page and `/health` say exactly what an instance runs, which also helps support.

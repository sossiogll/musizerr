# ADR-0007: Versions, compatibility and upgrades

**Status:** Proposed. Reviewed in part by the maintainer on 2026-10-17 (update check, automatic updates,
support of a contract until the next MAJOR); the rest awaits review.
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
  `MUSIZERR_VERSION` (`1.4.0` for a tag `v1.4.0`; `1.4.0-dev.7+abc1234` for a merge into master, from
  `git describe`) with `MUSIZERR_COMMIT`. The development stack is `dev`.
- The app and the gateway images carry it as environment variables; the frontend as a build constant.
  The hand-written `1.0.0` go away.
- `GET /health` reports `version` and `commit`, the gateway's health its own, and the FastAPI application the
  same version (the documentation it serves).
- The About page shows the version of the server, of the web app and of the gateway, the provider contracts
  this version runs, and the database schema revision. A web app older than its server (a cached page) is
  pointed out, with a reload.

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
  `MIGRATION_BACKUPS_KEEP` (default 5) and does not migrate if the backup fails. `MIGRATION_BACKUP=off`
  turns it off for whoever backs up otherwise.
- **Upgrade paths.** A MAJOR declares the oldest version it upgrades from (the last minor of the previous
  major); `init` refuses a database last migrated by an older one and names the version to go through.

### 5. What an operator does

- **Upgrade:** raise `MUSIZERR_VERSION` (or pull `latest`), `docker compose pull && docker compose up -d`.
  `init` backs up, checks the path, migrates; the release notes say beforehand what a MAJOR implies.
- **Go back one minor:** set the previous version and `up -d`: the schema is compatible (expand only).
- **Go back across a contract migration:** `scripts/restore_backup.py <backup>` and the previous version.

### 5.1 Checking for updates, and updating automatically when it is safe

Decided by the maintainer on 2026-10-17: the check is on, automatic updates can be turned on and off,
and an automatic update happens only when the new version is compatible with what runs; otherwise it is
not possible, and only a manual update remains.

- **Every release describes itself.** The release workflow attaches `release.json` to each GitHub release:
  the version, the image digests, the provider contracts it runs, the oldest version it upgrades from,
  whether it has contract migrations, and whether its notes ask for a manual step. The checks below read
  that, never the prose of the notes.
- **The check** (admin setting `check_for_updates`, **on** by default) is a routine that reads the
  releases of the repository once a day (the request carries nothing about the instance). Administration
  shows the newer versions, their notes and, for each, whether it can be applied automatically and if not
  why.
- **Compatible**, for Musizerr, means all of these hold:
  - the same MAJOR as the running version (a MINOR or a PATCH);
  - every installed provider's contract is among the target's contracts (`release.json`);
  - the database's upgrade path allows it (§4) and the release has no contract migrations;
  - the release asks for no manual step.
- **Compatible**, for a provider whose catalog offers a new digest, means: a contract this Musizerr runs,
  its `min_musizerr` met, and **no difference in permissions** (a new destination, folder or limit always
  waits for the admin's approval, as today).
- **Automatic updates** (admin settings `auto_update_musizerr` and `auto_update_providers`, each on or off)
  apply a compatible update by themselves; an update that is not compatible is only announced, and the
  admin applies it by hand, from the release notes, as in §5. Every automatic update is recorded (what,
  from, to, when) and notified to the admins.
- **Providers** update inside Musizerr, through the same path as an approved update (the gateway pulls the
  pinned digest and restarts that provider), recorded as approved by the automatic update.
- **Musizerr** cannot replace its own containers from inside them. An optional `updater` service (a
  compose profile, off unless enabled) does it: a small container with access to the Docker socket and to
  the compose directory, no published port, separate from the app. The app asks it for a version; the
  updater checks `release.json` again by itself (a compromised app can at most ask for a published,
  compatible release), writes `MUSIZERR_VERSION` in `.env`, pulls the images by digest and runs
  `docker compose up -d`: `init` backs up and migrates as for any upgrade. If the API is not healthy within
  a few minutes, the updater sets the previous version back and starts it again: within a MAJOR the schema
  allows it (expand only). Without the `updater` service the automatic update of Musizerr is not available,
  and the setting says so.

### 6. Releases

- Every pull request into `dev` carries one label: `breaking`, `feature`, `fix` or `chore`; a CI check
  fails without it. A pull request whose change needs a step from operators writes it in an "Upgrade
  notes" section.
- Releasing is a workflow run by hand on master: it computes the next version from the labels merged since
  the last tag (`breaking` → major, `feature` → minor, otherwise patch), tags it, and the images workflow
  publishes the images and the release. The notes have sections for breaking changes, features, fixes,
  upgrade notes (from the pull requests and the contract migrations) and the provider contracts supported.
- The SDK gets tags of its own (`sdk-vX.Y.Z`), which publish the base image `:X` and `:X.Y.Z`, instead of
  republishing it on every merge.
- Providers keep releasing through their catalog (a commit of `catalog.json`); a build may declare
  `min_musizerr`.

## Implementation plan

1. **The version from the tag** (§2): build arguments, `/health`, the About page. Small, and needed by
   every later step.
2. **Additive contract** (§3.1): tolerant codec, `since` markers, the SDK version label, encoding for the
   peer, the compatibility test. SDK 5.5.0. Closes the latent break of minor SDK changes.
3. **Database guard** (§4): backup in `init`, `schema_info` and the start check, the `phase` of every
   migration and its test (the existing ones are classified once).
4. **Releases** (§6): labels and their check, the release workflow with `release.json`, SDK tags.
5. **Updates** (§5.1): the check and its page in Administration; automatic updates of providers; then the
   `updater` service and automatic updates of Musizerr.
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
  `updater` service applies only what `release.json` says is compatible.
- **release-please or semantic-release.** Viable; a small workflow of our own over the PR labels keeps the
  rules (labels, upgrade notes, contract support) in one place we control. Can be revisited.

## Consequences

- An upgrade within a major is reversible by changing the version; a MAJOR says what it implies and is
  backed up before it touches the database.
- Adding to the contract becomes routine; changing it costs an adapter and a deprecation period.
- Schema changes that remove things take two releases instead of one.
- The About page and `/health` say exactly what an instance runs, which also helps support.

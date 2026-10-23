# ADR-0008: Root folders: the library, the download (staging) folder, and everything derived from them

**Status:** Proposed
**Date:** 2026-10-24
**Deciders:** @sossiogll (maintainer)
**Related:** ADR-0003 (workspaces), ADR-0006 (gateway, `filesystem_from_settings`), ADR-0007 (expand/contract
migrations, contract minors), ADR-0002 (provider instances, still Proposed), ADR-0009 (configuration surface and
environment names, the companion of this one)

## Context

### The reference layout: the owner's home server

Two folders side by side in the music area of the server, the one the music server reads:

```
/mnt/hdd2
├── Musica-staging/   DOWNLOAD (staging) folder. Musizerr downloads and works here.
│                     Its working directory is <staging>/.musizerr-work.
│                     Mounted: Musizerr (app, workers, gateway) at /downloads.
│                     Not mounted in Lidarr, not in Navidrome.
└── Musica/           the official LIBRARY, every artist. The storage provider writes the final
                      files here with the naming template.
                      Mounted: Musizerr (gateway) at /library, Lidarr at /music (its root folder, rw),
                      Navidrome at /music (read-only). Navidrome reads ONLY this folder.
```

The same facts are written today in these places, and every one is set by hand and has to agree with the others:

| Fact | Where it is written today |
|---|---|
| Staging is `/downloads` | the compose volume `downloads:/downloads` on `init`, `app`, the three workers, `beat` and the gateway (`docker-compose.yaml:100,155,200,253`); `WORKSPACE_DIR` (`docker-compose.yaml:32-33`), which becomes the default of the **setting** `workspace_dir` (`app/core/settings_catalog.py:461-466,649`) and, separately, the gateway's `GATEWAY_WORKSPACE_DIR` (`docker-compose.yaml:161`, `gateway/musizerr_gateway/config.py:38,81`) |
| The working folder is `/downloads/.musizerr-work` | the same three; plus the hard-wired name `.musizerr-work` inside `DEFAULT_WORKSPACE_DIR` (`settings_catalog.py:461`) |
| The gateway may grant `/downloads` and `/library` | `GATEWAY_FILESYSTEM_ROOTS` (`docker-compose.yaml:159`, `config.py:85`), a **fourth** copy |
| The storage provider writes to `/library` | the provider's own setting `root_dir` (`musizerr-providers/local/.../__init__.py:17,33`, default `/downloads`) and the setting `storage_provider` (`settings_catalog.py:631`) |
| Lidarr's `/music` is Musizerr's `/library` | nowhere. Lidarr's root folder is its own configuration; the Lidarr provider takes the first one it finds (`musizerr-providers/lidarr/.../__init__.py:249-253,280-297`) |
| Navidrome reads the library (and is not given staging) | the full-sync verification, which writes a probe through the storage provider and looks for it on the sync provider (`POST /config/verify-full-sync`); it is not tied to any declared folder, and staging not being visible to the music server is a property of the mounts that nothing states |

### Problems this produces

1. **Copies drift.** `workspace_dir` is both an environment default and a setting, and "a value saved in the settings wins"
   (`settings_catalog.py:457-460`). The setting and the gateway's variable drifted apart on the server and the full-sync
   check broke. Pull request #50 added one `WORKSPACE_DIR` for the environment side and a log line when the host's
   value differs from the gateway's (`app/services/provider_events.py:193-210`, which only logs). The cause is still
   there: two copies of one fact, compared after the fact.
2. **No distinction between the library and the download folder.** Both default to `/downloads`: local storage's
   `root_dir` default is `/downloads` and the workspace is `/downloads/.musizerr-work` (the library holds the
   workspace; the gateway hides it from the sandbox with an empty `tmpfs`, `filesystem.py:116-118`). Nothing tells the
   operator which folder the music server must read.
3. **The delegate's folder is unknown to the host.** A delegating download provider hands the job over and the delegate
   imports into its own root folder with its own naming. The host never learns where: `list_delegated_files` and
   `request_rescan` exist in the SDK (`sdk/src/musizerr_sdk/interfaces.py:418,428`) and no code of `app/` calls
   them. A delegated file reaches the library through the storage scan (`app/services/library_scan.py`) or the sync
   index (`app/services/sync_index.py`), and `db/library_store.py` counts a download "landed without a path" as in the
   library without checking anything. It works only if the delegate's root, the storage root and the music server's folder are
   the same directory; nothing states or checks it.
4. **The full-sync verification is not attached to a declared library.** `verify_full_sync`
   (`app/workers/maintenance.py:58-123`, `POST /config/verify-full-sync`) is the right check and stays: it writes a probe
   file through the storage provider and waits for the sync provider to find it, and on success sets the sync provider's
   `mode` to `full`. It validates, without saying so, that the storage root and the music server's folder are the same
   directory. With roots it has something to be about: the declared library root. (That the music server must not be
   given the download folder is a property of the mounts, a documentation point of §8, not a second check.)
5. **How a file leaves staging is not specified.** Local storage `store()` is `shutil.move`
   (`musizerr-providers/local/.../__init__.py:71`) and `stream()` tries `os.link` (`:83`). ADR-0003 says the move is an
   O(1) rename "when the workspace shares the library's volume". Two caveats, **not verified at run time** (to measure in
   phase 0): `rename(2)` and `link(2)` fail with `EXDEV` across *mount points*, even when both are the same filesystem, and
   bubblewrap binds the workspace (`/workspace`) and the library as separate mounts in the sandbox (`sandbox.py:181`). So
   the rename may never happen for a sandboxed provider, and the owner's two sibling folders are two bind mounts in any case.
   The fallback is a copy followed by an unlink, with no verification and no atomic appearance of the final file (a
   scanner can read a half-copied file).
6. **The host does not need the library.** No host code opens a library path (`grep` of `app/`: the library is reached
   through `StorageProvider` methods and files travel as workspace copies, ADR-0003). Only the staging folder has to be
   mounted in the API and the workers; the library has to be mounted in the gateway. The server report suggested mounting
   the library everywhere; that is not needed, and a smaller mount surface is better.

## Decision

### 1. Three levels, and who owns each

| Level | What | Owned by | Lives in |
|---|---|---|---|
| **Mount** | a host folder attached to a container (`/mnt/hdd2/Musica` → `/library`) | whoever runs the containers | `docker-compose.yaml` / the override file |
| **Root folder** | a folder Musizerr is told to use, with a **kind** and a role (below) | the administrator | the database, edited in the UI |
| **Derived folder** | the working folder, the held uploads, the folders a provider is granted, a provider's folder setting | nobody: computed from roots | not stored |

A mount is Docker's business and is not configured in Musizerr. A root is declared **once**, in one place, and
everything else is computed from it. No setting names a path that a root already names (see §3). Boundary rule
for the environment versus the database: ADR-0009.

### 2. Root folders are data

A root folder is a row (`root_folders`):

| Field | Meaning |
|---|---|
| `id` | stable identifier. Nothing is keyed by path or by provider name (see §10) |
| `kind` | `library` or `staging`. Closed set; a new kind is a new ADR |
| `label` | what the UI shows ("Library", "Downloads") |
| `path` | the folder **as the containers see it**, absolute and normalized. For a library root served by a storage provider that is not a local folder (a remote server), the path **in that provider's namespace** (a remote base path) |
| `storage_provider` | library roots only: the storage provider that reads and writes it |
| `is_default` | one default per kind |

Rules:

- **Staging root.** A local folder, mounted in the API, the workers and the gateway. Exactly one in this release.
  Its **working folder is `<path>/.musizerr-work`**, a fixed name that is code, not a setting. Held uploads stay
  `<working folder>/held-uploads` and workspaces `<working folder>/<provider>/<random>` (ADR-0003, unchanged).
- **Library root.** The place the finished music lives and the place every consumer reads. Exactly one in this
  release, with its `storage_provider` and the naming template (`track_format`, which becomes a field of the library
  root in phase 2). The data model allows several; the validation allows one, because everything above the storage
  (the `library_entries` namespace, coverage, the one `store()` target, a provider setting that has one value) assumes a
  single library. §10 says what unlocks several.
- **No overlap, with one exception.** Roots may not equal or contain one another. The exception is the shape every
  existing install has (staging `/downloads` is also the library root `/downloads`, the working folder hidden inside the
  library): it is accepted for a migrated install, flagged "staging is inside the library" in the checks, and not
  offered to a new one.
- **Declared roots are validated by the gateway**, which is the only process that sees every mount (§9): the path
  exists, is a directory, is writable by the sandbox user, is on a mounted volume and not on the container's own filesystem, and does not
  overlap the gateway's own folders. A root that fails is saved with its reason and is not used.
- **Where the roots come from on a new install.** The shipped compose file mounts the conventional targets `/downloads`
  (staging) and `/library` (library). On the first start with no root, the host creates the staging root for
  `/downloads` when the gateway reports it as a mount, and no library root until the administrator picks a storage provider
  in the first-run steps (its folder offered from the mounts). These two conventional paths are a seed, not a
  setting: once the rows exist, the environment is not consulted.

### 3. What is derived, and what stops being a setting

| Derived | From | Replaces |
|---|---|---|
| the working folder `<staging>/.musizerr-work` | the staging root | the setting `workspace_dir`, `WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` |
| the folders the gateway may grant | the declared roots (the host pushes them, §9) | `GATEWAY_FILESYSTEM_ROOTS` as a required list |
| the storage provider's folder (`root_dir` of the local provider) | the library root, projected into the provider's own setting (§4) | the hand-typed `provider.<name>.root_dir` |
| the active storage provider (`storage_provider`) | the default library root | the global setting (kept as a mirror until the next MAJOR) |
| the host's check that host and gateway agree | not needed: the host **tells** the gateway | `GatewayWatcher._check_workspace` stays as a safety net only |

The owner's server then reads like this: **declared** (once, in Administration › Root folders): staging `/downloads`
("Downloads"), library `/library` ("Library", storage provider `local`). **Derived:** working folder
`/downloads/.musizerr-work`; the gateway grants `/downloads` (the workspace subtree of each provider) and `/library` (to
the storage provider, read-write); local storage's `root_dir` is `/library`; the storage provider is `local`.

### 4. The storage provider's target is the library root

The provider keeps its contract: it reads its folder from its own setting, and its `filesystem_from_settings`
permission is how the sandbox gets the folder. What changes is who writes the setting.

- **SDK 5.6, an optional addition** (ADR-0007 §3.1: a field with a default and `wire.SINCE: "5.6"`):
  `ProviderSetting.root_for: str | None = None`. `root_for="library"` means "the host sets this from the library
  root". The local provider marks `root_dir`; the FTP provider marks `remote_base_path`. The host writes the value into
  the provider's settings when the root changes and the provider's card shows the setting as "set by Root folders". The
  gateway then judges the path as it does today (`validate_grant`), now against the declared roots.
- **A provider that does not mark a setting** (any image built before 5.6) keeps working: the host cannot write its
  setting, so it **compares** the library root with the setting the provider's `filesystem_from_settings` names and shows a
  mismatch as a warning in the checks. The same drift detector, for old images.
- A library root has the storage provider's **own validation** too: `test_connection` runs against the root, and a root
  whose storage provider is not installed or not running is "waiting for its storage provider".

A storage that is not a local folder (FTP, a future object store) fits the same row: the root's `path` is what that
provider means by its base, the staging root is still a local folder, and the move is a `store()` from the working
folder to the remote (always a copy, §6).

### 5. Direct download providers and delegating providers

| | Direct | Delegating |
|---|---|---|
| Who downloads | the provider process, in its sandbox | an external system (a library manager with its own queue) |
| Where bytes first land | the workspace, inside the **staging** root | the delegate's own download client folder; not Musizerr's, not a root |
| Who names the final file | Musizerr (`track_format`, `services/library_paths.py:19`) | the delegate (its own naming) |
| Who puts it in the library | the library root's storage provider (`store`) | the delegate, into **its** root folder; Musizerr never moves it |
| How Musizerr learns where the file is | it chose the path (`LibraryEntry`, `source=download`) | the **mapping** of §7, then a listing |
| What must be declared | a staging root and a library root with a storage provider | a mapping from the delegate's root folder to the library root |

The two coexist on one library root: a direct download writes `{artist}/{album}/...`, a delegate writes with its own
format. Two namings in one tree can duplicate an album on disk; that is a known limitation until the naming of the library root can be read from the
provider that writes into it (§12).

### 6. How a direct download gets from staging into the library

The rule the host enforces, for any storage provider:

1. The provider writes the file in the working folder. The host verifies it (as today) and decides the final relative path.
2. The host calls `store(source, relative_dest)`. **The contract rule** (a documented `MUST` on `StorageProvider.store`,
   in the SDK docs, no signature change): the file appears at its final path **atomically** or not at all; a reader of the
   library never sees a partial file.
3. The host **verifies** after `store` returns: `StorageProvider.size(relative_dest)` equals the size of the source in
   the working folder. Only then the download is `landed` and the workspace removed. A mismatch is a transient failure
   (the file is kept as a held upload, as today for a failed store).

How a provider achieves step 2 depends on the pair of roots, and the root checks report which one applies:

- **One atomic move**: the two roots are the same filesystem **and** the moving process sees them as one mount, so the
  provider can `rename(2)` the file. Nothing is copied, a scanner sees the file appear whole.
- **Copy and verify**: otherwise. The provider copies to a hidden temporary name *in the final directory*
  (`.<name>.part`, not an audio extension, ignored by scanners), flushes it, compares the size, then renames it into
  place (atomic inside one directory), then the source is removed by the host with the workspace. Appearance is still
  atomic; only the cost is a copy.

The owner's layout (two sibling folders, two bind mounts) is **copy and verify**, and so is any layout through the
sandbox until the gateway can present staging and library to a provider as one mount (phase 7, optional, measure first).
This ADR does not promise an instant move; it promises a safe one, and states which one the install gets. The UI says
"Files are copied from Downloads to Library and checked" or "Files are moved". The recommendation in the deployment guide
changes accordingly: keep the two folders on one *disk* for speed, do not rely on one *mount* for correctness.

### 7. The delegate's mapping: Lidarr's `/music` is Musizerr's `/library`

A delegate imports into a folder it names in its own namespace. The host stores that fact as a **mapping**
(`root_mappings`): `(provider, remote_path) → library root`, with a role and a verification:

| Field | Meaning |
|---|---|
| `provider` | the provider name (text; becomes an instance id if ADR-0002 option C is taken, §10) |
| `remote_path` | the folder in the provider's namespace (`/music`) |
| `root_id` | the library root it is (`Library`, `/library`) |
| `role` | `writer` (a delegate that imports into it) or `reader` (a sync provider that reads it) |
| `verified_at`, `verified_result` | when and how it was checked |

Path translation is lexical and on path boundaries: a path `p` of the delegate is under the root when
`p == remote_path` or `p` starts with `remote_path + "/"`; its relative path in the library is `p` minus that prefix,
which is also the key of `LibraryEntry.file_path`. `/music/Artist/Album/01 - x.flac` is `Artist/Album/01 - x.flac` in the
root `/library`. The storage provider's `exists(relative)` is the evidence that the file is there.

Two optional methods on `DownloadProvider` (SDK 5.6, data only, declared with `supports_*` queries so the host asks
only a provider that has them; no provider is required to change):

- `list_import_roots(session) -> list[str]`: the folders the delegate imports into, in its namespace. The Lidarr
  example: its root folders. This is what the administrator maps, and what the host compares against: a delegate
  that imports into a root nobody mapped is reported ("Lidarr imports into `/music`, which is not mapped to a library root").
- `locate_in_import_roots(name, session) -> str | None`: where the delegate sees a folder of this name inside its import roots. Used by the
  **verification**.

**Verification of a mapping** (an action in the UI, and part of the root checks), generic and without any typing:

1. the host stores a small marker file through the storage provider in `Musizerr Probe <random>/probe.ogg` under the library root;
2. it asks the delegate where it sees `Musizerr Probe <random>` (`locate_in_import_roots`); the answer, `/music/Musizerr Probe <random>`, gives `remote_path = /music`
   by removing the relative part;
3. it removes the marker. A delegate that does not implement the method is verified by suffix instead: a file the
   delegate reports for a delegated track (`list_delegated_files`, `FileRef.path`) must end with a relative path that the
   storage lists.

Once a mapping exists, completing a delegated download stops being invisible. The host calls `list_delegated_files`
for the handle, translates each path, and records a `LibraryEntry` (`source=delegate`) for every file the storage confirms. The sync
index and the scan remain as the safety net they are today. A path outside every mapping is shown to the administrator as
"imported outside the library you declared", not ignored.

### 8. The music server reads the library: the existing full-sync verification

No new mechanism. The existing full-sync verification (`POST /config/verify-full-sync` and its task `verify_full_sync`: a
probe file is written through the storage provider, the sync provider is asked to scan, and the probe is looked for on
it) is the check that **the music server sees the library**. With declared roots it gets a defined subject:

- it validates the **declared library root** against the sync provider: the probe goes through the storage provider of
  the default library root, so it lands in that root, and finding it on the sync provider is the evidence that the music
  server reads the same folder (the owner's Navidrome, `/music` read-only, which is `/mnt/hdd2/Musica`, Musizerr's
  `/library`);
- its result is reported on the library root in the checks (§11) as "music server sees this library: verified at
  <time>", and the `mode` of the sync provider keeps being written by it as today (until it stops being a setting in
  the next MAJOR);
- the endpoint, the task and the timeouts (`full_sync_probe_timeout_seconds`, `full_sync_probe_interval_seconds`) do not change.
  The only addition is that the probe is told which library root it runs against and the result is stored on it, which
  matters once there are several.

**Staging should stay invisible to the music server.** The music server must be given the library folder and not
the download folder: otherwise it indexes half-downloaded files that are still in the working folder. In the reference
layout this holds by construction (the two folders are siblings and only `Musica` is mounted in Navidrome). It is a
documented requirement in the deployment guide and in the page's help text, not something Musizerr checks. Likewise
Musizerr cannot see that the music server mounts the library **read-only**; the guide recommends `:ro`.

### 9. The gateway follows the roots

The environment no longer carries a list of folders. The host **pushes** the declared roots to the gateway with
the desired set of providers (`{"op": "install"}`, and on every change of a root, as it already does for providers,
`services/gateway_sync`), and the gateway:

- accepts a root if it passes the checks of §2 (the existing `check_root` / `validate_grant` rules in
  `gateway/musizerr_gateway/filesystem.py` apply to each) **and** if it lies on a *mounted volume*: the longest matching
  entry of the gateway's own `/proc/self/mountinfo` for the resolved path is not the container's root filesystem. A root cannot be
  `/etc` or any other part of the image;
- derives from the pushed roots the workspace directory (`<staging>/.musizerr-work`, the folder it mounts per provider
  as `/workspace`, `supervisor.py:272`) and the roots against which `validate_grant` judges a provider's folder. Every
  folder a provider is given is therefore **inside a declared root**;
- restarts a provider whose binds changed (existing behaviour of a change of grants);
- reports what it accepted and refused per root in `list` (`roots: [{id, path, state, reason, device, mount_id, writable,
  free_bytes}]`, next to the `workspace_root` it reports now), and answers two new read-only operations for the UI:
  `inspect` (one path: exists, directory, writable, device, mount, free space; the checks of §2) and `browse` (the
  subdirectories of a path inside a mounted volume, never files, never outside a volume, symlinks resolved and
  refused when they lead out; this is the folder picker of Lidarr's "Add root folder");
- keeps `MUSIZEER_GATEWAY_FILESYSTEM_ROOTS` (ADR-0009; the legacy name `GATEWAY_FILESYSTEM_ROOTS`) as an **optional ceiling**
  for an operator who wants the old fail-closed posture: when set, a root outside it is refused. Unset, the ceiling is
  "the mounted volumes". The security argument: a provider never chooses a folder; only an administrator declares a
  root, and what can be declared is bounded by what Docker mounted into the gateway.

Before the host has pushed anything the gateway runs no provider (unchanged), so there is no window in which it has a
workspace the host does not know. `GATEWAY_WORKSPACE_DIR` is read only as the fallback of a gateway that is older than
the host that talks to it.

### 10. Relation to ADR-0002 (provider type vs instance)

ADR-0008 does **not** need ADR-0002 and is written not to prejudice it:

- A root is keyed by its own `id`, never by `(provider, path)`. A library root holds a `storage_provider` (a name today).
  Under ADR-0002 option C that column becomes `storage_instance_id` by a rename in a contract migration; no root is
  re-keyed.
- `root_mappings.provider` has the same fate.
- **One library root in this release is the consequence of ADR-0002 being open.** Two library roots served by the same
  provider *type* (two local folders) need either instances or a per-call root in the contract
  (`store(source, relative_dest, session, root=...)`), because the folder lives in a provider setting that has one value
  per type. The model leaves the door open (several rows); lifting the validation is a decision for after ADR-0002.
- Option D (per-role credentials) does not interact.

### 11. What each part of the reference layout is checked by

"Declared once and checked": the page *Root folders* (§UI) shows one report; its lines are the checks above.

| Check | Done by | Reference layout |
|---|---|---|
| staging exists, writable, mounted in the API, the workers and the gateway | host and gateway (`inspect`) | `/downloads` in all three: ok |
| the working folder exists and is the gateway's | host asks `list` | `/downloads/.musizerr-work`: ok |
| library exists, writable by the storage provider, mounted in the gateway | gateway `inspect` + `test_connection` | `/library`: ok |
| the library is not required in the API | informational | "not mounted in the API: fine" |
| staging and library: same disk? same mount? transfer mode | gateway (`device`, `mount_id`) | same disk, two mounts: **copy and verify** |
| the storage provider's folder is the library root | derived, or compared for an old image | `local.root_dir` = `/library`: ok |
| the delegate's import root is mapped | `list_import_roots` + mapping + verification | `/music` ↔ `/library`: ok / "not mapped" |
| the music server sees the library | the existing full-sync verification, run against the library root | verified / not yet verified |
| the music server is not given the download folder | documentation (§8), not checked | siblings, only `Musica` mounted in Navidrome |
| a delegate that imports where nobody mapped | `list_import_roots` | none |

### 12. Room left for what comes after (not decided here)

Both are SDK minors (ADR-0007 §3.1: optional methods with a default, a declared query, new types with `wire.SINCE`),
and both consume what this ADR creates:

- **`LibraryLayout`** and `get_library_layout(session) -> LibraryLayout | None`: a provider that has a naming configuration
  (Lidarr's `standardTrackFormat`, `artistFolderFormat`, `rootfolder`) returns it already translated into the host's
  template vocabulary, with a `lossless_translation` flag. The library root is where it lands: the root's `track_format`
  gets a source (`own`, or `follow:<provider>`), and `root_folders` of the layout are the candidates of §7. Without
  the root and its mapping there is nothing to attach it to.
- **`resolve_import_block(handle, action, session)`**: a delegate whose import is blocked (the destination folder already holds
  the album, "not an upgrade") reports the block with `FileRef`s in **its** namespace. The mapping of §7 translates them into library
  paths, so the host can list what is already there (storage listing) and offer `import_replace` / `import_keep_both` /
  `skip`; today a `CONFLICT` without conflicts data ends by timeout.

## Migration from today's settings (ADR-0007 §4: expand, then contract)

**Expand, in a MINOR; the previous release must keep working on the result.**

- New tables `root_folders`, `root_mappings`. No existing column changes. Every migration declares `phase = "expand"`
  (ADR-0007 §4).
- A data step (in `init`, once, idempotent) creates the rows from the settings the install has:
  - **staging**: `workspace_dir`. If its last component is `.musizerr-work`, the staging root is its parent;
    otherwise (for instance the value `/downloads` an install had before #50) the staging root is that value and the
    working folder becomes `<value>/.musizerr-work`; in-flight workspaces are ephemeral and held uploads of
    the old location are dropped (the download is repeated: a listed consequence in the upgrade notes).
  - **library**: the value of the setting `storage_provider` and the storage provider's folder setting (`provider.<name>.root_dir`
    for the local provider, `remote_base_path` for the FTP one: the migration reads the provider's *declared*
    `root_for` setting from the registry when it can, and falls back to the settings of the provider named by
    `storage_provider`; a migration cannot name a provider, so it asks the provider's manifest through the installed
    set, and an install it cannot resolve gets the row at the first start of the API); the root's `track_format` is the current `track_format`.
  - **mapping**: none (nothing records a delegate's folder). The first-run step and the checks propose them.
- **Dual write while the window is open.** A change of a root also writes the legacy `workspace_dir`, the provider's
  folder setting and `storage_provider`, so that going back one minor (ADR-0007 §5) finds the values the old code reads.
  The gateway keeps reading `GATEWAY_WORKSPACE_DIR` / `GATEWAY_FILESYSTEM_ROOTS` if they are set, and the old compose
  file keeps working with the new image.
- A manual database backup is advised in the upgrade notes until ADR-0007 step 3 (backup in `init`, `schema_info`)
  exists. The expand migration does not need that step to be safe; **the contract step does**.

**Contract, in the next MAJOR (decided with ADR-0009's removal of legacy names):** remove the setting `workspace_dir`,
`WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR`, the dual writes, and make the provider folder settings that carry
`root_for` read-only in the settings catalog; the global `storage_provider` becomes a read-only mirror, then goes. Before it,
ADR-0007 step 3 (database guard) must be done.

**The owner's server**, migrated: `workspace_dir=/downloads/.musizerr-work` (via `WORKSPACE_DIR`, #50) gives staging
`/downloads`; `storage_provider=local`, `provider.local.root_dir=/library` gives the library root `/library`. The
checks then ask for the mapping `/music` ↔ `/library` (the Lidarr provider lists `/music`) and run the existing full-sync
verification against the library root for Navidrome. The compose file loses `WORKSPACE_DIR` and `GATEWAY_FILESYSTEM_ROOTS=/downloads:/library`; the
`/library` mount remains only in the gateway.

## UI

Administration › **Root folders** (admin only), modelled on Lidarr's Settings › Media Management:

- A table of roots: label, kind, path, storage provider, free space, status (ok / waiting / refused with the reason), the
  transfer mode between staging and library, and a **Check** button that runs the report of §11.
- **Add root folder**: kind, then for a library root the storage provider, then the folder picker (`browse`) limited to
  mounted volumes; the label; the naming template for a library root.
- A second table, **Other tools** (the mappings): one row per delegate or music server: "Lidarr imports into `/music`",
  mapped to the Library root, with **Verify**; "Navidrome reads the Library", with the result of the full-sync verification (and a one-line reminder not to give the music server the Downloads folder).
- First-run checklist (Administration › Providers): "Choose where your music lives" opens the same dialog.
- Provider cards hide the settings marked `root_for` ("set by Root folders"); Settings › Library › Files loses the
  working-folder field and shows the working folder, read-only, under the staging root.

Strings are keys in the dictionaries; no provider name is in the page (the lists come from `/providers` and the
declared capabilities).

## Implementation plan

Each step is a pull request into `dev`, expand-only until the last.

0. **Measure** (no product code): on the owner's layout and on a nested layout, time `store` and `stream` through the sandbox and
   record whether `rename`/`link` return `EXDEV`. The result goes in this ADR's open items and decides the weight of step 7.
1. **Gateway: roots, mount detection and `inspect`/`browse`** (gateway only; `docs/gateway.md`): `roots` pushed in `install`, the
   mountinfo rule, the derived workspace, `list.roots`, the optional ceiling, tests with the existing sandbox integration
   script. The host does not push yet.
2. **Host data and checks**: `root_folders`, `root_mappings`, the data step, `services/roots.py` (validation, derivations, dual write),
   `GET/POST/PATCH/DELETE /roots`, `app/openapi.yaml`; the host pushes roots; `workspace_dir` stops being read (the staging
   root is); `held_uploads`, `workspace`, `library_scan` take the working folder from the root.
3. **UI: Root folders** page, first-run step, settings changes; e2e spec.
4. **SDK 5.6 + providers**: `root_for` on `ProviderSetting`, wire `SINCE`, conformance rule (one `root_for` per kind), the `store` atomicity
   rule in the docs and a size check in the host after `store` (§6); the local and FTP providers mark their settings
   (providers repository, catalog entry); the host projects the root into the setting.
5. **Delegate mapping**: `list_import_roots`, `locate_in_import_roots`, mapping UI, verification, registering delegated files by
   `list_delegated_files` + mapping; the "imported outside the library" notice. The Lidarr provider implements both.
6. **Full-sync verification on the root**: the existing task is told which library root it runs against and stores its
   result on it; the check on the page. No new probe.
7. **Optional: a single-mount fast path**: only if step 0 shows the copy matters. The gateway presents the staging and the
   library to a storage provider as one mount (for the nested layout, the working folder inside the library bind; for siblings
   there is no safe way without exposing their parent, so they stay copy and verify).
8. **Contract (next MAJOR)**: the removals of "Migration", after ADR-0007 step 3.

Later ADRs: `LibraryLayout`/naming (§12), import blocks (§12), several library roots (after ADR-0002).

## Alternatives considered

- **Keep `GATEWAY_FILESYSTEM_ROOTS` as the required allow-list and add the roots on top.** The safest default, and what
  ADR-0006 chose, but it is one more copy of the library path that the operator must keep equal to the root the admin
  declares: the class of bug this ADR exists to remove. Kept as an optional ceiling instead; the question is asked below.
- **Roots in the environment only (compose).** One place, but changing a folder needs a restart and an edit on the
  server; no folder picker, no per-root validation in the UI, no mapping table. The administrator who can use the UI
  cannot use a compose file.
- **Roots in the UI only, without the gateway verifying mounts.** An administrator could declare any path of the
  gateway's image. Rejected: the mount rule (§9) is cheap and bounds the damage.
- **A separate "completed downloads" folder (Lidarr's download client folder).** Lidarr needs one because its
  download client is another program. Musizerr's downloads are its own: the working folder is that folder. A
  delegate's client folder belongs to the delegate and is not a root.
- **Let the host move the file into the library.** It would make the atomic rename possible in the host process, but gives the host
  (and the workers) write access to the library and takes the folder-granting model of ADR-0003/0006 apart. Rejected.
- **Derive the staging root from the library (`<library>/.musizerr-work`) and have one root.** It is the legacy shape and
  what made the library and the download folder indistinguishable. Kept only as the accepted legacy shape.
- **Make ADR-0002 option C a prerequisite.** Delays the work the owner needs for a decision that nobody has asked for.

## Consequences

- The facts the owner had to keep equal in six places are declared once; the gateway and the storage provider follow
  by construction; the drift between the setting and the gateway cannot happen, and the log line that reports it
  becomes a safety net.
- The page of checks says, per declared root, whether it is usable, whether the music server reads the library (the existing
  full-sync verification, now attached to the library root) and whether the delegate writes to the same library (the
  mapping and its verification). The layout of the owner's server is expressible and verifiable.
- The API and the workers no longer need the library mounted; the gateway needs it.
- The final step into the library is verified (size) and appears atomically. On the owner's layout it is a copy.
- The gateway gains control operations (`inspect`, `browse`) and a mount rule; `docs/gateway.md` and its tests grow.
- One library root per install until ADR-0002 is settled; several are a later decision, not a rewrite.
- The expand/contract window keeps the legacy names alive for one MAJOR.
- The operator's allow-list becomes optional: the default is "what Docker mounted", an administrator can declare any
  mounted volume. See the first question.

## Questions for the maintainer

1. **Gateway allow-list.** Derive the allowed folders from the declared roots bounded by the mounted volumes, with the
   env list as an optional ceiling (this ADR), or keep the env list required? *Recommended:* derive, ceiling optional; your
   requirement is "no settings to align by hand", and a required list is exactly one.
2. **One library root.** Accept one library root and one staging root in this release, several later? *Recommended:* yes.
3. **Nested legacy layout.** Accept "staging inside the library" for migrated installs with a warning, never for new ones?
   *Recommended:* yes; forbidding it would break every install made so far.
4. **Is the library mounted in the API at all?** This ADR says it is not needed. Do you want it mounted for a file browser
   or space reporting in the UI? *Recommended:* no; ask the gateway (`inspect`).
5. **Copy and verify as the stated mode on your layout, and a measurement first (step 0).** *Recommended:* yes to both; do
   not promise an instant move until measured.
6. **Marker for the delegate mapping.** The mapping verification (§7) stores a tiny file in a `Musizerr Probe <random>`
   folder of the real library through the storage provider and removes it afterwards (the music-server check adds
   nothing: it is the existing verification). Acceptable on the real library? *Recommended:* yes, with the folder name fixed
   and documented so that Lidarr's and Navidrome's logs are explicable; it is removed in a `finally`.
7. **Fixed name `.musizerr-work`** for the working folder, kept even if the product is renamed (ADR-0009). *Recommended:*
   yes: it is a name on disk in users' folders.
8. **No separate "completed downloads" folder.** *Recommended:* no.
9. **Mapping by a probe, not by typing.** Make the verification (marker, `locate_in_import_roots`) the primary way
   and the typed path the fallback? *Recommended:* yes.
10. **Order against ADR-0007 step 3.** Land the database guard before the contract step only, as written, or before the
    first migration of this ADR? *Recommended:* before the contract step; the expand step is safe with the advised manual backup.

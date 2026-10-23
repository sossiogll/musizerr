# ADR-0008: Root folders and folder settings: where the work happens and where the music lives

**Status:** Proposed
**Date:** 2026-10-24 (revised the same day after the maintainer's decisions)
**Deciders:** @sossiogll (maintainer)
**Related:** ADR-0003 (workspaces), ADR-0006 (gateway, `filesystem_from_settings`), ADR-0007 (the SDK major is the contract
version), ADR-0002 (provider instances, still Proposed), ADR-0009 (configuration surface and environment names, the
companion of this one), ADR-0010 (the rename to Musizeer: one breaking release, SDK 6, into which this record's steps 1 to
4 fall)

The record cites the code as it is today (file and line); the names it gives to new things are the ones after the rename of
ADR-0010 (`.musizeer-work`, `musizeer_sdk`).

## Context

### The reference layout: the owner's home server

Two folders side by side in the music area of the server, the one the music server reads:

```
/mnt/hdd2
├── Musica-staging/   where the application downloads and works.
│                     Mounted: application, workers, beat and gateway at /downloads.
│                     Not mounted in Lidarr, not in Navidrome.
└── Musica/           the LIBRARY, every artist. The storage provider writes the final files here with the
                      naming template.
                      Mounted: gateway at /library, Lidarr at /music (its root folder, rw),
                      Navidrome at /music (read-only). Navidrome reads ONLY this folder.
```

The same facts are written today in these places, and every one is set by hand and has to agree with the others:

| Fact | Where it is written today |
|---|---|
| Staging is `/downloads` | the compose volume `downloads:/downloads` on the application, the three workers (shared `worker-volumes`), `beat` and the gateway (`docker-compose.yaml:100,155,200,253`); `WORKSPACE_DIR` (`docker-compose.yaml:32-33`), which becomes the default of the **setting** `workspace_dir` (`app/core/settings_catalog.py:461-466,649`) and, separately, the gateway's `GATEWAY_WORKSPACE_DIR` (`docker-compose.yaml:161`, `gateway/musizerr_gateway/config.py:38,81`) |
| The working folder is `/downloads/.musizerr-work` | the same three; plus the hard-wired name inside `DEFAULT_WORKSPACE_DIR` (`settings_catalog.py:461`) |
| The gateway may grant `/downloads` and `/library` | `GATEWAY_FILESYSTEM_ROOTS` (`docker-compose.yaml:159`, `config.py:45,85`), a **fourth** copy |
| The storage provider writes to `/library` | the provider's own setting `root_dir` (`musizerr-providers/local/.../__init__.py:17,33`, default `/downloads`) and the setting `storage_provider` (`settings_catalog.py:631`) |
| Lidarr's `/music` is the same folder as `/library` | nowhere. Lidarr's root folder is its own configuration; the Lidarr provider takes the first one it finds (`musizerr-providers/lidarr/.../__init__.py:249-253,280-297`) |
| Navidrome reads the library (and is not given staging) | the full-sync verification, which writes a probe through the storage provider and looks for it on the sync provider (`POST /config/verify-full-sync`, `app/routes/config.py:44`); it is not tied to any declared folder, and staging not being visible to the music server is a property of the mounts that nothing states |

### Problems this produces

1. **Copies drift.** `workspace_dir` is both an environment default and a setting, and "a value saved in the settings wins"
   (`settings_catalog.py:457-460`). The setting and the gateway's variable drifted apart on the server and the full-sync
   check broke. Pull request #50 added one `WORKSPACE_DIR` for the environment side and a log line when the host's value
   differs from the gateway's (`app/services/provider_events.py:191-210`, which only logs). The cause is still there: two
   copies of one fact, compared after the fact.
2. **The working folder and the library are not distinguished.** The local storage's `root_dir` default is `/downloads`
   and the workspace is `/downloads/.musizerr-work`: the library holds the workspace, and the gateway hides it from the
   sandbox with an empty `tmpfs` (`gateway/musizerr_gateway/filesystem.py:96-100`). Nothing tells the operator which folder
   the music server must read.
3. **The delegate's folder is unknown to the host.** A delegating download provider hands the job over and the delegate
   imports into its own root folder with its own naming. The host never learns where: `list_delegated_files` and
   `request_rescan` exist in the SDK (`sdk/src/musizerr_sdk/interfaces.py:418,428`) and no code of `app/` calls them. A
   delegated file reaches the library through the storage scan (`app/services/library_scan.py`) or the sync index
   (`app/services/sync_index.py`), and `db/library_store.py` counts a download "landed without a path" as in the library
   without checking anything. It works only if the delegate's root, the storage folder and the music server's folder are
   the same directory; nothing states or checks it.
4. **The full-sync verification is not attached to a declared library.** `verify_full_sync`
   (`app/workers/maintenance.py:58-123`) is the right check and stays: it writes a probe file through the storage provider
   and waits for the sync provider to find it, and on success sets the sync provider's `mode` to `full`. It validates,
   without saying so, that the storage folder and the music server's folder are the same directory.
5. **How a file leaves the working folder is not specified, and today it is not atomic.** This flow exists: a download
   lands in the working folder, the host verifies it, then `StorageProvider.store()` puts it into the library; the local
   provider does `shutil.move(source.path, dest)` (`musizerr-providers/local/.../__init__.py:71`) and `stream()` tries
   `os.link` (`:83`). Through the sandbox the working folder (`/workspace`, bound at `sandbox.py:181`) and the library
   (bound at its own path by a grant) are **separate bind mounts**, so `rename(2)` and `link(2)` fail with `EXDEV` even
   when both are the same disk. `shutil.move` then silently falls back to **copy and delete, writing directly under the
   final name**: a music server that scans at that moment can index a half-copied file. ADR-0003 says the move is "an O(1)
   rename when the workspace shares the library's volume"; for a sandboxed provider that does not happen.
6. **The host does not need the library.** No host code opens a library path (`grep` of `app/`: the library is reached
   through `StorageProvider` methods and files travel as workspace copies, ADR-0003). Only the working folder has to be
   mounted in the API and the workers; the library has to be mounted in the gateway. A smaller mount surface is better.

## Decision

### 1. Three levels, and who owns each

| Level | What | Owned by | Lives in |
|---|---|---|---|
| **Mount** | a host folder attached to a container (`/mnt/hdd2/Musica` → `/library`) | whoever runs the containers | `docker-compose.yaml` / the override file |
| **Root folder** | a location the administrator allows the product to use: a label and an absolute path as the containers see it. **No kind and no role** | the administrator | the database, edited in the UI |
| **Folder setting** | a setting of the new type `folder`: a root plus a path relative to it. The role (working folder, library) belongs to the setting that uses the root | the administrator, where the setting lives | the settings store |

A mount is Docker's business and is not configured in the product. A root is declared **once**, and every folder the product
uses is a root plus a relative path. No setting names an absolute path that a root already names. The boundary rule for the
environment versus the database is ADR-0009's: roots and folder settings are application; the environment carries no folder.

### 2. Root folders are data

The model is Lidarr's "Add root folder". A root is a row (`root_folders`):

| Field | Meaning |
|---|---|
| `id` | stable identifier. Nothing is keyed by path or by provider name (see §10) |
| `label` | what the UI shows ("Downloads", "Library") |
| `path` | the folder **as the containers see it**, absolute and normalized |

Rules:

- **Validated by the gateway**, the only process that sees every mount (§9). The path exists; is a directory; is writable
  (`os.access`, and the explicit Check action creates and removes a hidden file); lies **on a mounted volume** (the longest
  matching entry of the gateway's own `/proc/self/mountinfo` for the resolved path is not the container's root
  filesystem, and not a pseudo or memory file system such as `tmpfs` or `proc`); and does not overlap the gateway's own
  folders (its state and run directories). A root that fails is saved with its reason and is not used.
- **The folder picker browses only mounted volumes** (`browse`, §9): subdirectories only, never outside a volume, links
  that lead out are refused.
- **Two roots may not have the same path.** One may lie inside another (the parent-folder layout below); the checks mention
  it, nothing forbids it, because a root has no role.
- **A root in use cannot be deleted**: the call answers 409 and lists the folder settings that point at it. Changing the
  `path` of a root in use is allowed after a confirmation that lists those settings (a mount that moved), and restarts the
  providers whose granted folders change.
- **Where the first roots come from.** A new install has none. The shipped compose mounts the conventional targets
  `/downloads` and `/library` (ADR-0009), the first-run step lists the mounted volumes the gateway reports, and the
  administrator declares what they want. The paths `/downloads` and `/library` are a convention of the compose file, not a
  setting and not code.

### 3. Folder settings: a root and a relative path

A new setting type **`folder`**, in the settings catalog (`SettingType`, `settings_catalog.py:57`) and in the SDK
(`ProviderSetting(type="folder")`, SDK 6). Its stored value is `{"root_id": "<id>", "path": "<relative>"}`.

- **Resolution** (host, `services/folders.py`, the one place that does it). `path` is relative: no leading `/`, no empty,
  `.` or `..` segment, no NUL, at most 4096 characters; the empty string is the root itself. The folder is the root's path
  joined with it, normalized. If the folder exists where the host runs, its real path must lie inside the real path of the
  root (a link that leaves the root is refused). The gateway repeats the containment check for every folder it grants
  (the existing rule, `filesystem.py:89-90`, now against the declared roots), because it sees mounts the host does not.
- **The SYSTEM setting "working folder"** (the old `workspace_dir`, same key) is a `folder` setting. Its default is the
  first root plus `.musizeer-work`. The held uploads and the per-provider workspaces stay under it exactly as ADR-0003
  says: `<working folder>/<provider>/<random>` and `<working folder>/held-uploads`. With no root yet there is no working
  folder, and a download is refused with a clear error until the first-run step is done. It must lie in a root that is
  mounted at the same path in the application, the workers and the gateway (the host checks its own side; the gateway
  reports its own).
- **A PROVIDER setting can be of type `folder` too.** The local storage provider's library folder (`root_dir`) is one. The
  UI renders a **root selector plus a sub-path field** (with the picker limited to the root). A provider folder setting has
  **no default root**: until an administrator picks one the setting is unset and the provider says it is not configured
  (a default of "the first root" could put the library in the downloads folder). The `default` of the setting is only the
  suggested relative path.
- **A provider still receives a plain absolute path string** through its context (`self.context.setting("root_dir")`): the
  host resolves a folder value when it builds the context snapshot (`HostProviderContext.snapshot()`), so provider code
  does not know about roots and the Host Boundary holds.
- **`filesystem_from_settings` names folder settings.** The host sends the gateway the resolved path of each, as it already
  does (`{"op": "egress", ... "paths": {setting: "/path"}}`, `control.py`); the gateway judges it against the declared
  roots the host pushed with `install` (§9). The access mode stays the one the verified manifest declares. A setting named
  there that is not of type `folder` is refused, and the conformance rule below catches it before.
- **Conformance** (`musizeer_sdk.conformance`, with a good and a bad sample provider in `sdk/tests/test_conformance.py`): a
  `folder` setting has a `default` that is a relative normalized path or `None`, is not `secret`, and every setting named
  by `filesystem_from_settings` is of type `folder`.
- A provider whose location is not a local folder (FTP's `remote_base_path`, a future object store) keeps a `string`
  setting: a path in the remote namespace is not a root of this machine.

### 4. What stops being a setting or a variable

| Gone | Replaced by |
|---|---|
| `WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` (ADR-0009) | the working folder, a `folder` setting; the host **pushes** the resolved path to the gateway (§9) |
| `GATEWAY_FILESYSTEM_ROOTS` (ADR-0009) | the declared roots, pushed with the installed set (§9) |
| the storage provider's hand-typed absolute folder | its own `folder` setting (root + path) |
| the host's check that host and gateway agree (`provider_events._check_workspace`, `:191`) | not needed for correctness: the host tells the gateway. It stays as a one-line safety net: after a push the host compares `workspace_root` of the gateway's `list` with its own and logs a mismatch |

The setting `storage_provider` (which storage is active) stays a setting: it is a choice, not a copy of a path.

The owner's server then reads like this. **Roots** (declared once): Downloads `/downloads`, Library `/library`. **Settings:**
working folder = Downloads + `.musizeer-work`; local storage folder = Library + empty path; `storage_provider` = `local`.
The gateway grants `/library` to the storage provider read-write and, from the working folder, each provider's own part
of `/downloads`. Navidrome reads only `Musica`.

*Variant.* When one parent folder is mounted (`/mnt/hdd2` as `/media`), a **single root** `/media` works too: working folder
= Media + `Musica-staging/.musizeer-work`, local storage folder = Media + `Musica`. The music server still mounts only
`Musica`. The cost is that the gateway then sees the whole parent.

### 5. Direct download providers and delegating providers

| | Direct | Delegating |
|---|---|---|
| Who downloads | the provider process, in its sandbox | an external system (a library manager with its own queue) |
| Where bytes first land | the workspace, inside the **working folder** | the delegate's own download client folder; not the product's, not a root |
| Who names the final file | the product (`track_format`, `services/library_paths.py:19`) | the delegate (its own naming) |
| Who puts it in the library | the storage provider (`store`) | the delegate, into **its** root folder; the product never moves it |
| How the product learns where the file is | it chose the path (`LibraryEntry`, `source=download`) | the **mapping** of §7, then a listing |
| What must be declared | a working folder and a storage provider with a library folder | a mapping from the delegate's root folder to the storage provider's library folder |

The two coexist on one library: a direct download writes `{artist}/{album}/...`, a delegate writes with its own format.
Two namings in one tree can duplicate an album on disk; that is a known limitation until the naming of the library can be
read from the provider that writes into it (§12).

### 6. How a file gets into the library

The flow already exists and stays the same: the provider writes the file in the working folder; the host verifies it and
decides the final relative path (`library_path()`); the host calls `StorageProvider.store(source, relative_dest)`; the
host then checks the stored file's size with `StorageProvider.size` (`app/workers/tasks.py:377`, `_verify_after_upload`,
setting `verify_after_upload`, default `size`) and only then marks the download as landed and removes the workspace; a
failed store keeps the file as a held upload and retries.

What is wrong is only the middle step on a sandboxed provider (problem 5): the local provider's `shutil.move` copies
straight under the final name when it cannot rename. **The decision: `store()` MUST make the file appear atomically, or not
at all.** It is a documented `MUST` on `StorageProvider.store` in the SDK, a conformance obligation of every storage
provider, and no signature change. For a provider that writes to a folder, the way is:

1. copy the source to a hidden temporary name **in the final directory** (`.<name>.part`; not an audio extension, so
   scanners ignore it);
2. flush it and `fsync`;
3. `rename` it onto the final name (atomic inside one directory).

A reader of the library therefore never sees a partial file. The local provider is changed to do exactly this when `rename`
from the working folder fails, instead of `shutil.move`. A provider for a remote store uploads to a
temporary name and renames, or uses the service's own atomic upload.

On the owner's layout this is **a copy, and that is fine**: two folders on one disk, two bind mounts. The cost is a second
read and write of one file at the end of a download, against the seconds to minutes the download took. There is no
measurement to wait for. If a layout ever shows the copy matters, a single-mount fast path (the gateway presenting the
working folder and the library as one mount to a storage provider) is a later, optional change and not part of this record.

The deployment guide says: keep the two folders on one disk for speed; correctness never depends on it.

### 7. The delegate's mapping: Lidarr's `/music` is the library folder

A delegate imports into a folder it names in its own namespace. The host stores that fact as a **mapping**
(`root_mappings`, the name kept from the first draft; it maps a path of the delegate, not a root): the delegate's
`remote_path` corresponds to **the resolved folder of the storage provider's library folder setting**, i.e. a root plus a
path.

| Field | Meaning |
|---|---|
| `provider` | the delegate or music server (text; becomes an instance id if ADR-0002 option C is taken, §10) |
| `remote_path` | the folder in that program's namespace (`/music`) |
| `role` | `writer` (a delegate that imports into it) or `reader` (a sync provider that reads it) |
| `target_folder` | the resolved library folder when it was verified (`/library`), kept to notice a later change |
| `verified_at`, `verified_result` | when and how it was checked. If the library folder resolves to something else than `target_folder` now, the mapping is shown as "to verify again" |

Which setting is the library folder is **declared by the storage provider**: `StorageProvider.library_folder_setting() ->
str | None` (SDK 6, a declared query in the manifest tables; `None` for a provider whose library is not a local folder).
Without it the mapping still works by relative paths (below); it only cannot show or check the folder.

Path translation is lexical and on path boundaries: a path `p` of the delegate is under the library when
`p == remote_path` or `p` starts with `remote_path + "/"`; its relative path in the library is `p` minus that prefix, which is
also the key of `LibraryEntry.file_path`. `/music/Artist/Album/01 - x.flac` is `Artist/Album/01 - x.flac` in the library.
The storage provider's `exists(relative)` is the evidence that the file is there.

Two optional methods on `DownloadProvider` (an SDK 6 minor, data only, declared with `supports_*` queries so the host asks
only a provider that has them; no provider is required to change):

- `list_import_roots(session) -> list[str]`: the folders the delegate imports into, in its namespace (Lidarr: its root
  folders). This is what the administrator maps, and what the host compares against: a delegate that imports into a root
  nobody mapped is reported ("Lidarr imports into `/music`, which is not mapped to the library").
- `locate_in_import_roots(name, session) -> str | None`: where the delegate sees a folder of this name inside its import
  roots. Used by the **verification**.

**Verification of a mapping** (an action in the UI, and part of the checks), by a probe file, without typing:

1. the host stores a small marker file through the storage provider in `Musizeer Probe <random>/probe.ogg` under the library;
2. it asks the delegate where it sees `Musizeer Probe <random>` (`locate_in_import_roots`); the answer,
   `/music/Musizeer Probe <random>`, gives `remote_path = /music` by removing the relative part;
3. it removes the marker, in a `finally`. A delegate that does not implement the method is verified by suffix instead: a
   file the delegate reports for a delegated track (`list_delegated_files`, `FileRef.path`) must end with a relative path
   that the storage lists. The typed path is the fallback when neither works.

Once a mapping exists, completing a delegated download stops being invisible. The host calls `list_delegated_files` for the
handle, translates each path, and records a `LibraryEntry` (`source=delegate`) for every file the storage confirms. The sync
index and the scan remain as the safety net they are today. A path outside every mapping is shown to the administrator as
"imported outside the library you declared", not ignored.

### 8. The music server reads the library: the existing full-sync verification

No new mechanism. The existing full-sync verification (`POST /config/verify-full-sync` and its task `verify_full_sync`: a
probe file is written through the storage provider, the sync provider is asked to scan, and the probe is looked for on it)
is the check that **the music server sees the library**, unchanged:

- the probe goes through the storage provider, so it lands in the library folder, and finding it on the sync provider is
  the evidence that the music server reads the same folder (the owner's Navidrome, `/music` read-only, which is
  `/mnt/hdd2/Musica`, the product's `/library`);
- its result is shown with the library folder in the checks (§11) as "music server sees this library: verified at <time>",
  and the `mode` of the sync provider keeps being written by it as today;
- the endpoint, the task and the timeouts (`full_sync_probe_timeout_seconds`, `full_sync_probe_interval_seconds`) do not
  change. The probe folder is renamed `Musizeer Probe` with the product (`workers/maintenance.py:89-95`).

**The working folder should stay invisible to the music server.** The music server must be given the library folder and not
the downloads folder, or it indexes half-downloaded files. In the reference layout this holds by construction. It is a
documented requirement in the deployment guide and in the page's help text, not something the product checks. Likewise
the product cannot see that the music server mounts the library **read-only**; the guide recommends `:ro`.

### 9. The gateway follows the roots

The environment no longer carries a list of folders or a workspace (ADR-0009). The host **pushes** them to the gateway with
the desired set of providers (`{"op": "install"}`, `control.py`, which already carries the set and its generation; and again
on every change of a root, of the working folder or of a provider folder, as it does for providers,
`services/gateway_sync`). The message gains `roots: [{id, path}]` and `workspace: "<resolved working folder>"`. The gateway:

- accepts a root if it passes the checks of §2 (the existing `check_root` / `validate_grant` rules in
  `gateway/musizerr_gateway/filesystem.py` apply to each) **and** it lies on a mounted volume (the `mountinfo` rule). A root
  cannot be `/etc` or any other part of the image;
- accepts the workspace only if it lies inside an accepted root, and mounts each provider's part of it as `/workspace`
  (`supervisor.py:272`, `sandbox.py:181`) as today;
- judges every folder a provider is granted (`validate_grant`, called at `supervisor.py:362`) against the **accepted roots**
  instead of `GATEWAY_FILESYSTEM_ROOTS`. Every folder a provider is given is therefore inside a declared root. A folder
  that holds the working folder is bound with the working folder covered by an empty `tmpfs` (`filesystem.py:96-100`, as
  today); one inside it is refused;
- restarts a provider whose binds changed (existing behaviour of a change of grants);
- reports what it accepted and refused per root in `list` (`roots: [{id, path, state, reason, device, mount_id, writable,
  free_bytes}]`, next to the `workspace_root` it reports now) and answers two new read-only operations for the UI:
  `inspect` (one path: exists, directory, writable, device, mount, free space) and `browse` (the subdirectories of a path
  inside a mounted volume, never files, symlinks resolved and refused when they lead out; this is the picker of Lidarr's
  "Add root folder").

There is no environment ceiling. The security argument: a provider never chooses a folder; only an administrator declares a
root, and what can be declared is bounded by what Docker mounted into the gateway. Before the host has pushed anything the
gateway runs no provider (unchanged), so there is no window in which it has a workspace the host does not know.

### 10. Relation to ADR-0002 (provider type vs instance)

This record does **not** need ADR-0002 and is written not to prejudice it:

- A root is keyed by its own `id` and knows nothing about providers. A folder setting belongs to a provider's settings, so
  under option C each instance has its own folder value over the same roots; nothing is re-keyed.
- `root_mappings.provider` has the same fate as any provider name stored as data: it becomes an instance id.
- **One library in this release is the consequence of ADR-0002 being open.** Two libraries served by the same provider *type*
  (two local folders) need instances or a per-call folder in the contract, because the folder lives in a provider setting
  that has one value per type. The roots model has no limit on how many roots exist; the limit is in `storage_provider`.
- Option D (per-role credentials) does not interact.

### 11. What each part of the reference layout is checked by

"Declared once and checked": the page *Root folders* (§UI) shows one report; its lines are the checks above.

| Check | Done by | Reference layout |
|---|---|---|
| each root exists, is writable and is a mounted volume | gateway `inspect` | `/downloads`, `/library`: ok |
| the working folder exists, is inside a root and is the gateway's | host asks `list` | Downloads + `.musizeer-work`: ok |
| the working folder is not inside the library folder | host | ok (the installs converted from the old default are flagged: see the conversion) |
| the library folder exists, is writable by the storage provider and mounted in the gateway | gateway `inspect` + `test_connection` | Library + empty path: ok |
| the library is not required in the API | informational | "not mounted in the API: fine" |
| working folder and library: same disk? same mount? | gateway (`device`, `mount_id`) | same disk, two mounts: files are copied, atomically (§6) |
| the delegate's import root is mapped | `list_import_roots` + mapping + verification | `/music` ↔ the library: ok / "not mapped" |
| the music server sees the library | the existing full-sync verification | verified / not yet verified |
| the music server is not given the download folder | documentation (§8), not checked | siblings, only `Musica` mounted in Navidrome |
| a delegate that imports where nobody mapped | `list_import_roots` | none |

### 12. Room left for what comes after (not decided here)

Both are SDK minors (ADR-0007 §3.1: optional methods with a default, a declared query, new types with `wire.SINCE`), and
both consume what this record creates:

- **`LibraryLayout`** and `get_library_layout(session) -> LibraryLayout | None`: a provider that has a naming configuration
  (Lidarr's `standardTrackFormat`, `artistFolderFormat`, `rootfolder`) returns it already translated into the host's
  template vocabulary, with a `lossless_translation` flag. The library is where it lands: `track_format` gets a source
  (`own`, or `follow:<provider>`), and the `root_folders` of the layout are the candidates of §7. Without the mapping there
  is nothing to attach it to.
- **`resolve_import_block(handle, action, session)`**: a delegate whose import is blocked (the destination folder already
  holds the album, "not an upgrade") reports the block with `FileRef`s in **its** namespace. The mapping of §7 translates
  them into library paths, so the host can list what is already there (storage listing) and offer `import_replace` /
  `import_keep_both` / `skip`; today a `CONFLICT` without conflicts data ends by timeout.

## The conversion of today's values, once, in the breaking release

There is no migration window, no dual write and no second copy (ADR-0010). One idempotent step converts what an install
already holds, when it can be expressed as roots and folder values:

- Only the gateway can say what is mounted, so the API does it the first time the gateway answers (`services/roots.py`,
  called from `GatewayWatcher.check`), and records that it is done. A fresh install has nothing to convert.
- **Working folder.** The stored `workspace_dir`, or the old default `/downloads/.musizerr-work` when none is stored. A
  value ending in `.musizerr-work` becomes the root of its parent plus `.musizeer-work` (the folder is ephemeral; the old
  one is left on disk and the held uploads in it are dropped, so those downloads repeat: an upgrade note). Another value
  becomes the root of its parent plus its last component, the same folder.
- **Provider folder settings.** A string stored under a setting the installed provider now declares as `folder` (the local
  provider's `root_dir`): if a declared root contains it, it becomes that root plus the remainder; else a root at the path
  itself is created (label from the last component). The old default `/downloads` of the local library becomes the root
  created for the working folder with an empty path, which is the "working folder inside the library" shape: it works as
  it did, the check of §11 flags it, and the first-run step proposes a separate library.
- Anything the gateway refuses (not mounted, not writable, cannot be expressed) is left **unset**, and the first-run
  checklist (Administration › Providers) says which setting and why.
- Labels are the last path component, capitalised; the administrator can rename them.

The owner's server, converted: the working folder gives the root `/downloads` ("Downloads") and `.musizeer-work`; the local
provider's `/library` gives the root `/library` ("Library") and an empty path. The checks then propose the mapping `/music` ↔
the library when that step exists, and the existing full-sync verification runs for Navidrome. The compose file loses
`WORKSPACE_DIR`, `GATEWAY_WORKSPACE_DIR` and `GATEWAY_FILESYSTEM_ROOTS`; the `/library` mount remains only in the gateway.

## UI

Administration › **Root folders** (admin only), modelled on Lidarr's Settings › Media Management:

- A table of roots: label, path, free space, status (ok / refused with the reason), the folder settings that use it, and a
  **Check** button that runs the report of §11. **Add root folder**: the folder picker (`browse`) limited to mounted
  volumes, and a label.
- A table of **Folders**: every folder setting (the working folder, each provider's), its root, path, resolved folder and
  status. Each is edited where it lives (Settings › Library › Files for the working folder, the provider's card for a
  provider's) with the same control: a root selector, a sub-path field and the picker inside the root.
- A table **Other tools** (the mappings): one row per delegate or music server: "Lidarr imports into `/music`", mapped to
  the library, with **Verify**; "Navidrome reads the library", with the result of the full-sync verification (and a
  one-line reminder not to give the music server the downloads folder).
- First-run checklist (Administration › Providers): "Choose where your music lives" opens the same dialog.

Strings are keys in the dictionaries; no provider name is in the page (the lists come from `/providers` and the declared
capabilities).

## Implementation plan

Steps 1 to 4 are part of the single breaking release of ADR-0010 (one pull request into `dev`); 5 and 6 are ordinary
minors after it.

1. **Gateway: roots, mount detection and `inspect`/`browse`** (`docs/gateway.md`): `roots` and `workspace` pushed in
   `install`, the `mountinfo` rule, grants judged against the accepted roots, `list.roots`, no `GATEWAY_FILESYSTEM_ROOTS` or
   `GATEWAY_WORKSPACE_DIR`, tests with the existing sandbox integration script.
2. **Host: data and folders**: the table `root_folders`, the `folder` setting type, `services/folders.py`
   (resolution), `services/roots.py` (validation, conversion of today's values), `GET/POST/PATCH/DELETE /roots`,
   `app/openapi.yaml`; the host pushes roots and working folder; `held_uploads`, `workspace`, `library_scan` take the working
   folder from the setting.
3. **UI**: Root folders page, the folder control, first-run step, e2e spec.
4. **SDK 6 and providers**: `ProviderSetting(type="folder")`, the conformance rule, `library_folder_setting()`, the
   documented atomicity of `store`; the local provider declares a `folder` setting and implements the atomic copy of §6
   (providers repository, catalog entry).
5. **Delegate mapping** (SDK 6.1): the table `root_mappings`, `list_import_roots`, `locate_in_import_roots`, mapping UI, verification, registering
   delegated files by `list_delegated_files` + mapping, the "imported outside the library" notice. The Lidarr provider
   implements both.
6. **Full-sync verification shown with the library folder**: the result is stored with the library folder and the check is on
   the page. No new probe.

Later records: `LibraryLayout`/naming and import blocks (§12), several libraries (after ADR-0002).

## Alternatives considered

- **Roots with a kind (library / staging), a default per kind and a storage provider on the root** (the first draft). It put
  the role in the wrong place: the working folder is a system setting, and where files go into the library depends on the
  storage provider. With roots that have no role, one mechanism serves every folder, including a provider's.
- **A setting that holds the absolute path, validated against the list of roots.** Simpler to store, but a path can drift out
  of its root when the root moves, and the UI could not offer "this root, this sub-path". A relative path under a root
  cannot name anything outside the root.
- **Keep an environment list as a ceiling on the gateway.** One more copy of what the administrator declares, the class of
  bug this record removes. Rejected by the maintainer; the mounts are the bound.
- **Roots in the environment only (compose).** One place, but changing a folder needs a restart and an edit on the server; no
  folder picker, no per-root validation in the UI, no mapping table.
- **Roots in the UI only, without the gateway verifying mounts.** An administrator could declare any path of the gateway's
  image. Rejected: the mount rule (§9) is cheap and bounds the damage.
- **A separate "completed downloads" folder (Lidarr's download client folder).** Lidarr needs one because its download client is
  another program. The product's downloads are its own: the working folder is that folder. A delegate's client folder belongs
  to the delegate and is not a root.
- **Let the host move the file into the library.** It would make an atomic rename possible in the host process, but gives the
  host (and the workers) write access to the library and takes the folder-granting model of ADR-0003/0006 apart. Rejected.
- **"Measure first", then decide how a file moves.** Replaced by a decision: the measurement could only choose between two
  correct behaviours, and the copy is correct on every layout (§6).
- **Make ADR-0002 option C a prerequisite.** Delays the work the owner needs for a decision nobody has asked for.

## Consequences

- The facts the owner kept equal in six places are declared once; the gateway and the storage provider follow by
  construction; the drift between the setting and the gateway cannot happen.
- A provider's folder is chosen as a root and a path, never typed as an absolute path, and cannot leave its root.
- The page of checks says whether each folder is usable, whether the music server reads the library (the existing
  full-sync verification) and whether the delegate writes to the same library (the mapping and its verification). The
  owner's layout is expressible and verifiable.
- The API and the workers no longer need the library mounted; the gateway needs it.
- The final step into the library is verified (size) and appears atomically. On the owner's layout it is a copy.
- The gateway gains control operations (`inspect`, `browse`) and a mount rule; `docs/gateway.md` and its tests grow.
- No environment variable limits what an administrator can declare: an operator who mounts more into the gateway than music
  folders lets an administrator use it.
- One library per install until ADR-0002 is settled; several are a later decision, not a rewrite.
- A contract addition (`folder`, `library_folder_setting()`) breaks every provider, which the SDK 6 of ADR-0010 absorbs.

## Questions for the maintainer

1. **No environment ceiling on the gateway.** The bound is what Docker mounts into it plus the mount rule. *Recommended:*
   accept; the deployment guide says to mount only the music folders into the gateway.
2. **Defaults.** The working folder defaults to the first root plus `.musizeer-work`; a provider's folder setting has no
   default root and stays unset until chosen. *Recommended:* yes (the first-run step asks for both, so the working-folder
   default rarely applies).
3. **Nested roots.** Allowed and noted in the checks, the same path twice refused. *Recommended:* yes.
4. **`StorageProvider.library_folder_setting()`**, the declaration that tells the host which of a storage provider's folder
   settings is its library (for the mapping's display and staleness). *Recommended:* yes; the alternative, treating the
   only read-write folder setting as the library, breaks the first time a provider has two.
5. **The marker for the mapping verification** is a tiny file in a `Musizeer Probe <random>` folder of the real library,
   written through the storage provider and removed in a `finally`. *Recommended:* yes, with the folder name fixed and
   documented so that Lidarr's and Navidrome's logs are explicable.
6. **Mapping by probe, not by typing.** The verification (marker, `locate_in_import_roots`) is the primary way and the typed
   path the fallback. *Recommended:* yes.
7. **Is the library mounted in the API at all?** Not needed. *Recommended:* no; ask the gateway (`inspect`).
8. **Installs converted from the old default** end with the working folder inside the library folder. *Recommended:* accept it
   with the warning of §11 and let the first-run step offer a separate library; forbidding it would break every install made
   so far (there is one).
9. **Changing the path of a root in use**: allowed after a confirmation that lists what depends on it, or only by deleting and
   recreating. *Recommended:* allowed with the confirmation (a mount that moved is the normal reason).

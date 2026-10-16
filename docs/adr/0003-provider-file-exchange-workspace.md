# ADR-0003: How files cross the provider boundary

**Status:** Accepted
**Date:** 2026-09-27
**Deciders:** @sossiogll (maintainer)

## Context

Providers are becoming external packages that may later run in a sandboxed container.
Providers already reach the host only through their `ProviderContext`, the `AuthSession`
and return values (see CLAUDE.md, "The Host Boundary"). Files were the last exception.
Before this decision, absolute host paths crossed the contract in both directions:

- `download_by_id(identifier, dest, session)` received an absolute path without extension
  and returned the absolute path it wrote. With local storage
  (`writes_directly_to_destination()`), the download provider wrote straight into the
  library.
- `StorageProvider.store(source_path, ...)` read an absolute host path.
- `StorageProvider.stream()` returned either an absolute `local_path` or a live byte
  iterator. For local storage that was the library file itself. For FTP it was a
  temporary file that nobody deleted, so every FTP stream leaked a full copy of the track.
- `SyncProvider.fetch_partial()` returned the absolute path of a temporary copy, with
  `is_temp` telling the caller whether to delete it.
- `LocalStorageProvider` read the host setting `downloads_dir` through `ConfigManager`.

None of this works across a container boundary. It also gives every download provider
write access to the library.

## Decision

1. **Host-owned workspaces.** For every file-producing call, the host creates an empty
   directory under the `workspace_dir` setting (default `/downloads/.musizerr-work`, on
   the same volume as the default library, hidden so library scanners skip it). The
   host removes the directory when the operation ends, also on failure. A system routine
   removes directories left behind by a crash.
2. **Relative names only.** A provider receives the workspace directory and returns a
   file name relative to it. It never receives or returns another host path. The host
   rejects a returned name that escapes the workspace.
3. **Downloads always land in a workspace.** `download_by_id(identifier, workspace,
   session)` and `download_fuzzy(track, workspace, session)` return the name of the file
   they wrote. The host decides the final library path (track format template plus the
   file's extension) and hands the file to the storage provider.
   `writes_directly_to_destination()` is removed: no download provider touches the library.
4. **Storage reads from a workspace.** `store(source, relative_dest, session)` takes a
   `WorkspaceFile(workspace, name)`. Local storage moves the file, which is an O(1)
   rename when the workspace shares the library's volume.
5. **Local storage owns its root.** The library root is the local storage provider's own
   setting `root_dir`. A data migration copies the stored `downloads_dir` value into it,
   and the host no longer reads `downloads_dir`.
6. **Streaming results are data.** `stream(relative_path, workspace, session)` returns
   one of:
   - a file name in the workspace, which the host serves with `FileResponse`, keeping HTTP
     Range requests and therefore seeking, and deletes after the response;
   - a URL with optional headers, which the host proxies;
   - a chunk iterator, for providers that can only stream.

   Local storage makes a hard link into the workspace instead of copying: O(1) on the same
   volume, with a copy only as a fallback. This deviates from a literal "copy into the
   workspace" to avoid copying the whole track on every play.
7. **Partial reads land in a workspace.** `fetch_partial(sync_id, workspace, session,
   max_bytes)` returns a file name. `PartialFile` and `is_temp` are removed, because the
   host always cleans up. `identify(audio)` reads a `WorkspaceFile` too.
8. **Contract version.** `core.interfaces.PROVIDER_API_VERSION` identifies this contract. Providers
   installed from external packages must declare `provider_api_version`. The registry
   refuses a package that declares a different version (or none) and logs why. Providers
   shipped in the repository are part of the host and are not checked.

## Stored paths

The data audit on 2026-09-27 (3433 downloads, 1925 library entries) found no absolute
`file_path`. No normalizing migration is needed. After this change the host builds every
stored path from the template, so it cannot produce absolute ones.

## Consequences

- The contract changes for download, storage and sync providers. External packages must
  be updated at the same time, and the version check makes a stale package fail loudly
  at discovery instead of at runtime.
- The FTP streaming leak disappears, because the temporary copy now lives in a workspace
  the host deletes.
- The default workspace is inside the downloads volume. On an installation whose library
  lives on another volume, local storage falls back from rename to copy, so it stays
  correct but gets slower. `workspace_dir` can be moved next to the library.
- A future sandbox gateway mounts the workspace directory into the provider container and
  implements the same contract without touching providers.

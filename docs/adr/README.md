# Architecture decision records

One file per decision, numbered in the order they were written. A record is never deleted; a later one supersedes it
and says so. **Status** is the one in the record's own header. Records written before ADR-0010 keep the name the product
had then (Musizerr): they are history, and are not edited for the rename.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-download-reliability-and-library-reconciliation.md) | Download reliability, delegated-download reconciliation and library integrity | Accepted |
| [0002](0002-provider-instance-identity.md) | Provider type vs provider instance | Proposed, awaiting decision |
| [0003](0003-provider-file-exchange-workspace.md) | How files cross the provider boundary | Accepted |
| [0004](0004-stored-host-url-references.md) | URLs served by the host are never stored | Accepted |
| [0005](0005-identity-data-and-reads.md) | Identity data has one home, and reading never writes it | Accepted |
| [0006](0006-provider-distribution-and-sandbox.md) | Distributing providers as OCI images on GHCR and running them in a sandbox | Implemented (phases 1 to 6) |
| [0007](0007-versions-compatibility-and-upgrades.md) | Versions, compatibility and upgrades | Accepted, partly implemented (see its implementation plan) |
| [0008](0008-library-roots-and-staging.md) | Root folders and folder settings: where the work happens and where the music lives | Accepted (2026-10-25), not yet implemented |
| [0009](0009-configuration-surface-and-environment-names.md) | The configuration surface: deployment versus application, and the `MUSIZEER_` environment names | Accepted (2026-10-25), not yet implemented |
| [0010](0010-rename-to-musizeer.md) | Rename the product from Musizerr to Musizeer, in one breaking release | Accepted (2026-10-25), not yet implemented |

Dependencies worth knowing: 0008, 0009 and 0010 are one piece of work shipped as **one breaking release** with no
compatibility window (SDK 6): 0010 is the release (the rename, the inventory, the order of work, the procedure for the
owner's server), 0009 gives it the environment names and the boundary between environment and settings, 0008 the root
folders and the `folder` setting type. 0010 supersedes the staged rename that 0009's first draft contained. 0008 leaves 0002
open on purpose, and 0010 does not use the overlapping majors of 0007 §3.2.

## Later

Deferred on purpose, and the subject of the **last** ADR or ADRs, after Musizeer 2.0 and the Lidarr work: the
identity / retag / dedup pipeline, the cross-provider cache, and the restructuring of the settings into tabs. Nothing
about them is decided here.

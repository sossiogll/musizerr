# Architecture decision records

One file per decision, numbered in the order they were written. A record is never deleted; a later one supersedes it
and says so. **Status** is the one in the record's own header.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-download-reliability-and-library-reconciliation.md) | Download reliability, delegated-download reconciliation and library integrity | Accepted |
| [0002](0002-provider-instance-identity.md) | Provider type vs provider instance | Proposed, awaiting decision |
| [0003](0003-provider-file-exchange-workspace.md) | How files cross the provider boundary | Accepted |
| [0004](0004-stored-host-url-references.md) | URLs served by the host are never stored | Accepted |
| [0005](0005-identity-data-and-reads.md) | Identity data has one home, and reading never writes it | Accepted |
| [0006](0006-provider-distribution-and-sandbox.md) | Distributing providers as OCI images on GHCR and running them in a sandbox | Implemented (phases 1 to 6) |
| [0007](0007-versions-compatibility-and-upgrades.md) | Versions, compatibility and upgrades | Accepted, partly implemented (see its implementation plan) |
| [0008](0008-library-roots-and-staging.md) | Root folders: the library, the download (staging) folder, and everything derived from them | Proposed |
| [0009](0009-configuration-surface-and-environment-names.md) | The configuration surface: deployment versus application, and the `MUSIZEER_` environment names | Proposed |

Dependencies worth knowing: 0008 and 0009 are a pair (0009's boundary rule is applied first by 0008); both are
migrations in the sense of 0007 (expand in a MINOR, contract in a MAJOR) and 0008 leaves 0002 open on purpose.

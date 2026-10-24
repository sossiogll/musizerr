#!/usr/bin/env bash
# The "Database" section of a release's notes (ADR-0007 section 4): printed only when the release adds a contract
# migration, which the previous release cannot run on. Called by .github/workflows/images.yml; tested by hand against
# the output of app/scripts/migration_phases.py (see docs/migrations.md, "Releases").
#
# Usage: scripts/release-database-notes.sh <migrations.json> <owner/repository> <version>
set -euo pipefail

migrations=$1
repository=$2
version=$3

if [ "$(jq .contract_migrations "$migrations")" = true ]; then
  echo "## Database"
  echo
  echo "This release has **contract migrations** ($(jq -r '.contract | join(", ")' "$migrations")). The previous release cannot run on the result: init takes a backup first (\`/data/backups\`), and going back means restoring it ([docs/migrations.md](https://github.com/$repository/blob/v$version/docs/migrations.md))."
  echo
fi

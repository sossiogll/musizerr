#!/usr/bin/env bash
# The release.json of a release (ADR-0007 sections 4 and 5.1): what an instance's update check reads. Called by
# .github/workflows/images.yml; the migration facts come from app/scripts/migration_phases.py.
#
# Usage: scripts/release-json.sh <version> <provider-contract> <images-json> <manual-steps true|false> <migrations.json>
set -euo pipefail

version=$1
contract=$2
images=$3
manual=$4
migrations=$5

jq -n --arg version "$version" --argjson contract "$contract" --argjson images "$images" --argjson manual "$manual" \
  --slurpfile migrations "$migrations" \
  '{version: $version, provider_contracts: [$contract], images: $images, manual_steps: $manual,
    contract_migrations: $migrations[0].contract_migrations, min_upgrade_from: $migrations[0].min_upgrade_from}'

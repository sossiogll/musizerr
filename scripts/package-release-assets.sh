#!/bin/sh
# The files an installer needs next to each other, without cloning the repository (docs/deployment.md):
#
#   scripts/package-release-assets.sh <output directory>
#
# writes there
#   docker-compose.yaml              the production compose file, its default seccomp profile pointing at the file below
#   musizerr-gateway.seccomp.json    the gateway's seccomp profile (gateway/seccomp/musizerr-gateway.json)
#   env.example                      .env.example (a leading dot would be renamed by GitHub)
#
# .github/workflows/images.yml attaches them to every GitHub release, so an installer runs
#   curl -fsSLO https://github.com/<owner>/musizerr/releases/latest/download/docker-compose.yaml
# (and the same for the other two). A Docker `security_opt: seccomp=` is a path on the HOST that the daemon (or
# compose) reads: it cannot come from inside an image, so the profile has to be a file next to the compose file.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:?usage: package-release-assets.sh <output directory>}
mkdir -p "$OUT"

PROFILE_SOURCE=./gateway/seccomp/musizerr-gateway.json
PROFILE_ASSET=musizerr-gateway.seccomp.json

grep -q "seccomp=\${GATEWAY_SECCOMP_PROFILE:-$PROFILE_SOURCE}" "$ROOT/docker-compose.yaml" \
  || { echo "docker-compose.yaml no longer names $PROFILE_SOURCE as the default of GATEWAY_SECCOMP_PROFILE: update this script" >&2; exit 1; }

sed "s#GATEWAY_SECCOMP_PROFILE:-$PROFILE_SOURCE#GATEWAY_SECCOMP_PROFILE:-./$PROFILE_ASSET#" "$ROOT/docker-compose.yaml" > "$OUT/docker-compose.yaml"
cp "$ROOT/gateway/seccomp/musizerr-gateway.json" "$OUT/$PROFILE_ASSET"
cp "$ROOT/.env.example" "$OUT/env.example"

grep -q "GATEWAY_SECCOMP_PROFILE:-./$PROFILE_ASSET" "$OUT/docker-compose.yaml" || { echo "the compose file was not rewritten" >&2; exit 1; }
if grep -q "$PROFILE_SOURCE" "$OUT/docker-compose.yaml"; then
  # Only comments may still name the repository path.
  if grep -v '^ *#' "$OUT/docker-compose.yaml" | grep -q "$PROFILE_SOURCE"; then echo "the compose file still names $PROFILE_SOURCE" >&2; exit 1; fi
fi
ls -l "$OUT"

#!/bin/sh
# Build a provider image and push it to a registry; print the digest to install it by.
#
#   scripts/build-provider-image.sh <provider-dir> <registry> [tag]
#
#   <provider-dir>  a directory with a Dockerfile FROM the base image (../musizeer-providers/lrclib)
#   <registry>      host:port of a registry the Docker daemon can push to. For images under
#                   test that is a LOCAL registry (docker run -d -p 127.0.0.1:18500:5000
#                   registry:2, or the oci-registry service of docker-compose.dev.yml);
#                   nothing is ever pushed to ghcr.io or Docker Hub by this script's
#                   defaults, and it does not log in anywhere.
#   [tag]           default: dev
#
# Steps: (1) build the base image locally (ghcr.io/sossiogll/musizeer-provider-base:6; it is
# a name, never pushed here), (2) build the provider image, (3) ask the provider itself for its
# org.musizeer.provider.* labels (the image describes itself: name, roles, contract version,
# permissions) and build again with them, (4) push, (5) print the digest and the entry for a
# catalog (docs/catalog.md; the local registry is plain HTTP, so the source that lists it is added with
# "allow insecure registry").
#
# PROVIDER_LABELS_OUT=<file>  also write the image's labels there (the org.musizeer.provider.* ones and
#                             the OCI description and url, if the Dockerfile sets them): what
#                             scripts/dev-catalog.sh makes a catalog entry from.
set -eu

[ $# -ge 2 ] || { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
PROVIDER_DIR=$1
REGISTRY=$2
TAG=${3:-dev}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BASE=${PROVIDER_BASE_IMAGE:-ghcr.io/sossiogll/musizeer-provider-base:6}
DIRNAME=$(basename "$PROVIDER_DIR")
LOCAL=${PROVIDER_LOCAL_TAG_PREFIX:-musizeer-provider-}$DIRNAME:build
LABELS=$(mktemp)
trap 'rm -f "$LABELS"' EXIT

case "$REGISTRY" in
  ghcr.io*|docker.io*|index.docker.io*) echo "refusing to push to $REGISTRY from this script" >&2; exit 2 ;;
esac

echo "== base image $BASE" >&2
docker build -q --provenance=false -f "$ROOT/provider-base/Dockerfile" -t "$BASE" "$ROOT/sdk" >/dev/null

echo "== $LOCAL" >&2
docker build -q --provenance=false -t "$LOCAL" "$PROVIDER_DIR" >/dev/null
docker run --rm "$LOCAL" --print-labels >"$LABELS"

# The repository is named after the provider, as the provider itself says (not after its directory).
NAME=$(sed -n 's/^org.musizeer.provider.name=//p' "$LABELS")
[ -n "$NAME" ] || { echo "the provider printed no name label" >&2; exit 1; }
REMOTE=$REGISTRY/musizeer-provider-$NAME:$TAG

set --
while IFS= read -r line; do
  set -- "$@" --label "$line"
done <"$LABELS"
echo "== $REMOTE" >&2
sed 's/^/   /' "$LABELS" >&2
docker build -q --provenance=false "$@" -t "$REMOTE" "$PROVIDER_DIR" >/dev/null

PUSHED=$(docker push "$REMOTE" 2>&1 | tee /dev/stderr | sed -n 's/.*digest: \(sha256:[0-9a-f]\{64\}\).*/\1/p' | tail -1)
[ -n "$PUSHED" ] || { echo "could not read the digest of the pushed image" >&2; exit 1; }

INSTALL_AS=${PROVIDER_INSTALL_REGISTRY:-$REGISTRY}
if [ -n "${PROVIDER_LABELS_OUT:-}" ]; then
  {
    cat "$LABELS"
    for key in org.opencontainers.image.description org.opencontainers.image.url; do
      value=$(docker inspect -f "{{index .Config.Labels \"$key\"}}" "$REMOTE")
      [ -z "$value" ] || printf '%s=%s\n' "$key" "$value"
    done
  } >"$PROVIDER_LABELS_OUT"
fi
label() { sed -n "s/^org.musizeer.provider.$1=//p" "$LABELS"; }
ROLES=$(label roles | sed 's/,/", "/g; s/^/["/; s/$/"]/')
echo "$PUSHED"
cat >&2 <<JSON

catalog entry (version 2, docs/catalog.md; scripts/dev-catalog.sh writes it for you, with the label and description of the image). The registry is local: add the source that lists it with "allow insecure registry", and set PROVIDER_INSTALL_REGISTRY when the gateway reaches the registry by another name than $REGISTRY:
  {"name": "$NAME", "image": "$INSTALL_AS/musizeer-provider-$NAME", "digest": "$PUSHED", "provider_api_version": $(label api-version), "roles": $ROLES, "label": "$NAME", "description": "", "permissions": $(label permissions)}
JSON

#!/bin/sh
# Publish the base image of the provider images to GHCR, for linux/amd64 and linux/arm64.
#
#   scripts/publish-provider-base.sh
#
# Tags: <namespace>/musizerr-provider-base:<contract> (the provider API version, the major version of
# the SDK: what every provider Dockerfile names in FROM) and :<SDK version>. Prints the digest of the
# multi-architecture index.
#
#   GHCR_NAMESPACE   where to publish (default ghcr.io/sossiogll)
#
# Needs `docker login ghcr.io` with a token that may write packages (GitHub: write:packages). A new
# package is private and stays so: its visibility is changed on GitHub, never here. The image carries
# org.opencontainers.image.source, which links the package to the repository it is built from.
# Builds with the buildx builder musizerr-publish (docker-container driver: the one that pushes an image
# for several platforms), created when missing. scripts/build-provider-image.sh, for images under test,
# never pushes here.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAMESPACE=${GHCR_NAMESPACE:-ghcr.io/sossiogll}
BUILDER=musizerr-publish
PLATFORMS=linux/amd64,linux/arm64

SDK_VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/sdk/pyproject.toml" | head -1)
CONTRACT=${SDK_VERSION%%.*}
[ -n "$SDK_VERSION" ] && [ -n "$CONTRACT" ] || { echo "cannot read the SDK version from sdk/pyproject.toml" >&2; exit 1; }
IMAGE=$NAMESPACE/musizerr-provider-base

docker buildx inspect "$BUILDER" >/dev/null 2>&1 || docker buildx create --name "$BUILDER" --driver docker-container >/dev/null

# GHCR reads the repository, the description and the license of a multi-platform image from the
# annotations of its index: the same values as the labels of the Dockerfile.
set --
for key in org.opencontainers.image.source org.opencontainers.image.description org.opencontainers.image.licenses; do
  value=$(sed -n "s|.*$key=\"\([^\"]*\)\".*|\1|p" "$ROOT/provider-base/Dockerfile" | head -1)
  [ -z "$value" ] || set -- "$@" --annotation "index:$key=$value"
done

META=$(mktemp)
trap 'rm -f "$META"' EXIT
echo "== $IMAGE:$CONTRACT and :$SDK_VERSION ($PLATFORMS)" >&2
docker buildx build --builder "$BUILDER" --platform "$PLATFORMS" --provenance=false --sbom=false \
  -f "$ROOT/provider-base/Dockerfile" -t "$IMAGE:$CONTRACT" -t "$IMAGE:$SDK_VERSION" "$@" \
  --metadata-file "$META" --push "$ROOT/sdk" >&2

DIGEST=$(sed -n 's/.*"containerimage.digest": *"\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$META" | head -1)
[ -n "$DIGEST" ] || { echo "could not read the digest of the pushed index" >&2; exit 1; }
echo "$DIGEST"

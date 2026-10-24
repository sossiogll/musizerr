#!/bin/sh
# Publish a provider image to GHCR, for linux/amd64 and linux/arm64, and write its catalog entry.
#
#   scripts/publish-provider-image.sh <provider-dir> <catalog-file> [catalog-name]
#
#   <provider-dir>  a directory with a Dockerfile FROM the base image and a pyproject.toml (its version
#                   is the image's tag); it may be anywhere on this machine
#   <catalog-file>  the catalog version 2 to write the entry in (docs/catalog.md): created when missing,
#                   the entry of the same name replaced where it stands, the others kept
#   [catalog-name]  the catalog's name, when this creates it
#
#   GHCR_NAMESPACE  where to publish (default ghcr.io/sossiogll)
#
# Steps: (1) pull the PUBLISHED base image (scripts/publish-provider-base.sh) and build the provider on
# it for this machine, (2) ask the provider for its org.musizeer.provider.* labels (the image describes
# itself), (3) build it with them for both platforms and push <namespace>/musizeer-provider-<name>:<version>,
# (4) write the entry: the image pinned by the digest of the multi-architecture index (the gateway picks
# its platform's manifest from it and checks the labels against the entry).
#
# Needs `docker login ghcr.io` with a token that may write packages. A new package is private and stays
# so (set org.opencontainers.image.source in the Dockerfile to link it to its repository). The entry is
# written by a throwaway python:3.12-slim container (scripts/dev_catalog.py): nothing runs on the host.
set -eu

[ $# -ge 2 ] || { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
PROVIDER_DIR=$(cd "$1" && pwd)
CATALOG_FILE=$2
CATALOG_NAME=${3:-Musizeer providers}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAMESPACE=${GHCR_NAMESPACE:-ghcr.io/sossiogll}
BUILDER=musizeer-publish
PLATFORMS=linux/amd64,linux/arm64

[ -f "$PROVIDER_DIR/Dockerfile" ] || { echo "$PROVIDER_DIR has no Dockerfile" >&2; exit 1; }
VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$PROVIDER_DIR/pyproject.toml" 2>/dev/null | head -1)
[ -n "$VERSION" ] || { echo "cannot read the version from $PROVIDER_DIR/pyproject.toml" >&2; exit 1; }
BASE=$(sed -n 's/^FROM \([^ ]*\).*/\1/p' "$PROVIDER_DIR/Dockerfile" | head -1)
case $BASE in
  "$NAMESPACE"/musizeer-provider-base:*) ;;
  *) echo "the Dockerfile is not FROM $NAMESPACE/musizeer-provider-base (it names $BASE)" >&2; exit 1 ;;
esac
mkdir -p "$(dirname "$CATALOG_FILE")"
CATALOG_DIR=$(cd "$(dirname "$CATALOG_FILE")" && pwd)
CATALOG_BASENAME=$(basename "$CATALOG_FILE")

TMP=$(mktemp -d)
LOCAL=musizeer-provider-publish-$(basename "$PROVIDER_DIR"):build
trap 'rm -rf "$TMP"; docker image rm -f "$LOCAL" >/dev/null 2>&1 || true' EXIT

echo "== base $BASE (published)" >&2
docker pull -q "$BASE" >/dev/null
docker build -q --provenance=false -t "$LOCAL" "$PROVIDER_DIR" >/dev/null
docker run --rm "$LOCAL" --print-labels >"$TMP/labels"

NAME=$(sed -n 's/^org.musizeer.provider.name=//p' "$TMP/labels")
[ -n "$NAME" ] || { echo "the provider printed no name label" >&2; exit 1; }
IMAGE=$NAMESPACE/musizeer-provider-$NAME

set --
while IFS= read -r line; do
  set -- "$@" --label "$line"
done <"$TMP/labels"
for key in org.opencontainers.image.description org.opencontainers.image.url; do
  value=$(docker inspect -f "{{index .Config.Labels \"$key\"}}" "$LOCAL")
  [ -z "$value" ] || printf '%s=%s\n' "$key" "$value" >>"$TMP/labels"
done
# GHCR reads the repository, the description and the license of a multi-platform image from the
# annotations of its index, not from the labels of each platform's image: without them the package is
# linked to no repository.
for key in org.opencontainers.image.source org.opencontainers.image.description org.opencontainers.image.licenses; do
  value=$(docker inspect -f "{{index .Config.Labels \"$key\"}}" "$LOCAL")
  [ -z "$value" ] || set -- "$@" --annotation "index:$key=$value"
done

docker buildx inspect "$BUILDER" >/dev/null 2>&1 || docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
echo "== $IMAGE:$VERSION ($PLATFORMS)" >&2
docker buildx build --builder "$BUILDER" --platform "$PLATFORMS" --provenance=false --sbom=false \
  "$@" -t "$IMAGE:$VERSION" --metadata-file "$TMP/meta.json" --push "$PROVIDER_DIR" >&2

DIGEST=$(sed -n 's/.*"containerimage.digest": *"\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$TMP/meta.json" | head -1)
[ -n "$DIGEST" ] || { echo "could not read the digest of the pushed index" >&2; exit 1; }

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$ROOT/scripts/dev_catalog.py":/dev_catalog.py:ro -v "$CATALOG_DIR":/catalogs -v "$TMP":/work:ro \
  python:3.12-slim python /dev_catalog.py --catalog "/catalogs/$CATALOG_BASENAME" \
    --image "$IMAGE" --digest "$DIGEST" --labels /work/labels --catalog-name "$CATALOG_NAME" >&2
echo "$DIGEST"

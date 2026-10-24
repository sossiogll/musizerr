#!/bin/sh
# Build provider images and list them in the development catalog the `registry` service of
# docker-compose.dev.yml serves: build, push to the local registry, write the entry.
#
#   scripts/dev-catalog.sh [-c NAME] [-r HOST:PORT] <provider-dir>...
#
#   <provider-dir>  one or more directories with a Dockerfile FROM the base image (see
#                   scripts/build-provider-image.sh). They may be anywhere on this machine,
#                   outside this repository included.
#   -c NAME         the catalog file, data/catalogs/NAME.json (default: registry, which the
#                   registry service serves as http://registry:9000/registry.json)
#   -r HOST:PORT    the registry to push to as THIS machine reaches it (default
#                   127.0.0.1:${MUSIZEER_DEV_OCI_REGISTRY_PORT:-15000}, the oci-registry service)
#
# Running it again for the same provider rebuilds the image and moves the entry's digest on; the
# entries of other providers in the file are kept. The entries name the image
# oci-registry:5000/musizeer-provider-<name> (how the gateway reaches the registry; set
# PROVIDER_INSTALL_REGISTRY for another name), so the source is added with "allow insecure registry".
#
# Needs Docker. The catalog is written by a throwaway python:3.12-slim container (scripts/dev_catalog.py):
# nothing runs on the host. Nothing is pushed anywhere but the local registry.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CATALOG=registry
REGISTRY=127.0.0.1:${MUSIZEER_DEV_OCI_REGISTRY_PORT:-15000}
while getopts "c:r:h" option; do
  case $option in
    c) CATALOG=$OPTARG ;;
    r) REGISTRY=$OPTARG ;;
    *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[ $# -ge 1 ] || { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
case $CATALOG in
  *[!A-Za-z0-9_-]*|'') echo "the catalog name is letters, digits, - and _ only" >&2; exit 2 ;;
esac

INSTALL_REGISTRY=${PROVIDER_INSTALL_REGISTRY:-oci-registry:5000}
CATALOG_DIR=${DEV_CATALOG_DIR:-$ROOT/data/catalogs}
mkdir -p "$CATALOG_DIR"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

for DIR in "$@"; do
  [ -f "$DIR/Dockerfile" ] || { echo "$DIR has no Dockerfile" >&2; exit 1; }
  echo "== $DIR" >&2
  LABELS=$TMP/labels
  DIGEST=$(PROVIDER_LABELS_OUT=$LABELS PROVIDER_INSTALL_REGISTRY=$INSTALL_REGISTRY "$ROOT/scripts/build-provider-image.sh" "$DIR" "$REGISTRY" 2>/dev/null)
  NAME=$(sed -n 's/^org.musizeer.provider.name=//p' "$LABELS")
  [ -n "$NAME" ] && [ -n "$DIGEST" ] || { echo "the build of $DIR printed no name or digest" >&2; exit 1; }
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$ROOT/scripts/dev_catalog.py":/dev_catalog.py:ro -v "$CATALOG_DIR":/catalogs -v "$TMP":/work:ro \
    python:3.12-slim python /dev_catalog.py --catalog "/catalogs/$CATALOG.json" \
      --image "$INSTALL_REGISTRY/musizeer-provider-$NAME" --digest "$DIGEST" --labels /work/labels
done
echo "catalog: $CATALOG_DIR/$CATALOG.json" >&2

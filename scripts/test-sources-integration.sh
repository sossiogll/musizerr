#!/bin/sh
# Integration test of sources and catalogs (ADR-0006, phase 4): a real catalog server, a real registry, the
# real gateway and the host's own code (the maintenance command, the registry, the sync).
#
#   scripts/test-sources-integration.sh
#
# Needs Docker (with user namespaces in its kernel), network access to lrclib.net and a checkout of the public
# providers repository (PROVIDERS_REPO, default ../musizerr-providers: only read; its lrclib/ is the provider
# installed). It uses its own compose project, `mzp4` (INTEGRATION_PROJECT), publishes only 127.0.0.1:18600
# (INTEGRATION_REGISTRY_PORT; a local registry) and tears everything down at the end. It does not touch any
# other container, volume or image, and pushes nothing outside the local
# registry it starts.
#
# It checks that:
#   a. a source added from the command line (the dev catalog format, version 2) lists the lyrics provider's image;
#      installing it shows the permissions, records image@digest, tells the gateway, and lyrics come back
#      end to end through the host's registry;
#   b. uninstalling stops the provider (no sandbox process is left) and the registry no longer has it;
#   c. an update whose digest adds an egress host to the image's labels but whose catalog entry does not
#      say so is applied (the admin saw no difference) and REFUSED by the gateway, with the reason;
#   d. when the catalog does say so, `update --dry-run` shows the difference and, approved, the gateway
#      runs the new image;
#   e. a gateway that restarted is told the installed providers again by the host's watcher.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
P=${INTEGRATION_PROJECT:-mzp4}
REGISTRY_PORT=${INTEGRATION_REGISTRY_PORT:-18600}
PROVIDERS_REPO=${PROVIDERS_REPO:-$ROOT/../musizerr-providers}
[ -d "$PROVIDERS_REPO/lrclib" ] || { echo "PROVIDERS_REPO ($PROVIDERS_REPO) is not a checkout of the public providers repository (no lrclib/)" >&2; exit 2; }
TMP=$(mktemp -d)
CATALOG=$TMP/catalog
mkdir -p "$CATALOG" "$TMP/hostdata"
chmod 777 "$TMP/hostdata"

export POSTGRES_PASSWORD=unused
export AUTH_SECRET=integration-test-secret-of-at-least-32-characters
export CREDENTIALS_KEY=3ER8zf6Vc8gFp5cbk2OqSEx_sA1KfLVT3q0ByU7bFHQ= # a key for these throwaway stacks only
export MZP4_CATALOG_DIR=$CATALOG
export OCI_REGISTRY_PORT=$REGISTRY_PORT
export PROVIDER_LOCAL_TAG_PREFIX=$P-
export PROVIDER_INSTALL_REGISTRY=oci-registry:5000
COMPOSE="docker compose -p $P -f docker-compose.dev.yml -f gateway/tests/integration/compose.sources.yml"
HOST_IMAGE=${HOST_IMAGE:-musizerr-app}   # the application's image: only its Python and dependencies are used

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- gateway log ----"; $COMPOSE logs --no-color --tail 80 provider-gateway 2>&1 || true
  fi
  docker rm -f $P-watcher >/dev/null 2>&1 || true
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  for image in ghcr.io/sossiogll/musizerr-provider-base:5 $P-lrclib:build $P-lrclib-wide:build \
      127.0.0.1:$REGISTRY_PORT/musizerr-provider-lrclib:dev 127.0.0.1:$REGISTRY_PORT/musizerr-provider-lrclib:wide $P-provider-gateway; do
    docker rmi "$image" >/dev/null 2>&1 || true
  done
  rm -rf "$TMP"
  exit $status
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
die() { echo "FAILED: $*" >&2; exit 1; }
wait_until() { # wait_until <seconds> <description> <command...>
  limit=$1; what=$2; shift 2
  i=0
  while ! "$@" >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -le $limit ] || die "timed out waiting for $what"
    sleep 1
  done
}
ctl() { $COMPOSE exec -T provider-gateway python -m musizerr_gateway.ctl "$@"; }
# The host's code, in a container of the application image, with the same sockets and library as the app has.
host() { # host <python command...>
  docker run --rm -i ${HOST_NAME:+--name $HOST_NAME} --network ${P}_default --user 1000:1000 \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/repo/app:/sdk/src -e DATA_DIR=/data -e AUTH_SECRET -e CREDENTIALS_KEY -e REDIS_URL=redis://redis:6379/0 \
    -v "$ROOT":/repo:ro -v "$ROOT/sdk":/sdk:ro -v "$TMP/hostdata":/data -v ${P}_provider_sockets:/run/musizerr -v ${P}_downloads:/downloads \
    -w /repo/app "$HOST_IMAGE" python "$@"
}
sources() { host -m scripts.provider_sources "$@"; }
label() { docker run --rm "$1" --print-labels | sed -n "s/^org.musizerr.provider.$2=//p"; }
write_catalog() { # write_catalog <digest> <permissions json>
  roles=$(label $P-lrclib:build roles | sed 's/,/", "/g; s/^/["/; s/$/"]/')
  cat > "$CATALOG/registry.json" <<JSON
{"name": "Integration catalog", "version": 2, "providers": [
  {"name": "lrclib", "image": "oci-registry:5000/musizerr-provider-lrclib", "digest": "$1",
   "provider_api_version": $(label $P-lrclib:build api-version), "roles": $roles, "label": "LRCLIB",
   "description": "Lyrics from lrclib.net", "permissions": $2}]}
JSON
}
state_of_lrclib() { ctl list | tr -d '\n ' | sed -n 's/.*"name":"lrclib"[^}]*"state":"\([a-z]*\)".*/\1/p'; }
state_is() { [ "$(state_of_lrclib)" = "$1" ]; }
no_providers() { ctl list | grep -q '"providers": \[\]'; }

step "starting the registry, the catalog server, redis and the gateway (compose project $P)"
printf '{"name": "Integration catalog", "version": 2, "providers": []}' > "$CATALOG/registry.json"   # until the image exists
$COMPOSE up -d --build oci-registry registry redis provider-gateway
wait_until 60 "the gateway's control socket" ctl health
ctl list | grep -q '"providers": \[\]' || die "the gateway runs something before the host told it anything"
echo "the gateway starts with no providers"

step "building the LRCLIB image and a variant whose labels add an egress host"
LRCLIB=$(scripts/build-provider-image.sh "$PROVIDERS_REPO/lrclib" 127.0.0.1:$REGISTRY_PORT 2>/dev/null)
cp -r "$PROVIDERS_REPO/lrclib" "$TMP/lrclib-wide"
sed 's|Permissions(egress=("lrclib.net:443",))|Permissions(egress=("lrclib.net:443", "extra.example:443"))|' "$PROVIDERS_REPO/lrclib/musizerr_provider_lrclib/__init__.py" > "$TMP/wide.py"
mv "$TMP/wide.py" "$TMP/lrclib-wide/musizerr_provider_lrclib/__init__.py"
grep -q extra.example "$TMP/lrclib-wide/musizerr_provider_lrclib/__init__.py" || die "the variant was not made"
WIDE=$(scripts/build-provider-image.sh "$TMP/lrclib-wide" 127.0.0.1:$REGISTRY_PORT wide 2>/dev/null)
echo "lrclib $LRCLIB"
echo "wide   $WIDE"
[ "$LRCLIB" != "$WIDE" ] || die "the two images have the same digest"
NARROW='{"egress": ["lrclib.net:443"]}'
BROAD='{"egress": ["lrclib.net:443", "extra.example:443"]}'
write_catalog "$LRCLIB" "$NARROW"

step "a. add the catalog as a source and install LRCLIB from it"
sources add-source http://registry:9000/registry.json --insecure
sources catalog "Integration catalog"
sources install "Integration catalog" lrclib --dry-run | tee "$TMP/dry.txt"
grep -q "network: lrclib.net:443" "$TMP/dry.txt" || die "the install did not show the permissions"
sources install "Integration catalog" lrclib --yes
wait_until 180 "lrclib to run" state_is running
ctl list | grep -E '"(name|state|image)"' | head -3
host /repo/gateway/tests/integration/sources_host_check.py lyrics | grep -E '^(LYRICS_OK|FAILED)' | tee "$TMP/lyrics.txt"
grep -q '^LYRICS_OK' "$TMP/lyrics.txt" || die "no lyrics came back through the host"
sources list

step "b. uninstalling stops the provider"
sources uninstall lrclib
wait_until 30 "the gateway to stop lrclib" no_providers
if $COMPOSE top provider-gateway 2>/dev/null | grep -q "musizerr_sdk.runner"; then die "a provider process is still running"; fi
echo "no provider process is left in the gateway"
host /repo/gateway/tests/integration/sources_host_check.py absent | grep -E '^(ABSENT_OK|FAILED)'

step "c. an update whose labels add an egress host that the catalog does not declare is refused by the gateway"
sources install "Integration catalog" lrclib --yes >/dev/null
wait_until 60 "lrclib to run again" state_is running
write_catalog "$WIDE" "$NARROW"
sources update lrclib --dry-run | tee "$TMP/update-honest-looking.txt"
grep -q "the permissions are the same" "$TMP/update-honest-looking.txt" || die "the catalog's own entry was expected to look unchanged"
sources update lrclib --yes
wait_until 120 "the gateway to refuse the image" state_is refused
ctl list | tr -d '\n' | grep -o '"reason": "the image declares permissions that are not the ones approved[^"]*"' | head -1 | tee "$TMP/refusal.txt"
[ -s "$TMP/refusal.txt" ] || die "the gateway did not say why it refused the image"
echo "REFUSED_OK"

step "d. when the catalog declares the new host, the difference is shown and, approved, the image runs"
write_catalog "$WIDE" "$BROAD"
sources update lrclib --dry-run | tee "$TMP/update-diff.txt"
grep -q "+ network: extra.example:443" "$TMP/update-diff.txt" || die "the difference was not shown"
grep -q "asks for MORE" "$TMP/update-diff.txt" || die "the update was not flagged as asking for more"
sources update lrclib --yes
wait_until 120 "the approved image to run" state_is running
echo "UPDATE_APPROVED_OK"
host /repo/gateway/tests/integration/sources_host_check.py lyrics | grep -E '^(LYRICS_OK|FAILED)'

step "e. a gateway that restarted is told the installed providers again by the host's watcher"
$COMPOSE restart provider-gateway >/dev/null
wait_until 60 "the gateway's control socket" ctl health
ctl list | grep -q '"providers": \[\]' || die "a restarted gateway runs something nobody told it"
echo "the restarted gateway runs nothing"
# The API's watcher (services/provider_events.GatewayWatcher) is what pushes the set; run it as the API does.
HOST_NAME=$P-watcher host - <<'PY' >/dev/null &
import time
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync
from services.provider_events import GatewayWatcher

class Registry:
    gateway_key = None

watcher = GatewayWatcher(Registry(), GatewayClient(), lambda: None, interval=1.0, sync=GatewaySync()).start()
time.sleep(60)
PY
WATCHER=$!
wait_until 120 "the watcher to bring lrclib back" state_is running
kill $WATCHER 2>/dev/null || true
docker rm -f $P-watcher >/dev/null 2>&1 || true
echo "WATCHER_OK"

step "all checks passed"

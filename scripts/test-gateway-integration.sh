#!/bin/sh
# Integration test of the provider gateway: real images, a real sandbox, a real registry.
#
#   scripts/test-gateway-integration.sh
#
# Needs Docker (with user namespaces in its kernel), network access to lrclib.net and a checkout of the public
# providers repository (PROVIDERS_REPO, default ../musizerr-providers: only read; its lrclib/ is the provider
# installed). It uses its own compose project, `mzp3` (INTEGRATION_PROJECT), publishes only 127.0.0.1:18500
# (INTEGRATION_REGISTRY_PORT; a local registry) and tears everything down at the end. It does not touch any
# other container, volume or image, and pushes nothing outside the local registry it starts.
#
# It checks that:
#   a. lyrics come back end to end from the lyrics provider's image, through the gateway, via the
#      host's registry (the application's own code, in a container of the application image);
#   b. a provider installed later reaches that registry through the providers-changed event
#      (Redis), and a hostile provider image (gateway/tests/fixtures/hostile-provider) that
#      tries to read /data, the environment, /proc/1/environ, files outside its workspace, an
#      undeclared host and a direct connection fails at every attempt, with the refused
#      egress in the gateway's log;
#   c. what a setting names is reachable while it is set and not after;
#   d. with Docker's DEFAULT seccomp profile in place of the gateway's, the provider does not
#      run and the gateway's health says why (it fails closed).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
P=${INTEGRATION_PROJECT:-mzp3}
REGISTRY_PORT=${INTEGRATION_REGISTRY_PORT:-18500}
PROVIDERS_REPO=${PROVIDERS_REPO:-$ROOT/../musizerr-providers}
[ -d "$PROVIDERS_REPO/lrclib" ] || { echo "PROVIDERS_REPO ($PROVIDERS_REPO) is not a checkout of the public providers repository (no lrclib/)" >&2; exit 2; }
TMP=$(mktemp -d)
export POSTGRES_PASSWORD=unused
export AUTH_SECRET=integration-test-secret-of-at-least-32-characters
export CREDENTIALS_KEY=3ER8zf6Vc8gFp5cbk2OqSEx_sA1KfLVT3q0ByU7bFHQ= # a key for these throwaway stacks only
export OCI_REGISTRY_PORT=$REGISTRY_PORT
export PROVIDER_LOCAL_TAG_PREFIX=$P-
export PROVIDER_INSTALL_REGISTRY=oci-registry:5000
COMPOSE="docker compose -p $P -f docker-compose.dev.yml -f gateway/tests/integration/compose.override.yml"
HOST_IMAGE=${HOST_IMAGE:-musizerr-app}   # the application's image: only its Python and dependencies are used

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- gateway log ----"; $COMPOSE logs --no-color --tail 80 provider-gateway 2>&1 || true
    echo "---- host check log ----"; docker logs $P-host 2>&1 | tail -60 || true
    echo "---- default-seccomp gateway log ----"; docker logs $P-gw-default 2>&1 | tail -30 || true
  fi
  docker rm -f $P-host $P-gw-default >/dev/null 2>&1 || true
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  docker volume rm $P-df-run $P-df-state $P-df-ws >/dev/null 2>&1 || true
  for image in ghcr.io/sossiogll/musizerr-provider-base:5 $P-lrclib:build $P-hostile-provider:build \
      127.0.0.1:$REGISTRY_PORT/musizerr-provider-lrclib:dev 127.0.0.1:$REGISTRY_PORT/musizerr-provider-hostile:dev $P-provider-gateway; do
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
# What the host would push for an image it was told to install: the roles and permissions its labels declare,
# standing in for what the admin approved.
label() { docker run --rm "$1" --print-labels | sed -n "s/^org.musizerr.provider.$2=//p"; }
installed_entry() { # <name> <digest> <local image>
  roles=$(label "$3" roles | sed 's/,/","/g; s/^/["/; s/$/"]/')
  printf '{"name": "%s", "image": "oci-registry:5000/musizerr-provider-%s", "digest": "%s", "roles": %s, "permissions": %s, "insecure": true}' \
    "$1" "$1" "$2" "$roles" "$(label "$3" permissions)"
}
# Push a desired set the way the host does (op install); $1 is the container, the entries are on stdin.
push() { docker exec -i "$1" python -m musizerr_gateway.ctl install; }

step "starting the registry, redis and the gateway (compose project $P, seccomp profile gateway/seccomp/musizerr-gateway.json)"
$COMPOSE up -d --build oci-registry redis provider-gateway
wait_until 60 "the gateway's control socket" ctl health

step "building the images and pushing them to the local registry"
LRCLIB=$(scripts/build-provider-image.sh "$PROVIDERS_REPO/lrclib" 127.0.0.1:$REGISTRY_PORT 2>/dev/null)
HOSTILE=$(scripts/build-provider-image.sh gateway/tests/fixtures/hostile-provider 127.0.0.1:$REGISTRY_PORT 2>/dev/null)
echo "lrclib  $LRCLIB"
echo "hostile $HOSTILE"
echo "{\"providers\": [$(installed_entry lrclib "$LRCLIB" $P-lrclib:build)]}" | push $P-provider-gateway-1 >/dev/null
wait_until 180 "lrclib to run" sh -c "$COMPOSE exec -T provider-gateway python -m musizerr_gateway.ctl list | grep -q '\"state\": \"running\"'"
ctl list | grep -E '"(name|state|image)"' | head -4

step "a. the host's registry gets lyrics from the LRCLIB image through the gateway"
docker run -d --name $P-host --network ${P}_default --user 1000:1000 \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/repo/app:/sdk/src -e DATA_DIR=/tmp/data -e AUTH_SECRET -e CREDENTIALS_KEY -e REDIS_URL=redis://redis:6379/0 \
  -v "$ROOT":/repo:ro -v "$ROOT/sdk":/sdk:ro -v ${P}_provider_sockets:/run/musizerr -v ${P}_downloads:/downloads \
  -w /repo/app "$HOST_IMAGE" python /repo/gateway/tests/integration/host_check.py >/dev/null
wait_until 120 "the host check to be ready" sh -c "docker logs $P-host 2>&1 | grep -q '^READY'"
docker logs $P-host 2>&1 | grep -E '^(REGISTRY|LYRICS_OK)'

step "b. a hostile provider is installed after the host started"
echo "{\"providers\": [$(installed_entry lrclib "$LRCLIB" $P-lrclib:build), $(installed_entry hostile "$HOSTILE" $P-hostile-provider:build)]}" | push $P-provider-gateway-1 >/dev/null
docker wait $P-host >/dev/null
CODE=$(docker inspect -f '{{.State.ExitCode}}' $P-host)
docker logs $P-host 2>&1 | grep -vE '^(REGISTRY|LYRICS_OK|READY)' | sed -n '1,80p'
[ "$CODE" = 0 ] || die "the host check failed (exit $CODE)"
docker logs $P-host 2>&1 | grep -q '^RELOADED' || die "the registry never learned of the new provider"
docker logs $P-host 2>&1 | grep -q '^HOSTILE_OK' || die "the hostile provider was not contained"
docker logs $P-host 2>&1 | grep -q '^GRANT_OK' || die "the grant did not follow the setting"

step "the refused egress is in the gateway's log"
$COMPOSE logs --no-color provider-gateway 2>&1 | grep "egress refused" | sed 's/^[^|]*| //' | sort | uniq -c | sort -rn | head -20
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -q "egress refused: provider=hostile target=example.com:443" || die "no refusal of example.com:443 in the gateway log"
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -q "egress refused: provider=hostile target=10.0.0.1:80" || die "no refusal of 10.0.0.1:80 in the gateway log"

step "c. with Docker's default seccomp profile the provider does not run, and health says why"
docker run -d --name $P-gw-default --network ${P}_default --user 1000:1000 --cap-drop ALL \
  --security-opt systempaths=unconfined \
  -v $P-df-run:/run/musizerr -v $P-df-state:/var/lib/musizerr-gateway -v $P-df-ws:/downloads \
  $P-provider-gateway >/dev/null
wait_until 60 "the second gateway" docker exec $P-gw-default python -m musizerr_gateway.ctl health
echo "{\"providers\": [$(installed_entry lrclib "$LRCLIB" $P-lrclib:build)]}" | push $P-gw-default >/dev/null
sleep 8
docker exec $P-gw-default python -m musizerr_gateway.ctl health | tee "$TMP/health.json"
grep -q '"status": "unavailable"' "$TMP/health.json" || die "health does not say the sandbox is unavailable"
grep -q "No permissions to create a new namespace" "$TMP/health.json" || die "health does not say why"
docker exec $P-gw-default python -m musizerr_gateway.ctl list | grep -E '"(state|reason)"'
docker exec $P-gw-default python -m musizerr_gateway.ctl list | grep -q '"state": "unavailable"' || die "the provider is not marked unavailable"
if docker top $P-gw-default 2>/dev/null | grep -q "musizerr_sdk.runner"; then die "a provider process is running without a sandbox"; fi
echo "no provider process is running in $P-gw-default"

step "all checks passed"

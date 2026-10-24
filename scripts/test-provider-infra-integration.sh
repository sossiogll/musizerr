#!/bin/sh
# Integration test of what the gateway grants a provider besides fixed hosts (ADR-0006, phase 5.1): wildcard egress, raw TCP
# to the host a setting names (FTP, passive data connections on ports the server picks) and folders named by settings;
# and of scripts/dev-catalog.sh, which makes the catalog the provider is installed from.
#
#   scripts/test-provider-infra-integration.sh
#
# Needs Docker (with user namespaces in its kernel) and network access to example.com (the wildcard's positive case). It uses its
# own compose project, `mzp51`, publishes only 127.0.0.1:18700 (a local registry) and tears everything down at the end. It does
# not touch any other container, volume or image, and pushes nothing outside the local registry it starts.
#
# It checks that:
#   a. scripts/dev-catalog.sh builds a provider directory that is no provider of the providers repository, pushes it and writes a valid catalog entry
#      (running it again updates the entry), and that catalog installs the provider through the host's own command, with
#      the wildcard and the raw-TCP permission shown in words;
#   b. (gateway/tests/integration/infra_host_check.py, the application's own code against the real gateway and the sample
#      image gateway/tests/fixtures/infra-provider) a wildcard lets a subdomain through and not the domain, another port or
#      another domain; ftplib lists and downloads through the proxy with the data connection on a port the server picked, and
#      a setting whose manifest allows only the control port gets the data port refused; a folder granted by a setting is
#      bound at the same path (rw writable and visible on the host, ro really read-only), a changed setting restarts the
#      sandbox with the new bind, a path outside the accepted roots / a link out of the root / an unnormalized path is
#      refused with the reason, and the working folder is hidden inside a granted root. The root folders (ADR-0008) are
#      declared by the host and judged by the gateway's own mountinfo: a mounted volume is accepted, /etc and a missing
#      folder are refused, the picker (browse) lists the volume, and the provider's /workspace is its part of the working folder.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
P=mzp51
TMP=$(mktemp -d)
CATALOGS=$TMP/catalogs
mkdir -p "$CATALOGS" "$TMP/hostdata"
chmod 777 "$TMP/hostdata"

export POSTGRES_PASSWORD=unused
export MUSIZEER_AUTH_SECRET=integration-test-secret-of-at-least-32-characters
export MUSIZEER_CREDENTIALS_KEY=3ER8zf6Vc8gFp5cbk2OqSEx_sA1KfLVT3q0ByU7bFHQ= # a key for these throwaway stacks only
export MZP51_CATALOG_DIR=$CATALOGS
export MUSIZEER_DEV_OCI_REGISTRY_PORT=18700
export PROVIDER_LOCAL_TAG_PREFIX=mzp51-
export DEV_CATALOG_DIR=$CATALOGS
COMPOSE="docker compose -p $P -f docker-compose.dev.yml -f gateway/tests/integration/compose.infra.yml"
HOST_IMAGE=${HOST_IMAGE:-musizeer-app}   # the application's image: only its Python and dependencies are used

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- gateway log ----"; $COMPOSE logs --no-color --tail 120 provider-gateway 2>&1 || true
  fi
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  for image in ghcr.io/sossiogll/musizeer-provider-base:6 $P-infra-provider:build \
      127.0.0.1:18700/musizeer-provider-infra:dev $P-provider-gateway; do
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
ctl() { $COMPOSE exec -T provider-gateway python -m musizeer_gateway.ctl "$@"; }
# The host's code, in a container of the application image, with the same sockets and library as the app has.
host() { # host <python command...>
  docker run --rm -i --network ${P}_default --user 1000:1000 \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/repo/app:/sdk/src -e MUSIZEER_DATA_DIR=/data -e MUSIZEER_AUTH_SECRET -e MUSIZEER_CREDENTIALS_KEY \
    -v "$ROOT":/repo:ro -v "$ROOT/sdk":/sdk:ro -v "$TMP/hostdata":/data -v ${P}_provider_sockets:/run/musizeer -v ${P}_downloads:/downloads \
    -w /repo/app "$HOST_IMAGE" python "$@"
}
sources() { host -m scripts.provider_sources "$@"; }
infra_running() { ctl list | tr -d '\n ' | grep -q '"name":"infra"[^}]*"state":"running"'; }

step "starting the registry, the catalog server, the gateway and a fake FTP server (compose project $P)"
printf '{"name": "Musizeer development catalog", "version": 2, "providers": []}' > "$CATALOGS/registry.json"
$COMPOSE up -d --build oci-registry registry provider-gateway ftp-fake
wait_until 60 "the gateway's control socket" ctl health
ctl list | grep -q '"providers": \[\]' || die "the gateway runs something before the host told it anything"

step "a. scripts/dev-catalog.sh builds a provider directory that is no provider of the providers repository and writes its catalog entry"
scripts/dev-catalog.sh -r 127.0.0.1:18700 gateway/tests/fixtures/infra-provider 2>&1 | grep -E '^(added|updated|catalog:)'
scripts/dev-catalog.sh -r 127.0.0.1:18700 gateway/tests/fixtures/infra-provider 2>&1 | tee "$TMP/second.txt" | grep -E '^(added|updated)'
grep -q '^updated infra' "$TMP/second.txt" || die "running the script again did not update the entry"
[ "$(grep -c '"name": "infra"' "$CATALOGS/registry.json")" = 1 ] || die "the entry is in the catalog more than once"
for expected in '"image": "oci-registry:5000/musizeer-provider-infra"' '"label": "Infrastructure (test fixture)"' '"description": "Test fixture' \
    '"\*.example.com:443"' '"tcp_from_settings"' '"ports": "2121"' '"ports": "any"' '"filesystem_from_settings"'; do
  grep -q "$expected" "$CATALOGS/registry.json" || die "the catalog entry lacks $expected"
done
PUSHED=$(curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
  http://127.0.0.1:18700/v2/musizeer-provider-infra/manifests/dev | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p')
grep -q "\"digest\": \"$PUSHED\"" "$CATALOGS/registry.json" || die "the digest in the catalog is not the one in the registry ($PUSHED)"
echo "catalog entry digest = registry digest = $PUSHED"

step "a. the catalog the script wrote installs the provider through the host's own command"
sources add-source http://registry:9000/registry.json --insecure
sources install "Musizeer development catalog" infra --dry-run | tee "$TMP/dry.txt"
grep -q "network: \*.example.com:443 (any subdomain, not example.com itself)" "$TMP/dry.txt" || die "the wildcard was not shown in words"
grep -q "network: any port of the host the setting 'ftp_host' names" "$TMP/dry.txt" || die "the raw TCP permission was not shown"
grep -q "network: port 2121 of the host the setting 'ftp_ctl' names" "$TMP/dry.txt" || die "the single-port TCP permission was not shown"
grep -q "files: the folder the setting 'rw_dir' names (read and write)" "$TMP/dry.txt" || die "the folder permission was not shown"
sources install "Musizeer development catalog" infra --yes
wait_until 180 "the infra provider to run" infra_running
ctl list | grep -E '"(name|state)"' | head -2

step "b. wildcard egress, raw TCP and folders from settings, end to end"
host /repo/gateway/tests/integration/infra_host_check.py | tee "$TMP/check.txt"
for marker in WILDCARD_OK ROOTS_OK FS_BEFORE_OK FS_RW_OK FS_STABLE_OK FS_CHANGE_OK FS_REFUSED_OK FS_MASK_OK TCP_OK; do
  grep -q "^$marker" "$TMP/check.txt" || die "$marker is missing"
done

step "the refusals are in the gateway's log"
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -E "egress refused: provider=infra|folder of '|restarting with the folders" | sed 's/^[^|]*| //' | sort | uniq -c | sort -rn | head -20
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -q "egress refused: provider=infra target=example.com:443" || die "no refusal of the bare domain in the gateway log"
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -qE "egress refused: provider=infra target=ftp-fake-ctl:30[0-9]{3} reason=port" || die "no refusal of the data port in the gateway log"
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -q "the folders its settings now name" || die "no restart for a change of folders in the gateway log"

step "all checks passed"

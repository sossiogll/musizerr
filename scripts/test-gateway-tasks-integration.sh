#!/bin/sh
# Integration test of what bounds the processes and threads of a sandbox: real images, real sandboxes, and a second
# container that shares the gateway's uid and holds more tasks than the old `--nproc` allowed.
#
#   scripts/test-gateway-tasks-integration.sh
#
# Needs Docker (with user namespaces in its kernel). It uses its own compose project, `mzp6` (INTEGRATION_PROJECT),
# publishes only 127.0.0.1:18800 (INTEGRATION_REGISTRY_PORT; a local registry) and tears everything down at the end. It does
# not touch any other container, volume or image, and pushes nothing outside the local registry it starts.
#
# The bug it guards against: the kernel counts RLIMIT_NPROC over every process of the real uid on the WHOLE HOST, not per
# process or per sandbox. The gateway runs as uid 1000, the main account of a typical host, which owns the processes of other
# containers too; with 481 threads of them already there, `--nproc=512` made a runner of two threads fail with "can't start new
# thread" at the first burst. It checks that, with a container holding 700 threads as the same uid next to the gateway:
#   0. (the precondition) a process under `--nproc=512` in that uid cannot start a thread: the condition is really there;
#   a. the provider (gateway/tests/fixtures/tasks-provider, `limits.processes` = 100, the default memory limit) runs, its sandbox has no RLIMIT_NPROC of its
#      own, and it starts its threads (64 of them, with the default memory limit and the default stacks);
#   b. a provider that holds more tasks than its limit is stopped by the gateway (the kernel gave it every thread), with the
#      reason in `list` and in the log, and is run again;
#   c. in a gateway whose container has a small pids limit, a provider that cannot start a thread is reported ("process/thread
#      limit reached (RLIMIT_NPROC / pids)") with the numbers, and `list` shows the container's limit.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
P=${INTEGRATION_PROJECT:-mzp6}
REGISTRY_PORT=${INTEGRATION_REGISTRY_PORT:-18800}
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
    echo "---- pids-limited gateway log ----"; docker logs $P-gw-pids 2>&1 | tail -40 || true
  fi
  docker rm -f $P-hog $P-gw-pids >/dev/null 2>&1 || true
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  docker volume rm $P-pids-run $P-pids-state $P-pids-ws >/dev/null 2>&1 || true
  for image in ghcr.io/sossiogll/musizerr-provider-base:5 $P-tasks-provider:build \
      127.0.0.1:$REGISTRY_PORT/musizerr-provider-tasks:dev $P-provider-gateway; do
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
label() { docker run --rm "$1" --print-labels | sed -n "s/^org.musizerr.provider.$2=//p"; }
installed_entry() { # <name> <digest> <local image>
  roles=$(label "$3" roles | sed 's/,/","/g; s/^/["/; s/$/"]/')
  printf '{"name": "%s", "image": "oci-registry:5000/musizerr-provider-%s", "digest": "%s", "roles": %s, "permissions": %s, "insecure": true}' \
    "$1" "$1" "$2" "$roles" "$(label "$3" permissions)"
}
push() { docker exec -i "$1" python -m musizerr_gateway.ctl install; } # the entries are on stdin
# The host's code, in a container of the application image, with the socket volume of the gateway it is asked about.
host() { # host <run volume> <workspace volume> <mode>
  docker run --rm -i --network ${P}_default --user 1000:1000 \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/repo/app:/sdk/src -e DATA_DIR=/tmp/data -e AUTH_SECRET -e CREDENTIALS_KEY \
    -v "$ROOT":/repo:ro -v "$ROOT/sdk":/sdk:ro -v "$1":/run/musizerr -v "$2":/downloads \
    -w /repo/app "$HOST_IMAGE" python /repo/gateway/tests/integration/tasks_host_check.py "$3"
}
running() { ctl list | tr -d '\n ' | grep -q '"name":"tasks"[^}]*"state":"running"'; }

step "starting the registry and the gateway (compose project $P: user 1000, the pids_limit of docker-compose.dev.yml)"
$COMPOSE up -d --build oci-registry provider-gateway
wait_until 60 "the gateway's control socket" ctl health
ctl health | tee "$TMP/health.json"
grep -q '"pids_limit": 4096' "$TMP/health.json" || die "the gateway does not report the container's pids limit (4096 by default)"

step "building the image and pushing it to the local registry"
TASKS=$(scripts/build-provider-image.sh gateway/tests/fixtures/tasks-provider 127.0.0.1:$REGISTRY_PORT 2>/dev/null)
echo "tasks $TASKS"
ENTRY=$(installed_entry tasks "$TASKS" $P-tasks-provider:build)
printf '%s' "$ENTRY" | grep -q '"processes":100' || die "the image does not declare limits.processes = 100: $ENTRY"

step "0. another container holds 700 threads as uid 1000, the gateway's uid"
docker run -d --name $P-hog --user 1000:1000 "$HOST_IMAGE" \
  python -c "import threading, time; [threading.Thread(target=time.sleep, args=(900,), daemon=True).start() for _ in range(700)]; time.sleep(900)" >/dev/null
sleep 3
if docker run --rm --user 1000:1000 "$HOST_IMAGE" prlimit --nproc=512 -- python -c "import threading, time; threading.Thread(target=time.sleep, args=(1,)).start()" >/dev/null 2>&1; then
  die "the precondition does not hold: a process under --nproc=512 as uid 1000 can start a thread next to the container that holds 700"
fi
echo "precondition holds: under --nproc=512 a process of uid 1000 cannot start even one thread here"

step "a. the provider runs and starts its threads next to it"
echo "{\"providers\": [$ENTRY]}" | push $P-provider-gateway-1 >/dev/null
wait_until 180 "the provider to run" running
host ${P}_provider_sockets ${P}_downloads shared-uid | tee "$TMP/a.txt"
grep -q '^SHARED_UID_OK' "$TMP/a.txt" || die "the provider could not start its threads while another container held the uid's tasks"
grep -q '^THREADS_OK' "$TMP/a.txt" || die "fewer than 64 threads fit in a sandbox with the default memory limit"

step "b. a provider above its limit is stopped by the gateway, and the kernel did not stop it first"
host ${P}_provider_sockets ${P}_downloads watchdog | tee "$TMP/b.txt"
grep -q '^WATCHDOG_OK' "$TMP/b.txt" || die "the gateway did not stop the provider that held more tasks than its limit"
$COMPOSE logs --no-color provider-gateway 2>&1 | grep -q "tasks: process/thread limit reached: the sandbox held .* over its limit of 100" || die "the log does not say why"

step "c. in a gateway whose container has a pids limit of 70, a provider that cannot start a thread is reported with the numbers"
docker run -d --name $P-gw-pids --network ${P}_default --user 1000:1000 --cap-drop ALL --pids-limit 70 \
  --security-opt seccomp="$ROOT/gateway/seccomp/musizerr-gateway.json" --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
  -v $P-pids-run:/run/musizerr -v $P-pids-state:/var/lib/musizerr-gateway -v $P-pids-ws:/downloads \
  $P-provider-gateway >/dev/null
wait_until 60 "the pids-limited gateway" docker exec $P-gw-pids python -m musizerr_gateway.ctl health
echo "{\"providers\": [$ENTRY]}" | push $P-gw-pids >/dev/null
wait_until 180 "the provider to run in the pids-limited gateway" sh -c "docker exec $P-gw-pids python -m musizerr_gateway.ctl list | tr -d '\n ' | grep -q '\"name\":\"tasks\"[^}]*\"state\":\"running\"'"
host $P-pids-run $P-pids-ws pids | tee "$TMP/c.txt"
grep -q '^PIDS_OK' "$TMP/c.txt" || die "the provider that could not start a thread was not reported"
docker logs $P-gw-pids 2>&1 | grep "process/thread limit reached (RLIMIT_NPROC / pids)" | head -1 | sed 's/^/   /'
docker logs $P-gw-pids 2>&1 | grep -q "process/thread limit reached (RLIMIT_NPROC / pids)" || die "the gateway's log does not say the limit was reached"
docker logs $P-gw-pids 2>&1 | grep -q "container pids: [0-9]* of 70 (pids_limit)" || die "the gateway's log lacks the numbers of the container"

step "all checks passed"

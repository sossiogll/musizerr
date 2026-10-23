#!/bin/sh
# Test of the frontend image's nginx: builds the production image, runs it against throwaway upstreams and
# checks the proxy (docs/deployment.md, "The frontend").
#
#   scripts/test-frontend-image.sh
#
# Needs Docker and curl. It builds a throwaway image, starts containers on a throwaway network and publishes one
# port on 127.0.0.1 (FRONTEND_TEST_PORT, default 18480); all of it is removed at the end. Nothing else is touched.
#
# It checks that:
#   a. `nginx -t` passes in the image, and the template renders: its own variables are left alone;
#   b. /api/<path> reaches BACKEND_URL as /<path>, query string and encoded characters kept, with
#      X-Forwarded-Proto / X-Forwarded-Host (a proxy's value for the scheme is kept, a bogus one is not);
#   c. the progress stream is not buffered (the first event arrives long before the second), through the same rewrite;
#   d. the backend's address is resolved again: replacing the upstream container (a new address) needs no restart,
#      and neither does a backend that did not exist when nginx started;
#   e. BACKEND_URL names the backend: another service name works; a malformed one stops the container with the reason;
#   f. the single-page app is served, deep links fall back to index.html.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
P=${FRONTEND_TEST_PROJECT:-mzfe}
PORT=${FRONTEND_TEST_PORT:-18480}
IMAGE=$P-frontend:test
NET=$P-net
FRONT=$P-front
TMP=$(mktemp -d)

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then echo "---- frontend log ----"; docker logs "$FRONT" 2>&1 | tail -30 || true; fi
  docker rm -f "$FRONT" $P-up1 $P-up2 $P-bad >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$TMP"
  exit $status
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
die() { echo "FAILED: $*" >&2; exit 1; }
expect() { # expect <description> <text> <expected substring>
  printf '%s' "$2" | grep -q -- "$3" || die "$1: expected '$3' in: $2"
}
wait_until() { # wait_until <seconds> <description> <command...>
  limit=$1; what=$2; shift 2
  i=0
  while ! "$@" >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -le "$limit" ] || die "timed out waiting for $what"
    sleep 1
  done
}
upstream() { # upstream <container name> <server name> <network alias> [delay]
  docker run -d --name "$1" --network "$NET" --network-alias "$3" \
    -v "$ROOT/scripts/frontend_test_upstream.py":/srv/upstream.py:ro python:3.12-slim \
    python /srv/upstream.py "$2" 8000 "${4:-4}" >/dev/null
}
front() { # front <env args...>: the frontend under test
  docker rm -f "$FRONT" >/dev/null 2>&1 || true
  docker run -d --name "$FRONT" --network "$NET" -p 127.0.0.1:$PORT:80 "$@" "$IMAGE" >/dev/null
  wait_until 20 "nginx to answer" curl -fsS -o /dev/null "http://127.0.0.1:$PORT/"
}
get() { curl -fsS --max-time 15 "$@"; }

step "building the production image of the frontend"
docker build -q -t "$IMAGE" --target production musizerr-ui >/dev/null
docker network create "$NET" >/dev/null

step "a. nginx -t, and the variables of nginx survive the template"
docker run --rm "$IMAGE" nginx -t 2>&1 | tail -2
CONF=$(docker run --rm --entrypoint sh "$IMAGE" -c 'envsubst "\${BACKEND_URL} \${DNS_RESOLVER}" < /etc/nginx/templates/default.conf.template')
expect "the backend is substituted" "$CONF" 'set \$backend "http://app:8000";'
expect "the resolver is substituted" "$CONF" 'resolver 127.0.0.11 valid=10s'
expect "nginx's own variables are not" "$CONF" 'proxy_set_header   Host \$host;'
expect "nginx's own variables are not (2)" "$CONF" 'X-Forwarded-For \$proxy_add_x_forwarded_for'

step "the frontend starts BEFORE its backend exists (nginx resolves at run time, not at start)"
front -e BACKEND_URL=http://backend:8000
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/api/health")
[ "$code" = 502 ] || [ "$code" = 503 ] || die "no backend yet: expected 502/503, got $code"
echo "no backend: $code, and nginx is up"

step "b. /api/<path> reaches the backend as /<path>"
upstream $P-up1 first backend
wait_until 20 "the upstream" docker exec $P-up1 python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/x')"
wait_until 20 "the proxy to find the backend" get -o /dev/null "http://127.0.0.1:$PORT/api/health"
body=$(get "http://127.0.0.1:$PORT/api/health")
expect "path" "$body" '"path": "/health"'
expect "server" "$body" '"server": "first"'
expect "X-Forwarded-Proto" "$body" '"x-forwarded-proto": "http"'
expect "X-Forwarded-Host" "$body" '"x-forwarded-host": "127.0.0.1:'"$PORT"'"'
expect "X-Forwarded-For" "$body" '"x-forwarded-for"'
body=$(get "http://127.0.0.1:$PORT/api/search/a%20b?q=caf%C3%A9&x=1&x=2")
expect "path with an escape" "$body" '"path": "/search/a%20b"'
expect "query string" "$body" '"query": "q=caf%C3%A9&x=1&x=2"'
body=$(get -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-Host: music.example.org' "http://127.0.0.1:$PORT/api/health")
expect "the proxy's scheme is kept" "$body" '"x-forwarded-proto": "https"'
expect "the proxy's host is kept" "$body" '"x-forwarded-host": "music.example.org"'
body=$(get -H 'X-Forwarded-Proto: javascript' "http://127.0.0.1:$PORT/api/health")
expect "a bogus scheme is replaced" "$body" '"x-forwarded-proto": "http"'

step "c. the progress stream is not buffered"
# The first event must be in the file 2 s in, although the second comes only after 4 s.
for query in "" "?x=1"; do
  curl -sN --max-time 9 "http://127.0.0.1:$PORT/api/progress$query" > "$TMP/sse.txt" &
  pid=$!
  sleep 2
  early=$(cat "$TMP/sse.txt")
  wait $pid || true
  expect "the first event, 2 s in (progress$query)" "$early" 'data: 1 first http'
  case "$early" in *"data: 2"*) die "the second event is already there 2 s in: the test is wrong" ;; esac
  expect "both events at the end (progress$query)" "$(cat "$TMP/sse.txt")" 'data: 2 first http'
done
echo "the first event arrives while the stream is open (the second 4 s later)"

step "d. a recreated backend (new address) is found without restarting nginx"
# The second container comes up while the first still holds its address, so it gets another one; then the first goes.
old=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" $P-up1)
upstream $P-up2 second backend
wait_until 20 "the second upstream" docker exec $P-up2 python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/x')"
new=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" $P-up2)
[ "$old" != "$new" ] || die "the second upstream got the first one's address: the test proves nothing"
docker rm -f $P-up1 >/dev/null
echo "address $old -> $new"
wait_until 40 "the new backend to answer" sh -c "curl -fsS --max-time 5 http://127.0.0.1:$PORT/api/health | grep -q '\"server\": \"second\"'"
echo "answered by the second container (nginx was not restarted)"

step "e. BACKEND_URL names the backend"
docker rm -f $P-up2 >/dev/null
upstream $P-up1 third renamed
wait_until 20 "the upstream" docker exec $P-up1 python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/x')"
front -e BACKEND_URL=http://renamed:8000/
wait_until 20 "the renamed backend" sh -c "curl -fsS --max-time 5 http://127.0.0.1:$PORT/api/health | grep -q '\"server\": \"third\"'"
echo "BACKEND_URL=http://renamed:8000/ (trailing slash dropped)"
for bad in "app:8000" "http://app:8000/api" "ftp://app"; do
  docker rm -f $P-bad >/dev/null 2>&1 || true
  docker run -d --name $P-bad --network "$NET" -e BACKEND_URL="$bad" "$IMAGE" >/dev/null
  wait_until 20 "the container with BACKEND_URL=$bad to stop" sh -c "[ \"\$(docker inspect -f '{{.State.Running}}' $P-bad)\" = false ]"
  [ "$(docker inspect -f '{{.State.ExitCode}}' $P-bad)" != 0 ] || die "BACKEND_URL=$bad was accepted"
  expect "the reason for $bad" "$(docker logs $P-bad 2>&1)" "BACKEND_URL"
  echo "BACKEND_URL=$bad refused"
done

step "f. the single-page app"
front -e BACKEND_URL=http://renamed:8000
root=$(get "http://127.0.0.1:$PORT/")
deep=$(get "http://127.0.0.1:$PORT/library/albums/some-id")
expect "index.html" "$root" '<div id="root"'
[ "$root" = "$deep" ] || die "a deep link does not fall back to index.html"

step "all checks passed"

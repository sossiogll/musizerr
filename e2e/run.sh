#!/usr/bin/env sh
# The e2e smoke suite against the running dev stack (docker-compose.dev.yml):
# resets the e2e accounts in the API container, then runs Playwright in its own
# container on the stack's network. Arguments go to `playwright test`.
# See e2e/README.md.
set -eu
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
# The administrator the request-approval spec decides with: its password is made
# here, once, and kept in .env (never committed): an admin with a published
# password would be a way into the instance.
grep -q '^E2E_ADMIN_USERNAME=' .env || echo 'E2E_ADMIN_USERNAME=e2e-admin' >> .env
if ! grep -q '^E2E_ADMIN_PASSWORD=.' .env; then
  sed -i.bak '/^E2E_ADMIN_PASSWORD=/d' .env && rm -f .env.bak
  echo "E2E_ADMIN_PASSWORD=$(openssl rand -hex 16)" >> .env
fi
set -a
. ./.env
set +a

docker exec -e E2E_USERNAME -e E2E_PASSWORD -e E2E_ADMIN_USERNAME -e E2E_ADMIN_PASSWORD "$E2E_APP_CONTAINER" python -m scripts.e2e_user

docker run --rm --network "$E2E_NETWORK" --ipc=host \
  -v "$PWD:/e2e" -v /e2e/node_modules -w /e2e \
  -e E2E_BASE_URL -e E2E_USERNAME -e E2E_PASSWORD -e E2E_ADMIN_USERNAME -e E2E_ADMIN_PASSWORD -e CI=true -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
  mcr.microsoft.com/playwright:v1.55.1-noble \
  sh -c 'corepack enable >/dev/null && pnpm install --frozen-lockfile --store-dir /tmp/pnpm-store --config.confirmModulesPurge=false >/dev/null && pnpm exec playwright test "$@"' e2e "$@"

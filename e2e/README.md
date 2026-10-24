# End-to-end smoke suite

A few Playwright tests against the **running dev stack**
(`docker-compose.dev.yml`), for the failures unit tests cannot see: the
browser's connection limits, several tabs sharing one session, the audio
element.

| Test | What it guards |
| --- | --- |
| `health-covers` | 10 broken covers on a page do not block `GET /api/health` (which decides whether the app says "offline"). |
| `session-tabs` | Two tabs restoring the session at the same moment (a dev-server restart reloads them all) both stay signed in: with Web Locks (the frontend serializes the refresh) and without (the server's rotation grace window). |
| `preview-recovery` | A preview URL that expired answers 403: the player asks for the track again, plays its fresh URL and never stays on "Pause" in silence. |
| `deep-link` | A guarded address (`/admin/requests`) opened in a new page right after the session is restored, with a guest's status in the saved copy of the app, stays where it was asked for: it never passes through sign-in. Needs the e2e administrator (below). |
| `player-heart-shuffle` | The bottom player's heart of a library track toggles at once and the row of the same track follows it (then it is put back); shuffle keeps the track playing first, the queue panel shows the shuffled order and Next follows it. Needs a trending track in the library on Home. |

| `download-requests` | The approval policy with two people at once: the e2e user asks for the whole discography of a trending artist whose provider declares the `discography` capability (a kind that waits under the default policy) and the page says "Waiting for approval"; the e2e administrator, on any page, sees the badge on Administration, finds the request in Administration › Requests and denies it with a reason; the user reads the denial in a toast without reloading and finds it, with the reason, in Downloads › Requests. Nothing is ever queued (it is denied), and a request left waiting by a failure is cancelled at the end (and at the start of the next run). Skipped when the e2e user's policy lets an artist through at once, no trending artist has a provider that can give a discography, or the artist is already all in the library. Needs the e2e administrator (below). |

## Run

With the dev stack up (`docker compose -f docker-compose.dev.yml up`):

```sh
e2e/run.sh
```

The script:

1. copies `e2e/.env.example` to `e2e/.env` the first time (a throwaway
   account for the **local** instance: never put a real password there);
2. gives `e2e/.env` a throwaway **administrator** (`E2E_ADMIN_USERNAME`,
   `E2E_ADMIN_PASSWORD`) if it has none: the password is random, made once
   and kept in `e2e/.env` (an admin with a published password would be a way
   into the instance); `download-requests` needs it;
3. creates or resets both accounts in the API container
   (`python -m scripts.e2e_user`);
4. runs Playwright in `mcr.microsoft.com/playwright:v1.55.1-noble`, on the
   stack's network, against the Vite dev server (`http://frontend:5173`,
   allowed in `musizerr-ui/vite.config.ts`).

Nothing is installed on the host: Node, pnpm and the browsers live in the
container (`node_modules` is an anonymous volume, the pnpm store stays in
the container's `/tmp`). Arguments go to
`playwright test`, e.g. `e2e/run.sh tests/session-tabs.spec.ts` or
`e2e/run.sh --trace on`. A failed test leaves its trace in
`e2e/test-results/`.

The preview test needs a metadata provider that answers the trending tracks
with previews; the tests name no provider (they read `/api/providers/public`
and the discover answers).

Type-check the suite in the same container:

```sh
docker run --rm -v "$PWD/e2e:/e2e" -v /e2e/node_modules -w /e2e mcr.microsoft.com/playwright:v1.55.1-noble sh -c 'corepack enable && pnpm install --frozen-lockfile --store-dir /tmp/pnpm-store && pnpm typecheck'
```

# Worker topology

Musizerr runs its background work on Celery with Redis as the broker. The
work is split over **three queues**, each consumed by **its own worker
process**, plus **one beat**. A slow task of one kind never makes the others
wait.

| Queue | Worker service | Concurrency | What runs there |
|---|---|---|---|
| `downloads` | `worker` | `max_concurrent_downloads` (read at start) | `download_task` (resolve, download, store) |
| `jobs` | `worker-jobs` | 2 | the library jobs a user starts ("Sincronizza i mi piace", "Scarica i mi piace mancanti"), the full-sync check, the library index routine (`sync_library_index`) |
| `maintenance` | `worker-maintenance` | 2 | the scheduler tick, the other routines (the like matching among them), the reconciliation chains (delegated downloads, likes), short tasks queued by the others (unlike, routine starts) |

- The routes live in one place, `workers/celery_app.py` (`TASK_ROUTES`), by
  registered task name: services queue tasks by name (`send_task`), which
  ignores the `queue=` of a task decorator. A task without a route goes to
  `maintenance` (the default queue), never to the downloads.
- `worker-maintenance` also consumes `default`, only to drain the messages
  queued before the split.
- **One beat per instance.** In production it is the `beat` service; in
  development it is embedded in `worker-maintenance` (`-B`). Beat sends
  `workers.routines.tick` every 60 s; the tick decides which routines are due.
- **One replica per worker.** At start, a worker fails what its predecessor
  left in flight on its own queue: the downloads worker the downloads still
  "in progress", the jobs worker the library jobs still "running". The
  maintenance worker touches neither.
- Every worker refreshes a heartbeat per queue it consumes
  (`services/worker_health.py`, `musizerr:worker:heartbeat:<queue>`). A
  library job queued while no `jobs` worker is alive is failed as
  `worker_unavailable`.

## Routine runs

A routine run is a row of `routine_runs` with three times:

- `queued_at` — the tick (or "Run now") dispatched it;
- `started_at` — a worker really started it (`NULL` while it waits);
- `finished_at`.

The status goes `queued → running → ok | error | skipped`. A routine is due
`interval` minutes after its last **real start**, not after the dispatch, and
a queued or running run blocks another dispatch. A scheduled run starts only
if no other run of the same routine started within its period (`skipped`
otherwise), and the message of a run closed as lost does nothing: a routine
never runs twice for the same period because of a backlog. "Run now" has no
period.

- **A run executes on the queue of its routine's task** (`TASK_ROUTES`; the
  default, `maintenance`, for every routine but one). The library index
  (`sync_library_index`, `workers.tasks.sync_library_index`) is routed to
  `jobs`: it lists the whole library of every sync provider and gives each
  file its record, which on a large library takes minutes the first time
  (3,749 → 7,675 records in 27 s on the live data) and a few seconds after.
  On `maintenance` it would hold one of its two processes, and with another
  long routine (duplicates scan, file reconciliation) the tick would wait.
  On `jobs` it runs beside the users' likes syncs, which start it anyway.
- **A routine can follow another** (`RoutineDef.follows`): the end of every
  run of the leader, ok or failed, dispatches its enabled followers, and the
  tick does not start a follower while its leader is running, or queued with
  a worker of its queue alive to start it (a leader queued with no worker
  does not hold the follower back). The like matching (`match_likes`)
  follows the library index: it always matches against an up-to-date
  library, and its result says how the last index ended
  (`library_index: {status, error_code}`). With the index switched off, the
  matching runs on its own interval.
- **Events of the host start a routine** through
  `services.routines.start_routine` (the task `workers.routines.start`, on
  `maintenance`, dispatches it like "Run now"): the library scan and the end
  of a likes sync start the library index, and the matching follows it.
  Nothing happens while the routine is already queued or running: that run
  serves, and its followers run after it.
- **A run fails when its task says so**: a result `{"status": "error", ...}`
  is recorded `error`, never `ok` with the failure inside. A result that
  names its `error_code` keeps it (the routines page shows that code and the
  result, e.g. one report per sync provider); otherwise the text is
  classified (`routine_<kind>`).
- **Progress**: a routine that declares `reports_progress` receives a
  `services.routines.RunProgress` and reports a phase key, the provider,
  processed/total, with a heartbeat — the conventions of the library jobs,
  throttled, each write its own short transaction after the task's commit.
  `GET /system/routines` shows it on the running run (`last_run.progress`).

- **Playlists** (`docs/playlists.md`): two routines on `maintenance`.
  `sync_playlists` copies every user's playlists to their own account on the sync
  providers that keep playlists, and **follows** `sync_library_index` (a track
  downloaded meanwhile has its id on the server only after the index; with the
  index off it runs on its own interval). An edit of a playlist and the end of
  `refresh_followed_playlists` start it through `start_routine`, so it runs at once
  unless it is already queued or running. `refresh_followed_playlists` looks at
  the source of every followed playlist (the provider's version marker) with the
  owner's accounts and asks the new tracks' download through the approval
  policy; it is its own leader, on its own interval. Both report their progress
  (`refreshing`, `pushing`) and fail with a stable code, never `ok` with a hidden
  warning. The task `workers.maintenance.delete_playlist_tracks` (queued by
  `DELETE /playlists/{id}?delete_tracks=true`) runs on `maintenance` too.

## Applying the topology

After pulling a version with this topology, recreate the workers: a worker
left on the old `--queues=downloads,default` no longer receives routines,
ticks or library jobs.

```sh
docker compose -f docker-compose.dev.yml up -d --build --remove-orphans worker worker-jobs worker-maintenance
```

`app/tests/test_worker_topology_compose.py` checks both compose files: every
queue consumed by exactly one worker, one beat, a downloads worker that
consumes only the downloads.

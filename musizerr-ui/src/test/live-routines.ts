/**
 * Real responses of GET /system/routines, captured from a running instance
 * on 29 Sep 2026:
 *
 * - `routines`: the answer of this backend (UTC times with their offset, the
 *   result of each run as data, its duration, the work still in progress);
 *   Captured before runs recorded `queued_at`/`wait_seconds` and `queued`:
 *   the panel reads such a run by its `started_at` (see the queued tests
 *   for the current shape);
 * - `legacy_routine`: one entry as the backend answered before the fix:
 *   `started_at`/`finished_at` without an offset (SQLite hands them back
 *   naive) and the result as the Python repr of a dict.
 *
 * The routines panel tests run on these shapes, with TZ=Europe/Rome
 * (vitest.config.ts): a naive time read as local time is two hours off.
 */
import type { RoutineInfo } from "@/lib/api-client";

export const LIVE_ROUTINES = {
  "routines": [
    {
      "key": "sync_saved_tracks",
      "task": "workers.tasks.sync_all_users_saved_tracks",
      "enabled": true,
      "interval_minutes": 360,
      "min_interval_minutes": 15,
      "last_run": {
        "started_at": "2026-09-29T09:38:06.018415+00:00",
        "finished_at": "2026-09-29T09:38:06.052212+00:00",
        "duration_seconds": 0.034,
        "status": "ok",
        "result": {
          "dispatched": 2,
          "users": 2
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-29T15:38:06.018415+00:00",
      "running": false,
      "scheduler_stale": true,
      "active_work": 0
    },
    {
      "key": "sweep_delegations",
      "task": "workers.maintenance.sweep_delegations",
      "enabled": true,
      "interval_minutes": 30,
      "min_interval_minutes": 5,
      "last_run": {
        "started_at": "2026-09-29T13:02:57.955295+00:00",
        "finished_at": "2026-09-29T13:02:57.999125+00:00",
        "duration_seconds": 0.044,
        "status": "ok",
        "result": {
          "status": "swept",
          "checked": 0,
          "resumed": 0
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-29T13:32:57.955295+00:00",
      "running": false,
      "scheduler_stale": true,
      "active_work": 0
    },
    {
      "key": "reconcile_pending_likes",
      "task": "workers.maintenance.reconcile_pending_likes",
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 5,
      "last_run": {
        "started_at": "2026-09-29T12:33:57.913106+00:00",
        "finished_at": "2026-09-29T12:33:57.930286+00:00",
        "duration_seconds": 0.017,
        "status": "ok",
        "result": {
          "status": "swept",
          "count": 2
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-29T13:33:57.913106+00:00",
      "running": false,
      "scheduler_stale": true,
      "active_work": 58
    },
    {
      "key": "match_likes",
      "task": "workers.tasks.match_likes",
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 5,
      "last_run": {
        "started_at": "2026-09-29T12:32:57.901852+00:00",
        "finished_at": "2026-09-29T12:32:59.000744+00:00",
        "duration_seconds": 1.099,
        "status": "ok",
        "result": {
          "checked": 1652,
          "matched": 33
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-29T13:32:57.901852+00:00",
      "running": false,
      "scheduler_stale": true,
      "active_work": null
    },
    {
      "key": "library_duplicates_scan",
      "task": "workers.duplicates.scan_library_duplicates",
      "enabled": true,
      "interval_minutes": 1440,
      "min_interval_minutes": 60,
      "last_run": {
        "started_at": "2026-09-29T01:36:11.819511+00:00",
        "finished_at": "2026-09-29T01:36:16.491332+00:00",
        "duration_seconds": 4.672,
        "status": "ok",
        "result": {
          "status": "done",
          "candidates": 6231,
          "clusters": 187,
          "fingerprinted": 2,
          "groups_created": 5,
          "groups_updated": 53,
          "dropped_members": 0,
          "linked_via_sync_id": 8,
          "linked_via_match": 16,
          "linked_none": 95
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-30T01:36:11.819511+00:00",
      "running": false,
      "scheduler_stale": false,
      "active_work": null
    },
    {
      "key": "retry_transient_failures",
      "task": "workers.tasks.retry_transient_failures",
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 15,
      "last_run": {
        "started_at": "2026-09-29T13:07:57.970181+00:00",
        "finished_at": "2026-09-29T13:07:58.171740+00:00",
        "duration_seconds": 0.202,
        "status": "ok",
        "result": {
          "candidates": 1161,
          "queued": 25
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-29T14:07:57.970181+00:00",
      "running": false,
      "scheduler_stale": true,
      "active_work": 25
    },
    {
      "key": "sweep_workspaces",
      "task": "workers.maintenance.sweep_workspaces",
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 15,
      "last_run": {
        "started_at": "2026-09-29T12:33:57.923261+00:00",
        "finished_at": "2026-09-29T12:33:57.942099+00:00",
        "duration_seconds": 0.019,
        "status": "ok",
        "result": {
          "status": "ok",
          "removed": 13
        },
        "detail": null,
        "error_code": null
      },
      "next_run_at": "2026-09-29T13:33:57.923261+00:00",
      "running": false,
      "scheduler_stale": true,
      "active_work": null
    }
  ],
  "scheduler_stale": true
} as unknown as { routines: RoutineInfo[]; scheduler_stale: boolean };

/** Before the fix: no `result`/`duration_seconds`/`active_work`, naive times. */
export const LEGACY_ROUTINE = {
  "key": "reconcile_pending_likes",
  "task": "workers.maintenance.reconcile_pending_likes",
  "enabled": true,
  "interval_minutes": 60,
  "min_interval_minutes": 5,
  "last_run": {
    "started_at": "2026-09-29T12:33:57.913106",
    "finished_at": "2026-09-29T12:33:57.930286",
    "status": "ok",
    "detail": "{'status': 'swept', 'count': 2}",
    "error_code": null
  },
  "next_run_at": "2026-09-29T13:33:57.913106+00:00",
  "running": false,
  "scheduler_stale": true
} as unknown as RoutineInfo;

/**
 * GET /system/routines entries of the library index and the like matching
 * that follows it, as the backend answers them (captured on 3 Oct 2026 from
 * `routes/system/routines.list_routines`, run on the real scheduler and tasks
 * with a fake sync provider — `app/tests/test_sync_index_routine.py`):
 *
 * - `ok`: an index run and the matching after it;
 * - `error`: the sync provider unreachable (the 3 Oct DNS outage): the index
 *   run is an error with its code, per provider, and the matching says it
 *   matched against the library as it was;
 * - `running`: an index run that reports its progress.
 */
export const LIVE_SYNC_INDEX = {
  "ok": [
    {
      "key": "sync_library_index",
      "task": "workers.tasks.sync_library_index",
      "follows": null,
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 15,
      "last_run": {
        "queued_at": "2026-10-03T18:40:12.546428+00:00",
        "started_at": "2026-10-03T18:40:12.549092+00:00",
        "finished_at": "2026-10-03T18:40:12.581581+00:00",
        "wait_seconds": 0.003,
        "duration_seconds": 0.032,
        "status": "ok",
        "result": {
          "listed": 40,
          "created": 40,
          "attached": 0,
          "aliased": 0,
          "stale_dropped": 0,
          "gone": 0,
          "providers": [
            {
              "sync_provider": "musicserver",
              "listed": 40,
              "already_indexed": 0,
              "aliased": 0,
              "attached": 0,
              "created": 40,
              "twins": 0,
              "stale_dropped": 0,
              "stale_kept": 0,
              "gone": 0,
              "pruned": true,
              "status": "done"
            }
          ],
          "status": "ok"
        },
        "detail": null,
        "error_code": null,
        "progress": null
      },
      "next_run_at": "2026-10-03T19:40:12.549092+00:00",
      "queued": false,
      "running": false,
      "scheduler_stale": false,
      "active_work": null
    },
    {
      "key": "match_likes",
      "task": "workers.tasks.match_likes",
      "follows": "sync_library_index",
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 5,
      "last_run": {
        "queued_at": "2026-10-03T18:40:12.582634+00:00",
        "started_at": "2026-10-03T18:40:12.583072+00:00",
        "finished_at": "2026-10-03T18:40:12.584118+00:00",
        "wait_seconds": 0.0,
        "duration_seconds": 0.001,
        "status": "ok",
        "result": {
          "checked": 0,
          "matched": 0,
          "library_index": {
            "status": "ok",
            "finished_at": "2026-10-03T18:40:12.581581+00:00"
          }
        },
        "detail": null,
        "error_code": null,
        "progress": null
      },
      "next_run_at": "2026-10-03T19:40:12.583072+00:00",
      "queued": false,
      "running": false,
      "scheduler_stale": false,
      "active_work": null
    }
  ],
  "error": [
    {
      "key": "sync_library_index",
      "task": "workers.tasks.sync_library_index",
      "follows": null,
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 15,
      "last_run": {
        "queued_at": "2026-10-03T18:40:12.590577+00:00",
        "started_at": "2026-10-03T18:40:12.591007+00:00",
        "finished_at": "2026-10-03T18:40:12.591655+00:00",
        "wait_seconds": 0.0,
        "duration_seconds": 0.001,
        "status": "error",
        "result": {
          "listed": 0,
          "created": 0,
          "attached": 0,
          "aliased": 0,
          "stale_dropped": 0,
          "gone": 0,
          "providers": [
            {
              "sync_provider": "musicserver",
              "status": "error",
              "error_code": "sync_provider_unreachable",
              "error_message": "The sync provider could not be reached.",
              "error_detail": "[Errno -3] Temporary failure in name resolution"
            }
          ],
          "status": "error",
          "error_code": "sync_provider_unreachable",
          "error_message": "The sync provider could not be reached.",
          "error_detail": "[Errno -3] Temporary failure in name resolution",
          "failed": [
            "musicserver"
          ]
        },
        "detail": null,
        "error_code": "sync_provider_unreachable",
        "progress": null,
        "error_message": "The sync provider could not be reached.",
        "error_detail": "[Errno -3] Temporary failure in name resolution"
      },
      "next_run_at": "2026-10-03T19:40:12.591007+00:00",
      "queued": false,
      "running": false,
      "scheduler_stale": false,
      "active_work": null
    },
    {
      "key": "match_likes",
      "task": "workers.tasks.match_likes",
      "follows": "sync_library_index",
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 5,
      "last_run": {
        "queued_at": "2026-10-03T18:40:12.592190+00:00",
        "started_at": "2026-10-03T18:40:12.592576+00:00",
        "finished_at": "2026-10-03T18:40:12.593137+00:00",
        "wait_seconds": 0.0,
        "duration_seconds": 0.001,
        "status": "ok",
        "result": {
          "checked": 0,
          "matched": 0,
          "library_index": {
            "status": "error",
            "finished_at": "2026-10-03T18:40:12.591655+00:00",
            "error_code": "sync_provider_unreachable",
            "error_message": "The sync provider could not be reached."
          }
        },
        "detail": null,
        "error_code": null,
        "progress": null
      },
      "next_run_at": "2026-10-03T19:40:12.592576+00:00",
      "queued": false,
      "running": false,
      "scheduler_stale": false,
      "active_work": null
    }
  ],
  "running": [
    {
      "key": "sync_library_index",
      "task": "workers.tasks.sync_library_index",
      "follows": null,
      "enabled": true,
      "interval_minutes": 60,
      "min_interval_minutes": 15,
      "last_run": {
        "queued_at": "2026-10-03T18:40:12.596465+00:00",
        "started_at": "2026-10-03T18:40:12.596797+00:00",
        "finished_at": null,
        "wait_seconds": 0.0,
        "duration_seconds": null,
        "status": "running",
        "result": null,
        "detail": null,
        "error_code": null,
        "progress": {
          "phase": "indexing",
          "provider": "musicserver",
          "processed": 3200,
          "total": 7675,
          "heartbeat_at": "2026-10-03T18:40:12.597115+00:00"
        }
      },
      "next_run_at": "2026-10-03T19:40:12.596797+00:00",
      "queued": false,
      "running": true,
      "scheduler_stale": false,
      "active_work": null
    }
  ]
} as unknown as { ok: RoutineInfo[]; error: RoutineInfo[]; running: RoutineInfo[] };

# Database migrations

Schema lives in `app/db/models.py` (SQLModel). Migrations live in
`app/migrations/versions/` and are managed by Alembic.

## Why this exists

`SQLModel.metadata.create_all()` only creates tables that don't yet
exist — it never alters columns of existing tables. Adding a field to a
model in a fresh checkout would silently fail on long-lived DBs at
runtime (`OperationalError: no such column …`). Alembic handles the
ALTER side.

## Runtime behaviour

**Only `init` migrates.** `services/database_init.py:init_db()` is what the init
container runs (`python -m scripts.init_db`) and what a maintenance script runs
before it opens the database. The API and the workers never migrate: they check
(below) and refuse to start on a schema they cannot use. The database is the one
`DATABASE_URL` names (PostgreSQL) or, unset, the SQLite file of `DATA_DIR` (see
`docs/postgresql.md`). `init_db()` judges the schema first
(`core/schema_guard.py`), and then:

1. **Up to date** (the database is at the head of the migrations): nothing runs,
   no backup is taken, nothing is recorded. One `alembic_version` row, as ever.
2. **Fresh DB** — no tables exist. Alembic runs every revision in sequence. There
   is nothing to back up.
3. **Pre-alembic DB** — tables exist but no `alembic_version` table (an old SQLite
   install; a PostgreSQL database is never stamped: it is refused). It is backed up
   as `unversioned`, stamped at `0001` and upgraded.
4. **Managed DB behind the code** — `alembic_version` names older revisions. Pending
   migrations: the upgrade path is checked, **the database is backed up**, the
   migrations run, `schema_info` is written (below).
5. **Managed DB ahead of the code** — the database was migrated by a later release.
   If everything this code does not know is `expand`, it is the allowed rollback of
   one minor: the database is left as it is and the app starts. If a later release
   applied a `contract` migration this code does not have, `init` refuses (below).

The init container (`docker-compose.yaml`) runs this and exits; the app, the workers
and beat start only after it completed successfully (`depends_on: init: condition:
service_completed_successfully`), so migrations get applied before any of them
boots. (It no longer scans the library: that is the routine
`import_existing_library`, [workers.md](workers.md).) `init` exits with status 1 and
a message when it stops, so the rest of the stack does not start.

## Expand, then contract (ADR-0007 §4)

Every migration declares its **phase** in its module, with a short reason; a test
fails on one that does not:

```python
phase = "expand"  # adds a table
irreversible = True  # optional: its downgrade does nothing or may refuse
```

| Phase | What it may do | Released in |
|---|---|---|
| `expand` | New tables and columns (nullable, or with a server default), new indexes, backfills that leave the old columns as they were, widening a type, dropping or renaming an index no code names. **The previous release works on the result**, so going back one minor needs no downgrade. | any release |
| `contract` | Drop or rename a column or a table, tighten a constraint (NOT NULL, unique, a foreign key, a narrower type), and any change of data the previous code cannot read or that loses data (an encryption like `0041`, a deletion). | a **MAJOR** only, listed in the release notes |

`irreversible = True` marks a migration whose `downgrade()` cannot be relied on to
restore the previous state (it does nothing, or may refuse). The way back across one
is the backup, not Alembic.

Something that takes two steps (rename a column) takes two releases: expand (add the
new column, write both) in a minor, contract (drop the old one) in the next major.

The existing migrations were classified once (`phase` in each file). The rule that
tests enforce: a migration that calls `drop_column`, `drop_table`, `rename_table` or
renames a column in `upgrade()` is `contract`.

## A backup before every migration

When migrations are pending, `init` dumps the database **first**, and does not
migrate if the dump fails (the message says why and what to do). Nothing is dumped
when nothing is pending, or for a database that has no tables yet.

| | PostgreSQL | SQLite |
|---|---|---|
| How | `pg_dump --format=custom` (the image ships `postgresql-client-17`, the client of the `postgres:17` the compose files run; an older client refuses a newer server and says so) | the online backup API: a consistent snapshot of the file, WAL included, whoever is writing |
| File | `/data/backups/premigration-<UTC time>-from-<revision>-to-<revision>.dump` | the same name ending `.sqlite3` |
| Restore | `pg_restore` (below) | copy the file back (below) |

- **Where**: `DATA_DIR/backups` — in the compose files the `data` volume, mounted at
  `/data` in `init` and the app. The folder is mode 700 and each file mode 600: a
  backup holds everything (sealed credentials, password hashes).
- **Which**: the name carries the revision the database was at and the one the
  migration went to (`unversioned` for a pre-alembic database), after the time, so a
  listing sorts in the order they were taken.
- **How many**: the last `MIGRATION_BACKUPS_KEEP` (default **5**) are kept, older ones
  pruned right after the new one is written. A file that is not one of ours is never
  touched.
- **Off**: `MIGRATION_BACKUP=off` skips the backup, for whoever backs the database up
  otherwise (a warning is logged). There is no other way to skip it.
- A backup is written under a temporary name and renamed when complete: a failure
  leaves no file that looks like a backup.
- **The credentials key is not in it.** A backup holds the credentials sealed with
  `CREDENTIALS_KEY`; keep the key with your backups ([credentials.md](credentials.md)).
- The backup lives on the `data` volume, next to nothing else that matters for a
  PostgreSQL install. Copy the folder somewhere else if you want it to survive the
  loss of the host; it is a safety net for a migration, not a backup strategy.

## `schema_info`: which app can use this database

A table of one row (`schema_info`, migration `0043`), written by `init` after every
migration run:

| Column | Meaning |
|---|---|
| `revision` | the revisions the database was at after that run |
| `app_version` | the version that ran it (`dev` for a development build) |
| `min_app_version` | the oldest version that can run on it; raised to the MAJOR of the version that applied a `contract` migration, never lowered; empty until one is recorded |
| `contract_revision` | the newest `contract` migration applied |
| `phase` | `contract` when that run applied one, else `expand` |
| `migrated_at` | when |

Admins see it: `GET /health` carries a `schema` object (the revisions the database is at now, `migrated_by`,
`min_app_version`, `contract_revision`, `phase`, `migrated_at`; admins only, like the version, and absent while the
database does not answer), and so does `GET /admin/updates`, which the About page reads (Database, and the oldest version
that can use it once a contract migration is recorded).

## The start check

The API (`main.lifespan`) and every worker (`workers.celery_app`, at `worker_init`)
compare the database with their code and **refuse to start** when they cannot use it,
with a message that says what to do:

| The database is… | The API and the workers |
|---|---|
| at the head of the migrations | start |
| behind (some migration not applied: `init` has not run, or failed) | refuse: "run init" (`docker compose up init`, read its log) |
| ahead, and what the code does not know is `expand` only (a later release migrated it; this is the **rollback of one minor**) | start, logging a warning |
| ahead, and a `contract` migration the code does not have was applied (`contract_revision` unknown to it), or `min_app_version` is above this version | refuse: names the versions and the backup to restore |
| ahead, with no `schema_info` row to say it is safe | refuse |

`init` applies the same judgment before it touches anything: it migrates a database
that is behind, leaves one that is expand-ahead as it is, and refuses the rest. A
development build (`dev`) has no version to compare and is judged by revisions alone.

**The first release with the guard** (the one that adds `0043`) is where the way back
begins: a release *before* it does not know `0043` and its own `init` stops with
Alembic's "Can't locate revision". To go back past it, restore the backup that `init`
took when it migrated.

### Upgrade paths

A MAJOR may declare the oldest version it upgrades from (`MIN_UPGRADE_FROM` in
`core/schema_guard.py`, empty today; it goes to the release's `release.json` as
`min_upgrade_from`). `init` refuses a database last migrated by an older one (or by a
release without `schema_info`) and names the version to go through first.

## Going back

- **One minor, expand only** (the usual case): set the previous `MUSIZERR_VERSION`
  and `docker compose up -d`. The schema is compatible: nothing to restore.
- **Across a contract migration, or a migration that went wrong**: restore the backup
  taken before it and start the version it was taken for.

### Restoring a backup

The backups are in the `data` volume (`/data/backups`). Stop everything that talks to
the database first, then list them:

```sh
docker compose stop app worker worker-jobs worker-maintenance beat
docker compose run --rm --no-deps init ls -l /data/backups
```

Check what a file is (a dry run: it changes nothing) and restore it. `init` has the
image, the `data` volume and the database settings, so the script runs there:

```sh
# dry run
docker compose run --rm init python -m scripts.restore_backup /data/backups/premigration-20261024T101500Z-from-0042_open_mode_accounts-to-0043_schema_info.dump
# restore
docker compose run --rm init python -m scripts.restore_backup /data/backups/premigration-20261024T101500Z-from-0042_open_mode_accounts-to-0043_schema_info.dump --apply
```

(`.sqlite3` on SQLite.) With `--apply` it first copies the database as it is now to
`/data/backups/pre-restore-<time>.<ext>` (never pruned; `--skip-safety-copy` for a
database that cannot be read), then replaces it. Everything written after the backup
is lost. Then start **the version the backup was taken for** (the name says the
revision: it is the version before the migration) and `docker compose up -d`. Exit
status: 0 done, 1 the restore failed, 2 refused (nothing was touched).

By hand, without the script:

- **PostgreSQL** (`postgres` service, user and database `musizerr`):

  ```sh
  docker compose exec -T postgres psql -U musizerr -d musizerr -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
  docker compose run --rm -T --no-deps init cat /data/backups/<file>.dump \
    | docker compose exec -T postgres pg_restore -U musizerr -d musizerr --no-owner --no-acl --exit-on-error --single-transaction
  ```

  (`DROP SCHEMA ... CASCADE` empties the database: the dump of an older schema does not
  name the tables a later migration added.)
- **SQLite** (`DATA_DIR/musizerr.db`): stop the stack, then copy the backup over the
  file and remove its `-wal` and `-shm` files:

  ```sh
  docker run --rm -v <data volume>:/data alpine sh -c \
    'cp /data/backups/<file>.sqlite3 /data/musizerr.db && rm -f /data/musizerr.db-wal /data/musizerr.db-shm'
  ```

If a MAJOR release says in its notes that it has contract migrations, take a copy of
the backups folder before upgrading: `init` makes the backup itself, but it is the one
thing between a bad upgrade and a rebuilt instance.

### Releases

The release workflow runs `app/scripts/migration_phases.py --since <last tag>` and puts
the result in the release's `release.json` (`contract_migrations`, `min_upgrade_from`)
and, when there are contract migrations, in a "Database" section of the notes. The
update check (`GET /admin/updates`) reads it: a release with contract migrations is
never a plain update.

## Workflow for devs

After changing `app/db/models.py`:

```sh
# From inside the app container (or with DATA_DIR pointing at a sqlite,
# or DATABASE_URL at a database, you don't mind touching - autogenerate
# compares the model metadata to the live DB schema).
cd app
alembic revision --autogenerate -m "short description"
```

Then:

1. **Read the generated file** before committing. Autogenerate is good
   but not perfect — especially on SQLite, where ALTER COLUMN doesn't
   exist natively. Anything beyond a plain `add_column` is wrapped in
   `op.batch_alter_table(...)` because `env.py` sets `render_as_batch` on SQLite
   (PostgreSQL alters in place; the same batch code emits plain `ALTER`s there);
   leave that intact. A migration runs on BOTH databases: no `PRAGMA`, no
   `GROUP_CONCAT`, no `= 1` for a boolean (use a bound parameter or `true`), a
   derived table gets an alias, a partial index gets `sqlite_where` and
   `postgresql_where`, use `sa.inspect` to look at the schema. PostgreSQL runs
   all of them in one transaction (an error undoes the upgrade). The test of
   the migrations (`tests/test_migrations_schema.py`) runs them from empty to the
   head on both and compares the result with the models.
2. **Declare the phase** (`phase = "expand"` or `"contract"`, with the reason after
   it) under `depends_on`. Be honest: if it drops or renames a column or a table,
   tightens a constraint or rewrites data the previous release reads, it is
   `contract`, and a `contract` migration waits for a MAJOR. A migration without a
   phase fails `tests/test_database_guard.py`. Add `irreversible = True` if the
   downgrade cannot restore what the upgrade did.
3. If adding a NOT NULL column to an existing table, include
   `server_default=...` (alembic will need a value to backfill existing
   rows). You can drop the default in a follow-up revision once
   backfilled if you don't want it long-term. (A NOT NULL column with no default
   is `contract`: the previous release cannot insert.)
4. Bump the filename prefix to keep an obvious order (`0003_…`,
   `0004_…`). The actual `down_revision` chain is what alembic uses;
   the prefix is just for humans reading `ls`.
5. Test on a copy of a populated DB before merging.

## Manual commands

```sh
cd app

alembic current              # which revision is the DB at?
alembic history              # full revision chain
alembic upgrade head         # apply all pending revisions (no backup, no schema_info: use init)
alembic downgrade -1         # roll back one (don't on prod)
alembic stamp <rev>          # mark DB at <rev> without running anything
```

`DATABASE_URL` says which database alembic touches; unset, `DATA_DIR`
chooses the sqlite file (default `/data`, the Docker volume). For local
one-offs, point it at `/tmp/musizerr_test` or similar. `alembic upgrade head`
by hand takes no backup and records nothing; `python -m scripts.init_db` does both.

## Pitfalls

- SQLite can't `ALTER COLUMN` — batch mode works around it by
  copy/rename, but the table is locked for the duration. Don't do it
  online on a hot DB. PostgreSQL alters in place (a lock, not a copy).
- `from sqlmodel import SQLModel` is a re-export of SQLAlchemy's
  declarative base; the shared metadata only sees a model if its module
  has been imported. `env.py` does `import db.models` for this reason —
  if you split models into multiple modules, import them all from
  `app/db/models/__init__.py` (or extend env.py).
- Don't run `create_all()` again from anywhere — it would create new
  tables behind alembic's back and break future migrations.
- A test that needs the database at an older revision uses
  `tests.database.migration_database("<revision>")`.

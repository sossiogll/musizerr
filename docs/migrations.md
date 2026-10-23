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

`init_db()` in `app/db/engine.py` is called once from the FastAPI
lifespan in `app/main.py`. It runs `alembic upgrade head` instead of
`create_all()`, on the database `DATABASE_URL` names (PostgreSQL) or, unset, the
SQLite file of `DATA_DIR` (see `docs/postgresql.md`). Three cases:

1. **Fresh DB** — no tables exist. Alembic runs every revision in
   sequence (currently `0001` → `0002`).
2. **Pre-alembic DB** — tables exist but no `alembic_version` table.
   This is the case for every SQLite install before this commit (a PostgreSQL
   database is never stamped: it is refused). The startup
   code inspects the `library_entries` table:
   - if `storage_provider` is already present (someone live-patched the
     column manually), stamp at `head` and no-op.
   - otherwise stamp at `0001` and let `0002` add the column.
3. **Managed DB** — `alembic_version` is set. Just apply any new
   revisions on top.

The init container (`docker-compose.yaml`) calls `init_db()` via
`scripts/init_db.py` and exits; the app, the workers and beat start only after
it completed successfully (`depends_on: init: condition:
service_completed_successfully`), so migrations get applied before any of them
boots. (It no longer scans the library: that is the routine
`import_existing_library`, [workers.md](workers.md).)

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
2. If adding a NOT NULL column to an existing table, include
   `server_default=...` (alembic will need a value to backfill existing
   rows). You can drop the default in a follow-up revision once
   backfilled if you don't want it long-term.
3. Bump the filename prefix to keep an obvious order (`0003_…`,
   `0004_…`). The actual `down_revision` chain is what alembic uses;
   the prefix is just for humans reading `ls`.
4. Test on a copy of a populated DB before merging.

## Manual commands

```sh
cd app

alembic current              # which revision is the DB at?
alembic history              # full revision chain
alembic upgrade head         # apply all pending revisions
alembic downgrade -1         # roll back one (don't on prod)
alembic stamp <rev>          # mark DB at <rev> without running anything
```

`DATABASE_URL` says which database alembic touches; unset, `DATA_DIR`
chooses the sqlite file (default `/data`, the Docker volume). For local
one-offs, point it at `/tmp/musizerr_test` or similar.

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

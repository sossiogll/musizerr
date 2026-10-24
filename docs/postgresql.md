# PostgreSQL

Musizeer keeps its data in **PostgreSQL**; **SQLite** still works for a small
install. One variable chooses:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Where the data lives, e.g. `postgresql+psycopg://user:password@postgres:5432/musizeer` (`postgresql://` and `postgres://` work too). **Unset: SQLite**, the file `DATA_DIR/musizeer.db`, exactly as before. |
| `DB_BUSY_TIMEOUT_MS` | How long a write waits for a lock another writer holds before it fails with a retryable 503 `database_busy` (default 15000). SQLite busy timeout; PostgreSQL `lock_timeout`. |
| `DB_STATEMENT_TIMEOUT_MS` | PostgreSQL: a query that runs longer is cancelled (default 120000, `0` never). |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | PostgreSQL connections per process (default 5 + 10). The API and every worker process have their own pool: the server's `max_connections` must cover the sum. |
| `POSTGRES_PASSWORD` | **Required by the compose files**, never committed: write it in `.env` (see `.env.example`). It goes inside `DATABASE_URL`, so use letters and digits (`openssl rand -hex 24`). `POSTGRES_USER` and `POSTGRES_DB` default to `musizeer`. |

Both compose files (`docker-compose.yaml`, `docker-compose.dev.yml`) run a
`postgres` service (official image, named volume `postgres_data`, healthcheck)
and give `DATABASE_URL` to `init`, `app` and every worker, which wait for the
database to be healthy. To stay on SQLite, remove the `postgres` service, the
`<<: *database` lines and the `depends_on` on it.

The schema is Alembic's (`app/migrations`) on both: the `init` step (`services/database_init.py`) runs
`alembic upgrade head` at start, on whichever database `DATABASE_URL` names, after backing it up
([migrations.md](migrations.md)); the API and the workers only check that it is current.

## Switching an existing SQLite install

`scripts/migrate_sqlite_to_postgres.py` copies **every row of every table**
(users, credentials, settings, routines, library, jobs, favorites...) keeping
the primary keys, so nothing has to be configured again: the same `AUTH_SECRET`
keeps every session valid (it must be a strong one, see `docs/permissions.md`:
the compose files refuse to run without it), and the same `CREDENTIALS_KEY`
keeps every connected account readable (`docs/credentials.md`). The SQLite file is only read and is never changed.

1. **Add `POSTGRES_PASSWORD`, `AUTH_SECRET` and `CREDENTIALS_KEY` to `.env`** (and update the code).
2. **Stop the stack** so nothing writes during the copy:
   `docker compose -f docker-compose.dev.yml stop`.
3. **Back up the SQLite database**: copy `data/musizeer.db` together with
   `musizeer.db-wal` and `musizeer.db-shm` if they exist.
4. **Bring the SQLite database to the current schema** (the tool refuses a
   source that is behind, and says so). With `DATABASE_URL` emptied the
   command works on the SQLite file:
   ```sh
   docker compose -f docker-compose.dev.yml run --rm -e DATABASE_URL= app alembic upgrade head
   ```
5. **Dry run** (the default: it reads the whole source, checks every
   precondition and converts every value as the copy would, without touching
   PostgreSQL):
   ```sh
   docker compose -f docker-compose.dev.yml run --rm app \
     python -m scripts.migrate_sqlite_to_postgres --source /data/musizeer.db
   ```
   `--target` defaults to the service's `DATABASE_URL`; `--json` prints the
   report for a machine.
   The directory of the SQLite file must be writable even for the dry run: a
   file in WAL mode cannot be opened read-only from a read-only mount (SQLite
   needs its `-shm` file). The stack is stopped, so mount `data/` as usual.
6. **Apply**:
   ```sh
   docker compose -f docker-compose.dev.yml run --rm app \
     python -m scripts.migrate_sqlite_to_postgres --source /data/musizeer.db --apply
   ```
7. **Start on PostgreSQL**: `docker compose -f docker-compose.dev.yml up -d`.
   Sign in and look around; `docker compose exec postgres psql -U musizeer`
   if in doubt.

### What the tool does and checks

- The target is brought to the Alembic head first. A target that **held data
  before the run** is refused, unless `--replace` (it empties the tables
  first). The source must be at the head too.
- **Rows the migrations seed.** On an empty target the migrations are not
  silent: `0010_settings_store` imports `DATA_DIR/config.json` into the
  `settings` table, so every real install has rows right after
  `alembic upgrade head`. They are not data you hold and the source is
  authoritative, so the tool empties them in the same transaction before the
  copy (a failure rolls that back too) and reports it ("N rows seeded by the
  migrations (settings: N) replaced by the source", `seeded_by_migrations` in
  the JSON report). No `--replace` is needed for this. A target that already ran
  the migrations before the tool did (the app was started on it once) holds
  those rows as data it had: that is `--replace`.
- Every table is copied in foreign key order inside **one transaction**.
  Types go through the models: SQLite 0/1 become booleans, the text of a
  datetime a `timestamp` (UTC), a size past 2 GiB fits (`BIGINT`).
  Credentials are copied as they are, encrypted (`docs/credentials.md`): the
  target needs the same `CREDENTIALS_KEY` the SQLite database was encrypted
  with, and the tool refuses to run without it.
- In the same transaction every table is read back and compared with what was
  sent: row count and a checksum over **every column of every row**; the
  stored credentials must open, with the configured key, into JSON. Any difference rolls everything back
  (exit 1, PostgreSQL stays empty). Only then it commits, and counts the rows
  again from a new connection.
- Sequences and identity columns continue after the highest id copied. Every
  table has a text primary key today, so the report lists none.
- **Orphans.** SQLite never checked foreign keys, PostgreSQL does. A row that
  points at nothing is reported. A reference that only remembers something
  (the requester of a download, the library track of a like or a file, the
  download of a duplicate) is set to NULL. The user an instance with
  authentication off writes as (`__system__`, not an account) is created. A
  refresh token of a user that no longer exists is a dead session: it is
  dropped and reported under "dropped", without asking. Any other orphan
  (real user data) stops the copy (exit 2) and says which; `--discard-orphans`
  drops those rows, and the rows that pointed at them.
- Exit code: `0` done (or a clean dry run), `1` the copy failed or did not
  verify and was rolled back, `2` refused (a precondition does not hold,
  nothing was touched).

### Going back

The SQLite file was never touched: stop the stack, start it **without**
`DATABASE_URL` (remove it from compose) and it runs on that file again. What
was written on PostgreSQL after the switch is not in the file: there is no
reverse copy.

## Running it

- `init` dumps the database (`pg_dump`, custom format) into `/data/backups` before every
  migration, and keeps the last 5; [migrations.md](migrations.md#restoring-a-backup) restores one. That is a safety net
  for a migration, not a backup plan: back PostgreSQL up on a schedule too (`docker compose exec postgres pg_dump -U
  musizeer musizeer > musizeer.sql`).
- The image carries `postgresql-client-17`, the major of the `postgres:17` the compose files run. If you run another
  major, `pg_dump` refuses a server newer than itself and `init` stops with that message; use a 17 server, or
  build the image with the matching client.
- A write that waits more than `DB_BUSY_TIMEOUT_MS` for a lock, a deadlock, a
  serialization failure and a query cancelled by the statement timeout all
  answer **503 `database_busy`** with `Retry-After`, never a 500.
- Text sorts by the database's collation: SQLite compares bytes ("Zebra"
  before "apple"), PostgreSQL in the usual image sorts by locale ("apple"
  before "Zebra"). Columns that can be empty sort with NULL as the smallest
  value on both (`db/expressions.py`).
- A text value with a NUL character (`\x00`) cannot be stored in PostgreSQL.

## Writing code that runs on both

- No SQL that only one database understands. What differs is written once in
  `db/expressions.py` (`ascending`/`descending` for a nullable column,
  `json_number`). A partial unique index needs both `sqlite_where` and
  `postgresql_where`.
- A "look, then write" is only safe when one writer at a time gets in, which
  PostgreSQL does not do. A rule a unique constraint can express is written as
  an `INSERT ... ON CONFLICT` (`db/upsert.py`: `insert_ignore`, `upsert`); one
  it cannot (the track two identifiers resolve to, a download request per
  track, a routine claimed once per period) takes `db.locks.xact_lock(session,
  name)` first. `tests/test_concurrency.py` races every one of them.
- A row that points at another needs the other to exist (PostgreSQL enforces
  it), and a parent is deleted after its children: flush between them, or
  delete with a statement.
- Migrations run from an empty database to the head on both
  (`tests/test_migrations_schema.py` compares the result with the models).

## Tests

The whole backend suite runs on both. SQLite (in memory) is the default;
`TEST_DATABASE_URL=postgresql+psycopg://user:password@host:5432/postgres` (a
user that can create databases) runs it on PostgreSQL: the schema is built
once in a template database and each test gets a copy (`tests/database.py`;
`new_engine()` or the `engine` fixture). A test about one engine only is
marked `sqlite_only("reason")` or `postgresql_only("reason")` and skipped, with
that reason, on the other.

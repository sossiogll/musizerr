"""
The database engine: ONE resolution of where the data lives, shared by the
app, the workers, the scripts and Alembic (``migrations/env.py``).

``DATABASE_URL`` names the database (PostgreSQL: ``postgresql+psycopg://user:
password@host:5432/musizeer``). Unset, the data is the SQLite file
``DATA_DIR/musizeer.db``: a small install needs nothing else.

Every connection gets the settings of its dialect (``make_engine``):

- SQLite: WAL, a busy timeout, ``synchronous=NORMAL``;
- PostgreSQL: a lock timeout and a statement timeout, a ``pool_pre_ping``.

``DB_BUSY_TIMEOUT_MS`` is how long a write waits for a lock another writer
holds before it fails (SQLite busy timeout, PostgreSQL ``lock_timeout``).
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect
from sqlalchemy.engine import URL, Engine, make_url
from sqlmodel import Session, create_engine

logger = logging.getLogger(__name__)

SQLITE = "sqlite"
POSTGRESQL = "postgresql"

# Il database senza DATABASE_URL: un file in DATA_DIR (default /data nel
# container Docker).
DEFAULT_DATA_DIR = "/data"
_APP_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _APP_ROOT / "alembic.ini"

# Quanto una scrittura aspetta il lock di un'altra prima di arrendersi
# ("database is locked" su SQLite, lock_not_available su PostgreSQL). L'API e
# i worker scrivono sullo stesso database: senza attesa una scrittura
# dell'API fallisce subito mentre un worker scrive.
DEFAULT_BUSY_TIMEOUT_MS = 15_000
# Una singola query oltre questo tempo viene interrotta (PostgreSQL; 0 la
# lascia correre): una query rimasta appesa non deve tenere per sempre una
# connessione del pool.
DEFAULT_STATEMENT_TIMEOUT_MS = 120_000
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def parse_url(value: str) -> URL:
    """A database URL. A plain ``postgresql://`` (or ``postgres://``) names
    the psycopg driver, the one installed."""
    url = make_url(value)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+psycopg")
    return url


def database_url() -> URL:
    """Where the data lives: ``DATABASE_URL`` or the SQLite file of
    ``DATA_DIR``."""
    configured = os.environ.get("DATABASE_URL", "").strip()
    if not configured:
        return sqlite_url(Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)) / "musizeer.db")
    return parse_url(configured)


def sqlite_url(path: Path | str) -> URL:
    return URL.create("sqlite", database=str(path))


def configure_sqlite(engine: Engine, *, busy_timeout_ms: int) -> Engine:
    """Pragma di ogni connessione, API e worker:

    - ``journal_mode=WAL``: i lettori non bloccano lo scrittore né viceversa
      (persistente nel file; su un DB in memoria resta ``memory``);
    - ``busy_timeout``: chi trova il lock di scrittura aspetta invece di
      fallire subito;
    - ``synchronous=NORMAL``: il valore raccomandato con WAL (durabile agli
      arresti dell'applicazione, fsync solo ai checkpoint).
    """
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
        finally:
            cursor.close()

    return engine


def make_engine(
    url: URL | str,
    *,
    busy_timeout_ms: int | None = None,
    statement_timeout_ms: int | None = None,
    **engine_options,
) -> Engine:
    """The engine of ``url``, configured for its dialect. ``engine_options``
    go to ``create_engine`` (the pool class of a migration, for one)."""
    url = parse_url(url) if isinstance(url, str) else url
    busy = _env_int("DB_BUSY_TIMEOUT_MS", DEFAULT_BUSY_TIMEOUT_MS) if busy_timeout_ms is None else busy_timeout_ms
    if url.get_backend_name() == SQLITE:
        return configure_sqlite(
            create_engine(
                url,
                # Il timeout del driver è la stessa attesa, in secondi.
                connect_args={"check_same_thread": False, "timeout": busy / 1000},
                **engine_options,
            ),
            busy_timeout_ms=busy,
        )
    statement = (
        _env_int("DB_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS)
        if statement_timeout_ms is None else statement_timeout_ms
    )
    # A caller that picks its own pool class (a migration: no pool) sizes none.
    sizing = {} if "poolclass" in engine_options else {
        "pool_size": _env_int("DB_POOL_SIZE", DEFAULT_POOL_SIZE),
        "max_overflow": _env_int("DB_MAX_OVERFLOW", DEFAULT_MAX_OVERFLOW),
    }
    return create_engine(
        url,
        pool_pre_ping=True,
        **sizing,
        # timezone=UTC: the columns hold naive UTC, so the database clock (
        # CURRENT_TIMESTAMP in a migration) must read UTC too.
        connect_args={"options": f"-c lock_timeout={busy} -c statement_timeout={statement} -c timezone=UTC"},
        **engine_options,
    )


def dialect_of(bind) -> str:
    """``sqlite`` or ``postgresql``: the dialect of an engine, a connection
    or a session."""
    if isinstance(bind, Session):
        bind = bind.get_bind()
    return bind.dialect.name


engine = make_engine(database_url())

# A forked worker process (Celery prefork) must not share the sockets of the
# connections its parent opened.
os.register_at_fork(after_in_child=lambda: engine.dispose(close=False))


def alembic_config(url: URL) -> Config:
    """The Alembic configuration for the database at ``url``.
    ``migrations/env.py`` reads the URL from here, or resolves the same
    ``database_url()`` when Alembic runs from its own command line."""
    cfg = Config(str(_ALEMBIC_INI))
    # Absolute script_location so the call works regardless of CWD.
    cfg.set_main_option("script_location", str(_APP_ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    # Il processo che chiama init_db() ha già la propria configurazione di
    # logging: quella di alembic.ini vale solo per la CLI di Alembic.
    cfg.attributes["configure_logger"] = False
    return cfg


def head_revisions() -> set[str]:
    """The revisions the migrations end at: a database is up to date when it is at them."""
    return set(ScriptDirectory.from_config(alembic_config(database_url())).get_heads())


def upgrade_to_head(db_engine: Engine) -> None:
    """Bring the schema of ``db_engine`` up to head.

    Three cases:
    - Fresh DB: alembic builds everything from migration 0001 onward.
    - Pre-alembic SQLite DB (tables exist but no alembic_version, every
      install before migrations were introduced): stamp at 0001 (baseline),
      then run all subsequent migrations. Each migration is idempotent so
      it's safe regardless of whether manual patches (e.g. ALTER TABLE) were
      applied.
    - Already managed: applies any new pending revisions.
    """
    cfg = alembic_config(db_engine.url)
    tables = set(inspect(db_engine).get_table_names())

    if db_engine.dialect.name == SQLITE and tables and "alembic_version" not in tables:
        logger.info("Pre-alembic DB detected, stamping at 0001 before upgrade.")
        command.stamp(cfg, "0001")

    command.upgrade(cfg, "head")


def get_session():
    """Dependency FastAPI: fornisce una sessione DB per request."""
    with Session(engine) as session:
        yield session


@contextmanager
def atomic_session(*, commit: bool) -> Iterator[Session]:
    """A session whose work is ONE transaction, whatever commits it makes
    inside: kept when the block ends well and ``commit`` is True, undone
    otherwise (a dry run: the whole run, writes and commits included, leaves
    no trace; an error undoes it too).

    The session's commits release savepoints; the real one is the outer
    transaction. SQLite's driver opens no transaction before the first write,
    so one is opened by hand, or the first savepoint would end up committing."""
    with engine.connect() as connection:
        if connection.dialect.name == SQLITE:
            connection.exec_driver_sql("BEGIN")
        outer = connection.get_transaction() or connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                yield session
        except BaseException:
            outer.rollback()
            raise
        if commit:
            outer.commit()
        else:
            outer.rollback()


def read_snapshot(session: Session) -> None:
    """Every read of ``session`` that follows sees ONE snapshot of the
    database, until the session ends (or commits).

    Without it each SELECT sees whatever was committed until then (SQLite:
    its own implicit transaction; PostgreSQL: READ COMMITTED), so a route
    that filters in one query and computes its rows in others can see a job's
    commit in between (a track filtered as failed, then read as in the
    library).

    - SQLite: an explicit deferred ``BEGIN`` makes the reads one read
      transaction; in WAL mode it takes no lock a writer waits for.
    - PostgreSQL: a ``REPEATABLE READ``, read only transaction. It starts at
      the first statement, so a transaction a dependency has already opened
      (the sign-in lookup) is closed first: it only read.

    For read routes only ("reading never writes"). A no-op on SQLite when a
    transaction is already open."""
    if dialect_of(session) == POSTGRESQL:
        if session.in_transaction():
            session.rollback()
        session.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
        return
    connection = session.connection()
    raw = connection.connection.driver_connection
    if not raw.in_transaction:
        connection.exec_driver_sql("BEGIN")

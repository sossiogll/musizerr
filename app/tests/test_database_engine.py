"""
``db/engine.py``: where the data lives, the settings of every connection, the
single snapshot of a read route, the transaction of a dry run, and what the
application makes of a database that is busy.
"""
from __future__ import annotations

import threading

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from db import engine as db_engine
from db.engine import (
    POSTGRESQL,
    SQLITE,
    atomic_session,
    database_url,
    make_engine,
    parse_url,
    read_snapshot,
    upgrade_to_head,
)
from db.models import Setting, TrackRecord
from routes.errors import install_error_handlers, is_lock_timeout

from tests.database import DIALECT, new_engine


# ---------------------------------------------------------------------------
# Where the data lives
# ---------------------------------------------------------------------------


def test_without_database_url_the_data_is_the_sqlite_file_of_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    url = database_url()

    assert url.get_backend_name() == SQLITE
    assert url.database == str(tmp_path / "musizeer.db")


@pytest.mark.parametrize("given", [
    "postgresql://u:p@db:5432/m", "postgres://u:p@db:5432/m", "postgresql+psycopg://u:p@db:5432/m",
])
def test_database_url_names_the_installed_driver(monkeypatch, given):
    monkeypatch.setenv("DATABASE_URL", given)

    url = database_url()

    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.password, url.host, url.port, url.database) == ("u", "p", "db", 5432, "m")


def test_a_password_with_special_characters_survives():
    url = parse_url("postgresql://user:p%40ss%2Fw%3Ard@db/m")

    assert url.password == "p@ss/w:rd"


# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------


@pytest.mark.postgresql_only("PostgreSQL session settings")
def test_a_postgresql_connection_has_a_lock_timeout_a_statement_timeout_and_utc():
    engine = new_engine()
    with engine.connect() as connection:
        shown = {name: connection.execute(sa.text(f"SHOW {name}")).scalar() for name in
                 ("lock_timeout", "statement_timeout", "TimeZone")}

    assert shown == {"lock_timeout": "15s", "statement_timeout": "2min", "TimeZone": "UTC"}


@pytest.mark.postgresql_only("PostgreSQL session settings")
def test_the_timeouts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_BUSY_TIMEOUT_MS", "2500")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "0")
    engine = make_engine(new_engine().url)

    with engine.connect() as connection:
        assert connection.execute(sa.text("SHOW lock_timeout")).scalar() == "2500ms"
        assert connection.execute(sa.text("SHOW statement_timeout")).scalar() == "0"


@pytest.mark.postgresql_only("PostgreSQL pool")
def test_the_pool_is_sized_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")

    pool = make_engine(new_engine().url).pool

    assert (pool.size(), pool._max_overflow) == (2, 1)


# ---------------------------------------------------------------------------
# One snapshot for a read route
# ---------------------------------------------------------------------------


def _add_track(engine, title: str) -> None:
    with Session(engine) as session:
        session.add(TrackRecord(title=title, artist="A", album="B"))
        session.commit()


def test_a_snapshot_session_does_not_see_what_another_connection_commits_meanwhile():
    engine = new_engine(addressable=True)
    _add_track(engine, "first")
    with Session(engine) as snapshot, Session(engine) as plain:
        read_snapshot(snapshot)
        assert len(snapshot.exec(select(TrackRecord)).all()) == 1
        assert len(plain.exec(select(TrackRecord)).all()) == 1

        _add_track(engine, "second")  # committed by a third connection

        assert len(snapshot.exec(select(TrackRecord)).all()) == 1  # the snapshot holds
        plain.rollback()
        assert len(plain.exec(select(TrackRecord)).all()) == 2  # what a session does without it


def test_a_snapshot_can_start_after_a_dependency_already_read():
    engine = new_engine(addressable=True)
    _add_track(engine, "first")
    with Session(engine) as session:
        assert len(session.exec(select(TrackRecord)).all()) == 1  # the sign-in lookup of a request
        read_snapshot(session)
        assert len(session.exec(select(TrackRecord)).all()) == 1

        _add_track(engine, "second")

        assert len(session.exec(select(TrackRecord)).all()) == 1


@pytest.mark.postgresql_only("a PostgreSQL read only transaction")
def test_a_snapshot_is_read_only_and_the_connection_is_normal_again_afterwards():
    engine = new_engine(addressable=True)
    with Session(engine) as session:
        read_snapshot(session)
        with pytest.raises(sa.exc.InternalError, match="read-only"):
            session.add(TrackRecord(title="no", artist="A", album="B"))
            session.commit()
    # The pooled connection goes back to what it was.
    _add_track(engine, "yes")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SHOW transaction_isolation")).scalar() == "read committed"


# ---------------------------------------------------------------------------
# The transaction of a dry run
# ---------------------------------------------------------------------------


@pytest.fixture
def application_database(monkeypatch):
    engine = new_engine(addressable=True)
    monkeypatch.setattr(db_engine, "engine", engine)
    return engine


def _titles(engine) -> list[str]:
    with Session(engine) as session:
        return sorted(t.title for t in session.exec(select(TrackRecord)).all())


def test_a_dry_run_session_leaves_no_trace_whatever_it_commits(application_database):
    with atomic_session(commit=False) as session:
        session.add(TrackRecord(title="one", artist="A", album="B"))
        session.commit()
        session.add(TrackRecord(title="two", artist="A", album="B"))
        session.commit()
        assert len(session.exec(select(TrackRecord)).all()) == 2

    assert _titles(application_database) == []


def test_an_applied_session_keeps_everything(application_database):
    with atomic_session(commit=True) as session:
        session.add(TrackRecord(title="one", artist="A", album="B"))
        session.commit()
        session.add(TrackRecord(title="two", artist="A", album="B"))
        session.commit()

    assert _titles(application_database) == ["one", "two"]


def test_an_error_undoes_the_whole_session_even_when_applying(application_database):
    with pytest.raises(RuntimeError):
        with atomic_session(commit=True) as session:
            session.add(TrackRecord(title="one", artist="A", album="B"))
            session.commit()
            raise RuntimeError("stopped halfway")

    assert _titles(application_database) == []


# ---------------------------------------------------------------------------
# The schema of a database
# ---------------------------------------------------------------------------


@pytest.mark.postgresql_only("a PostgreSQL database never gets a baseline stamp")
def test_a_postgresql_database_with_tables_and_no_history_is_refused_not_stamped():
    engine = new_engine(addressable=True)  # the tables of the models, no alembic_version

    with pytest.raises(Exception):
        upgrade_to_head(engine)

    assert "alembic_version" not in sa.inspect(engine).get_table_names()


def test_upgrading_an_up_to_date_database_changes_nothing():
    engine = new_engine(schema=False, addressable=True)
    upgrade_to_head(engine)
    upgrade_to_head(engine)

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM alembic_version")).scalar() == 1


# ---------------------------------------------------------------------------
# A database that is busy is a retryable 503, not a 500
# ---------------------------------------------------------------------------


def _client_that_raises(error: Exception) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise error

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.sqlite_only("SQLite's lock message")
def test_a_sqlite_lock_is_database_busy():
    error = OperationalError("UPDATE x", {}, Exception("database is locked"))

    response = _client_that_raises(error).get("/boom")

    assert is_lock_timeout(error)
    assert response.status_code == 503 and response.json()["detail"]["error_code"] == "database_busy"
    assert response.headers["retry-after"]


@pytest.mark.postgresql_only("PostgreSQL error codes")
def test_a_lock_timeout_is_database_busy():
    engine = new_engine(addressable=True)
    quick = make_engine(engine.url, busy_timeout_ms=200)
    with Session(engine) as holder:
        holder.add(Setting(key="k", scope="system", value="1"))
        holder.commit()
        holder.exec(select(Setting).with_for_update()).all()  # the row is locked

        with pytest.raises(OperationalError) as caught:
            with Session(quick) as waiter:
                waiter.exec(sa.update(Setting).values(value="2"))
        assert caught.value.orig.sqlstate == "55P03"

    assert is_lock_timeout(caught.value)
    response = _client_that_raises(caught.value).get("/boom")
    assert response.status_code == 503 and response.json()["detail"]["error_code"] == "database_busy"


@pytest.mark.postgresql_only("PostgreSQL error codes")
def test_a_statement_timeout_is_database_busy():
    quick = make_engine(new_engine().url, statement_timeout_ms=100)

    with pytest.raises(OperationalError) as caught:
        with quick.connect() as connection:
            connection.execute(sa.text("SELECT pg_sleep(2)"))

    assert caught.value.orig.sqlstate == "57014" and is_lock_timeout(caught.value)


@pytest.mark.postgresql_only("PostgreSQL error codes")
def test_a_deadlock_is_database_busy():
    engine = new_engine(addressable=True)
    with Session(engine) as session:
        session.add_all([Setting(key="a", scope="system", value="1"), Setting(key="b", scope="system", value="1")])
        session.commit()
    first_locked, second_locked = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def cross(mine: str, theirs: str, mine_done: threading.Event, theirs_done: threading.Event) -> None:
        try:
            with Session(engine) as session:
                session.exec(sa.update(Setting).where(Setting.key == mine).values(value="x"))
                mine_done.set()
                theirs_done.wait(timeout=10)
                session.exec(sa.update(Setting).where(Setting.key == theirs).values(value="x"))
                session.commit()
        except OperationalError as error:
            errors.append(error)

    threads = [
        threading.Thread(target=cross, args=("a", "b", first_locked, second_locked)),
        threading.Thread(target=cross, args=("b", "a", second_locked, first_locked)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(errors) == 1 and errors[0].orig.sqlstate == "40P01"
    assert is_lock_timeout(errors[0])


@pytest.mark.postgresql_only("PostgreSQL error codes")
def test_a_serialization_failure_is_database_busy():
    engine = new_engine(addressable=True)
    with Session(engine) as session:
        session.add(Setting(key="a", scope="system", value="1"))
        session.commit()
    with Session(engine) as first, Session(engine) as second:
        for session in (first, second):
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.exec(select(Setting)).all()
        first.exec(sa.update(Setting).values(value="first"))
        first.commit()

        with pytest.raises(OperationalError) as caught:
            second.exec(sa.update(Setting).values(value="second"))

    assert caught.value.orig.sqlstate == "40001" and is_lock_timeout(caught.value)


def test_another_database_error_is_not_database_busy():
    assert not is_lock_timeout(OperationalError("SELECT 1", {}, Exception("no such table: x")))
    assert not is_lock_timeout(ValueError("database is locked"))


def test_this_suite_runs_on_the_dialect_it_says():
    engine = new_engine()

    assert engine.dialect.name == DIALECT
    assert DIALECT in (SQLITE, POSTGRESQL)

"""
Serializing writers on a name.

SQLite lets one writer at a time in, so a "look, then write" sequence is
only as safe as the moment it takes the write lock. PostgreSQL lets writers
in parallel (READ COMMITTED): two transactions can both look, find nothing
and both insert. Where a unique constraint cannot express the rule (a track
that two identifiers resolve to, a download request per track, a routine run
per period) the writer takes ``xact_lock`` first: the check and the write that
follows it then run one writer at a time, on both databases.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlmodel import Session

from .engine import POSTGRESQL, SQLITE, dialect_of


def _lock_id(name: str) -> int:
    """The 64-bit key PostgreSQL advisory locks are taken on, derived from
    the name (stable across processes)."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def advisory_xact_lock(connection: Connection, name: str) -> None:
    """PostgreSQL: wait for the advisory lock ``name`` and hold it until the
    end of the transaction of ``connection``."""
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_id(name)})


def xact_lock(session: Session, name: str) -> None:
    """Wait for the lock ``name`` and hold it until the transaction of
    ``session`` ends (its next commit or rollback). Whoever held it has
    committed by the time this returns, so the reads that follow see its
    writes.

    - PostgreSQL: an advisory lock on the name (writers of other names go
      on in parallel).
    - SQLite: the write lock of the database, taken now instead of at the
      first write (``BEGIN IMMEDIATE``); a transaction that has already
      written holds it.
    """
    dialect = dialect_of(session)
    if dialect == POSTGRESQL:
        advisory_xact_lock(session.connection(), name)
    elif dialect == SQLITE:
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")

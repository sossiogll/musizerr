"""
Writes that stay correct when two writers race: the database decides, not a
"look first" query.

A "SELECT, then INSERT if nothing" pair is only safe when one writer at a time
gets in. Both databases support ``INSERT ... ON CONFLICT`` (SQLite, PostgreSQL):
the unique constraint is the arbiter and the loser of a race simply does not
insert (or updates). The constraint itself lives on the model.
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql.elements import TextClause
from sqlmodel import Session, SQLModel

from .engine import POSTGRESQL, dialect_of


def _insert(session: Session, model: type[SQLModel]):
    return (postgresql_insert if dialect_of(session) == POSTGRESQL else sqlite_insert)(model)


def _values(record: SQLModel) -> dict:
    return {attr.key: getattr(record, attr.key) for attr in inspect(type(record)).mapper.column_attrs}


def insert_ignore(session: Session, *records: SQLModel) -> int:
    """Insert ``records`` that do not violate a unique constraint; those that
    do are skipped (``ON CONFLICT DO NOTHING``). Returns how many went in.
    Nothing is committed. The records are not attached to the session:
    read them back if they are needed."""
    if not records:
        return 0
    session.flush()
    model = type(records[0])
    statement = _insert(session, model).values([_values(record) for record in records])
    # RETURNING, not rowcount: a bulk insert reports none.
    inserted = session.execute(statement.on_conflict_do_nothing().returning(*inspect(model).primary_key))
    return len(inserted.all())


def upsert(
    session: Session,
    record: SQLModel,
    *,
    conflict: Sequence[str],
    update: Sequence[str],
    where: Optional[TextClause] = None,
) -> None:
    """Insert ``record``, or, if its ``conflict`` columns already name a row,
    set that row's ``update`` columns to the record's values. ``where`` is the
    predicate of a partial unique index the conflict columns belong to.
    Nothing is committed."""
    values = _values(record)
    statement = _insert(session, type(record)).values(values)
    session.flush()
    session.execute(statement.on_conflict_do_update(
        index_elements=list(conflict),
        index_where=where,
        set_={column: values[column] for column in update},
    ))

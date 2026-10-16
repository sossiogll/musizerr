"""
SQL that reads the same on SQLite and PostgreSQL.

- ``ascending`` / ``descending``: the order of a nullable column. SQLite sorts
  NULL as the smallest value (first when ascending, last when descending);
  PostgreSQL sorts it as the largest. The order the application shows is the
  first one on both, written out instead of left to the engine.
- ``json_number``: a number inside a JSON document kept as text.
"""
from __future__ import annotations

import re

from sqlalchemy import Float
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement, FunctionElement


def ascending(column: ColumnElement) -> ColumnElement:
    """Ascending, NULL first."""
    return column.asc().nulls_first()


def descending(column: ColumnElement) -> ColumnElement:
    """Descending, NULL last."""
    return column.desc().nulls_last()


_JSON_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class json_number(FunctionElement):
    """The number at ``key`` of the JSON object in ``column`` (text); NULL
    when the document has no such key or it is not a number. ``key`` is part
    of the SQL text: a plain identifier, never user input."""

    type = Float()
    inherit_cache = True
    name = "json_number"

    def __init__(self, column: ColumnElement, key: str) -> None:
        if not _JSON_KEY.match(key):
            raise ValueError(f"not a JSON key: {key!r}")
        super().__init__(column)
        self.key = key


@compiles(json_number, "sqlite")
def _json_number_sqlite(element: json_number, compiler, **kw) -> str:
    document = compiler.process(element.clauses.clauses[0], **kw)
    return f"CASE WHEN json_type({document}, '$.{element.key}') IN ('integer', 'real') THEN json_extract({document}, '$.{element.key}') END"


@compiles(json_number, "postgresql")
def _json_number_postgresql(element: json_number, compiler, **kw) -> str:
    document = f"CAST({compiler.process(element.clauses.clauses[0], **kw)} AS jsonb)"
    return (
        f"CASE WHEN jsonb_typeof({document} -> '{element.key}') = 'number' "
        f"THEN CAST({document} ->> '{element.key}' AS DOUBLE PRECISION) END"
    )

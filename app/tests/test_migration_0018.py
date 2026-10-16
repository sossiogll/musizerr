"""
Migrazione 0018: ``duplicate_members.group_id`` resta con un solo indice, lo
stesso che il modello dichiara; il downgrade rimette il secondo.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from db.engine import alembic_config
from db.models import DuplicateMember

from tests.database import new_engine


def _group_id_indexes(engine) -> list[str]:
    return sorted(
        index["name"] for index in sa.inspect(engine).get_indexes("duplicate_members")
        if index["column_names"] == ["group_id"]
    )


def test_one_index_on_group_id_matching_the_model():
    engine = new_engine(schema=False, addressable=True)
    cfg = alembic_config(engine.url)

    command.upgrade(cfg, "0018_drop_duplicate_members_group_index")
    model_indexes = sorted(i.name for i in DuplicateMember.__table__.indexes if [c.name for c in i.columns] == ["group_id"])
    assert _group_id_indexes(engine) == model_indexes == ["ix_duplicate_members_group_id"]

    command.downgrade(cfg, "0017_retry_unidentified_fingerprints")
    assert _group_id_indexes(engine) == ["ix_duplicate_members_group", "ix_duplicate_members_group_id"]

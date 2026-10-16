"""
Migration 0039: provider sources v2. The sources a database holds are kept (with no credential and
no insecure registry); the pip-era installed providers are dropped and the table has the new
shape (an image pinned by digest, what was approved, who approved it); the downgrade goes back
to the old shape, empty.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.database import migration_database

NOW = datetime(2026, 10, 13, 12, 0)


@pytest.fixture
def db():
    return migration_database("0038_playlists")


def _source(conn, source_id="s1", url="http://registry:9000/registry.json"):
    conn.execute(sa.text(
        "INSERT INTO provider_sources (id, name, url, enabled, added_at) VALUES (:i, 'Official', :u, :e, :n)"),
        {"i": source_id, "u": url, "e": True, "n": NOW})


def _old_installed(conn, name="oldprov", source_id="s1"):
    conn.execute(sa.text(
        "INSERT INTO installed_providers (id, source_id, name, package, version, types, installed_at) "
        "VALUES (:i, :s, :n, 'musizerr-provider-x', '1.0', '[\"metadata\"]', :t)"),
        {"i": f"i-{name}", "s": source_id, "n": name, "t": NOW})


def _new_installed(conn, name="lrclib", source_id="s1", **extra):
    values = {
        "id": f"i-{name}", "source_id": source_id, "name": name, "image": "ghcr.io/o/x", "digest": "sha256:" + "a" * 64,
        "provider_api_version": 5, "label": "X", "roles": '["lyrics"]', "permissions": "{}",
        "approved_at": NOW, "installed_at": NOW, **extra,
    }
    conn.execute(sa.text(
        f"INSERT INTO installed_providers ({', '.join(values)}) VALUES ({', '.join(':' + k for k in values)})"), values)


def test_the_sources_are_kept_with_no_credential_and_no_insecure_registry(db):
    engine, cfg = db
    with engine.begin() as conn:
        _source(conn)
        _source(conn, "s2", "https://example.org/catalog.json")

    command.upgrade(cfg, "0039_provider_sources_v2")

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, name, url, enabled, credential, allow_insecure_registry FROM provider_sources ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [
        ("s1", "Official", "http://registry:9000/registry.json", True, None, False),
        ("s2", "Official", "https://example.org/catalog.json", True, None, False),
    ]


def test_the_pip_era_installed_providers_are_dropped(db):
    engine, cfg = db
    with engine.begin() as conn:
        _source(conn)
        _old_installed(conn, "oldprov")
        _old_installed(conn, "other")

    command.upgrade(cfg, "0039_provider_sources_v2")

    columns = {c["name"] for c in sa.inspect(engine).get_columns("installed_providers")}
    assert {"image", "digest", "provider_api_version", "label", "roles", "permissions", "approved_at", "approved_by", "installed_at"} <= columns
    assert not columns & {"package", "version", "types"}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM installed_providers")).scalar() == 0
        assert conn.execute(sa.text("SELECT count(*) FROM provider_sources")).scalar() == 1


def test_an_installed_provider_is_unique_by_name_and_belongs_to_a_source(db):
    engine, cfg = db
    command.upgrade(cfg, "0039_provider_sources_v2")
    with engine.begin() as conn:
        _source(conn)
        _new_installed(conn, "lrclib")

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _new_installed(conn, "lrclib", id="another-id")

    foreign_keys = sa.inspect(engine).get_foreign_keys("installed_providers")
    assert [(fk["constrained_columns"], fk["referred_table"]) for fk in foreign_keys] == [(["source_id"], "provider_sources")]
    indexes = {i["name"] for i in sa.inspect(engine).get_indexes("installed_providers")}
    assert "ix_installed_providers_source_id" in indexes


def test_approved_by_is_optional_and_has_no_foreign_key(db):
    engine, cfg = db
    command.upgrade(cfg, "0039_provider_sources_v2")
    with engine.begin() as conn:
        _source(conn)
        _new_installed(conn, "shell")
        _new_installed(conn, "web", approved_by="a-user-that-no-longer-exists")

    foreign_keys = sa.inspect(engine).get_foreign_keys("installed_providers")
    assert [fk["referred_table"] for fk in foreign_keys] == ["provider_sources"]


def test_the_downgrade_goes_back_to_the_old_shape_and_keeps_the_sources(db):
    engine, cfg = db
    command.upgrade(cfg, "0039_provider_sources_v2")
    with engine.begin() as conn:
        _source(conn)
        _new_installed(conn)

    command.downgrade(cfg, "0038_playlists")

    inspector = sa.inspect(engine)
    assert {c["name"] for c in inspector.get_columns("installed_providers")} >= {"package", "version", "types"}
    assert not {c["name"] for c in inspector.get_columns("provider_sources")} & {"credential", "allow_insecure_registry"}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM installed_providers")).scalar() == 0
        assert conn.execute(sa.text("SELECT count(*) FROM provider_sources")).scalar() == 1
    command.upgrade(cfg, "0039_provider_sources_v2")

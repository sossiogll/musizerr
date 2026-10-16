"""
Fase 4a: ``db/settings_store.py`` (``SettingsRepository``) — l'unico punto di
accesso alla tabella ``settings`` che sostituisce le tre fonti sparse di
Fase 3b. Nessun nome di provider concreto qui: sono chiavi generiche, il
repository non sa nulla di "provider" (il namespacing è un affare di
``core/settings_resolver.py``, esercitato altrove).
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from db.models import Setting
from db.settings_store import SettingsRepository

from tests.database import new_engine, seed_parents


@pytest.fixture
def session():
    engine = new_engine()
    with Session(engine) as s:
        seed_parents(s, users=("user-1", "user-2", "user-a", "user-b"))
        yield s


@pytest.fixture
def repo(session):
    return SettingsRepository(session)


# ---------------------------------------------------------------------------
# Cascata: utente -> sistema -> default dichiarato
# ---------------------------------------------------------------------------


def test_resolve_falls_through_to_declared_default_when_nothing_is_stored(repo):
    assert repo.resolve("some_key", user_id="user-1", default="fallback") == "fallback"
    assert repo.resolve("some_key", user_id=None, default="fallback") == "fallback"


def test_resolve_uses_system_row_when_no_user_row_exists(repo):
    repo.set_system("some_key", ["global", "value"])

    assert repo.resolve("some_key", user_id="user-1", default=None) == ["global", "value"]
    assert repo.resolve("some_key", user_id=None, default=None) == ["global", "value"]


def test_resolve_user_row_shadows_system_row(repo):
    repo.set_system("some_key", "system-value")
    repo.set_user("some_key", "user-1", "personal-value")

    # L'utente con un override personale lo vede al posto del valore di sistema...
    assert repo.resolve("some_key", user_id="user-1", default=None) == "personal-value"
    # ...un altro utente, senza un proprio override, eredita ancora il sistema.
    assert repo.resolve("some_key", user_id="user-2", default=None) == "system-value"


def test_get_system_and_get_user_are_scoped_independently(repo):
    repo.set_system("k", "system-only")
    assert repo.get_user("k", "user-1") is None  # nessuna riga utente ancora
    repo.set_user("k", "user-1", "user-only")
    assert repo.get_system("k") == "system-only"  # scrivere l'utente non tocca il sistema
    assert repo.get_user("k", "user-1") == "user-only"


# ---------------------------------------------------------------------------
# Unicità su (key, scope, user_id): due utenti hanno valori indipendenti;
# scrivere due volte per la STESSA tripla aggiorna, non duplica.
# ---------------------------------------------------------------------------


def test_two_users_hold_independent_values_for_the_same_key(repo, session):
    repo.set_user("shared_key", "user-a", "value-a")
    repo.set_user("shared_key", "user-b", "value-b")

    assert repo.get_user("shared_key", "user-a") == "value-a"
    assert repo.get_user("shared_key", "user-b") == "value-b"

    rows = session.exec(select(Setting).where(Setting.key == "shared_key")).all()
    assert len(rows) == 2
    assert {r.user_id for r in rows} == {"user-a", "user-b"}


def test_writing_the_same_triple_twice_updates_in_place_without_duplicating(repo, session):
    repo.set_system("k", "first")
    repo.set_system("k", "second")

    rows = session.exec(select(Setting).where(Setting.key == "k", Setting.scope == "system")).all()
    assert len(rows) == 1
    assert rows[0].value == '"second"'
    assert repo.get_system("k") == "second"


def test_writing_the_same_user_triple_twice_updates_in_place_without_duplicating(repo, session):
    repo.set_user("k", "user-1", "first")
    repo.set_user("k", "user-1", "second")

    rows = session.exec(
        select(Setting).where(Setting.key == "k", Setting.scope == "user", Setting.user_id == "user-1")
    ).all()
    assert len(rows) == 1
    assert repo.get_user("k", "user-1") == "second"


# ---------------------------------------------------------------------------
# all_system / all_user
# ---------------------------------------------------------------------------


def test_all_system_returns_every_system_row_and_excludes_user_rows(repo):
    repo.set_system("a", 1)
    repo.set_system("b", 2)
    repo.set_user("a", "user-1", 999)

    assert repo.all_system() == {"a": 1, "b": 2}


def test_all_user_returns_only_that_users_rows(repo):
    repo.set_user("a", "user-1", "1a")
    repo.set_user("b", "user-1", "1b")
    repo.set_user("a", "user-2", "2a")

    assert repo.all_user("user-1") == {"a": "1a", "b": "1b"}
    assert repo.all_user("user-2") == {"a": "2a"}

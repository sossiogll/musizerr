"""
``db/adoption.py``: what the open-mode owner (the synthetic user) owned becomes an administrator's
when authentication is turned on. The rules (docs/permissions.md):

- every foreign key to ``users`` is classified (moves, or deliberately stays), so a table added
  later cannot be forgotten;
- the personal data moves, the sessions and policies of an account do not;
- a row the administrator already has an equivalent of stays with the synthetic user (their own wins,
  nothing is merged or deleted);
- running it again changes nothing;
- the owner's accounts move only for a provider whose model is ``personal``.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select as core_select
from sqlmodel import Session, SQLModel, select

from db import adoption, user_service
from db.models import (
    SYSTEM_USER_ID,
    Download,
    DownloadFollower,
    DownloadRequest,
    LibraryJobRun,
    PendingLike,
    Playlist,
    PlaylistSync,
    PlaylistTrack,
    ProviderCredential,
    RemovedFavorite,
    Setting,
    User,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)

from tests.auth_modes_support import owner_data, owned_by, sessions_of_the_synthetic_user
from tests.database import seed_parents, seed_system_user

ADMIN = "admin-1"


@pytest.fixture
def session(engine):
    seed_system_user(engine)
    with Session(engine) as s:
        seed_parents(s, users=[ADMIN])
        yield s


def _count(session: Session, table: str, column: str, user_id: str) -> int:
    meta = SQLModel.metadata.tables[table]
    return session.execute(core_select(func.count()).select_from(meta).where(meta.c[column] == user_id)).scalar_one()


# ---------------------------------------------------------------------------
# The classification: every foreign key to users says whether it moves
# ---------------------------------------------------------------------------


def _foreign_keys_to_users() -> set[tuple[str, str]]:
    return {
        (table.name, fk.parent.name)
        for table in SQLModel.metadata.tables.values() for fk in table.foreign_keys if fk.column.table.name == "users"
    }


def test_every_foreign_key_to_a_user_is_classified_as_adopted_or_deliberately_not():
    classified = set(adoption.ADOPTED) | set(adoption.NOT_ADOPTED)

    unclassified = _foreign_keys_to_users() - classified
    assert not unclassified, (
        f"{sorted(unclassified)} point at users and are neither in db.adoption.ADOPTED (the open-mode owner's data "
        "that moves to the administrator when authentication is turned on) nor in NOT_ADOPTED (with the reason)"
    )
    assert classified <= _foreign_keys_to_users(), "a classified column is not a foreign key to users any more"


def test_a_column_is_adopted_or_not_never_both_and_every_exception_says_why():
    assert not set(adoption.ADOPTED) & set(adoption.NOT_ADOPTED)
    assert all(reason.strip() for reason in adoption.NOT_ADOPTED.values())
    assert len(adoption.ADOPTED) == len(set(adoption.ADOPTED))


def test_the_columns_that_are_deleted_with_an_account_are_the_data_that_adoption_moves_or_a_stated_exception():
    """The two lists describe the same tables from two sides: what is deleted with an account
    (``user_service.ACCOUNT_DATA``) is moved with the owner's data, or is a stated exception."""
    deleted = {(model.__tablename__, "user_id") for model in user_service.ACCOUNT_DATA}
    assert deleted <= set(adoption.ADOPTED) | set(adoption.NOT_ADOPTED)


# ---------------------------------------------------------------------------
# What moves
# ---------------------------------------------------------------------------


def test_every_adopted_column_moves_to_the_administrator(session):
    owner_data(session, SYSTEM_USER_ID, tag="owner")

    report = adoption.adopt(session, ADMIN)
    session.commit()

    for table, column in adoption.ADOPTED:
        assert _count(session, table, column, SYSTEM_USER_ID) == _expected_left(table, column), f"{table}.{column}"
        assert _count(session, table, column, ADMIN) >= 1, f"{table}.{column} moved nothing"
    assert report.left == {"library_job_runs.user_id": 1}  # the job still running; nothing else is left
    assert not report.cancelled_requests and not report.accounts_moved


def _expected_left(table: str, column: str) -> int:
    """A run that is still queued or running stays with the job's worker."""
    return 1 if table == "library_job_runs" else 0


def test_what_hangs_from_a_moved_row_goes_with_it(session):
    ids = owner_data(session, SYSTEM_USER_ID, tag="owner")

    adoption.adopt(session, ADMIN)
    session.commit()

    assert [p.provider for p in session.exec(
        select(UserSavedTrackProvider).where(UserSavedTrackProvider.saved_track_id == ids["saved"])).all()] == ["p"]
    assert session.get(UserSavedTrack, ids["saved"]).user_id == ADMIN
    for playlist in ("own_playlist", "followed_playlist"):
        assert session.get(Playlist, ids[playlist]).user_id == ADMIN
        assert len(session.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == ids[playlist])).all()) == 1
        assert [s.user_id for s in session.exec(select(PlaylistSync).where(PlaylistSync.playlist_id == ids[playlist])).all()] == [ADMIN]
    # The copy of a playlist that is gone is the user's, so the routine can delete it from the administrator's account.
    assert [s.user_id for s in session.exec(select(PlaylistSync).where(PlaylistSync.playlist_id == "owner-deleted")).all()] == [ADMIN]


def test_the_reference_columns_are_kept_in_step(session):
    ids = owner_data(session, SYSTEM_USER_ID, tag="owner")

    adoption.adopt(session, ADMIN)
    session.commit()

    assert session.get(Download, ids["download"]).requested_by == ADMIN
    request = session.get(DownloadRequest, ids["request"])
    assert (request.user_id, request.decided_by) == (ADMIN, ADMIN)
    assert session.get(DownloadRequest, ids["pending_request"]).status == "pending"  # a request that waits still waits
    assert [f.user_id for f in session.exec(select(DownloadFollower)).all()] == [ADMIN]


def test_the_personal_settings_of_the_owner_become_the_administrators(session):
    session.add_all([
        Setting(key="theme", scope="user", user_id=SYSTEM_USER_ID, value='"dark"'),
        Setting(key="language", scope="user", user_id=SYSTEM_USER_ID, value='"it"'),
        Setting(key="save_cover", scope="system", user_id=None, value="true"),  # not anybody's
    ])
    session.commit()

    adoption.adopt(session, ADMIN)
    session.commit()

    mine = {s.key: json.loads(s.value) for s in session.exec(select(Setting).where(Setting.user_id == ADMIN)).all()}
    assert mine == {"theme": "dark", "language": "it"}
    assert [s.key for s in session.exec(select(Setting).where(Setting.scope == "system")).all()] == ["save_cover"]


# ---------------------------------------------------------------------------
# What does not move
# ---------------------------------------------------------------------------


def test_the_sessions_and_policies_of_the_synthetic_user_stay_with_it(session):
    sessions_of_the_synthetic_user(session)

    adoption.adopt(session, ADMIN)
    session.commit()

    for table, column in adoption.NOT_ADOPTED:
        if table == "provider_credentials":
            continue
        assert _count(session, table, column, SYSTEM_USER_ID) == 1, table
        assert _count(session, table, column, ADMIN) == 0, table


def test_a_run_still_queued_or_running_stays_with_its_worker_and_a_finished_one_moves(session):
    session.add_all([
        LibraryJobRun(user_id=SYSTEM_USER_ID, kind="library_sync", status="queued"),
        LibraryJobRun(user_id=SYSTEM_USER_ID, kind="download_sync", status="failed"),
        LibraryJobRun(user_id=SYSTEM_USER_ID, kind="download_sync", status="completed"),
    ])
    session.commit()

    adoption.adopt(session, ADMIN)
    session.commit()

    assert sorted(r.status for r in owned_by(session, LibraryJobRun, ADMIN)) == ["completed", "failed"]
    assert [r.status for r in owned_by(session, LibraryJobRun, SYSTEM_USER_ID)] == ["queued"]


def test_other_accounts_data_is_never_touched(session):
    seed_parents(session, users=["alice"])
    owner_data(session, "alice", tag="alice")
    owner_data(session, SYSTEM_USER_ID, tag="owner")

    adoption.adopt(session, ADMIN)
    session.commit()

    assert len(owned_by(session, UserFavorite, "alice")) == 1
    assert len(owned_by(session, Playlist, "alice")) == 2
    assert len(owned_by(session, Setting, "alice")) == 1


# ---------------------------------------------------------------------------
# The administrator already has an equivalent: their own wins, nothing is lost
# ---------------------------------------------------------------------------


def test_a_row_the_administrator_already_has_stays_with_the_synthetic_user(session):
    ids = owner_data(session, SYSTEM_USER_ID, tag="owner")
    owner_data(session, ADMIN, tag="admin", shared_with="owner")  # the same favorite, removal, like, setting, source, target

    report = adoption.adopt(session, ADMIN)
    session.commit()

    # Held by both: the administrator's own is untouched, the owner's stays where it was.
    assert [f.track_id for f in owned_by(session, UserFavorite, ADMIN)] == ["owner-t1"]
    assert [f.track_id for f in owned_by(session, UserFavorite, SYSTEM_USER_ID)] == ["owner-t1"]
    assert len(owned_by(session, RemovedFavorite, ADMIN)) == 1 and len(owned_by(session, RemovedFavorite, SYSTEM_USER_ID)) == 1
    assert len(owned_by(session, UserSavedTrack, SYSTEM_USER_ID)) == 1 and len(owned_by(session, PendingLike, SYSTEM_USER_ID)) == 1
    theme = lambda user: [json.loads(s.value) for s in owned_by(session, Setting, user)]  # noqa: E731
    assert theme(ADMIN) == ["admin-theme"] and theme(SYSTEM_USER_ID) == ["owner-theme"]
    # The followed playlist of a source the administrator follows already stays, with its tracks and its copy.
    assert session.get(Playlist, ids["followed_playlist"]).user_id == SYSTEM_USER_ID
    assert len(session.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == ids["followed_playlist"])).all()) == 1
    assert [s.user_id for s in session.exec(select(PlaylistSync).where(PlaylistSync.playlist_id == ids["followed_playlist"])).all()] == [SYSTEM_USER_ID]
    # The own playlist (no source) always moves: two own playlists of the same name are fine.
    assert session.get(Playlist, ids["own_playlist"]).user_id == ADMIN
    assert report.left["user_favorites.user_id"] == 1 and report.left["playlists.user_id"] == 1


def test_a_pending_request_the_administrator_has_a_twin_of_is_cancelled_then_moves(session):
    owner_data(session, SYSTEM_USER_ID, tag="owner")
    twin = owner_data(session, ADMIN, tag="admin", shared_with="owner")

    report = adoption.adopt(session, ADMIN)
    session.commit()

    assert report.cancelled_requests == 1
    assert session.get(DownloadRequest, twin["pending_request"]).status == "pending"  # the administrator's own
    mine = {r.status for r in owned_by(session, DownloadRequest, ADMIN)}
    assert mine == {"approved", "pending", "cancelled"}
    assert not owned_by(session, DownloadRequest, SYSTEM_USER_ID)


# ---------------------------------------------------------------------------
# Idempotent
# ---------------------------------------------------------------------------


def _snapshot(session: Session) -> dict[str, list]:
    session.expire_all()
    return {
        table: sorted(tuple(row) for row in session.execute(core_select(SQLModel.metadata.tables[table])).all())
        for table, _ in adoption.ADOPTED
    }


def test_running_it_again_changes_nothing(session):
    owner_data(session, SYSTEM_USER_ID, tag="owner")
    owner_data(session, ADMIN, tag="admin", shared_with="owner")
    adoption.adopt(session, ADMIN)
    session.commit()
    first = _snapshot(session)

    again = adoption.adopt(session, ADMIN)
    session.commit()

    assert again.total_moved == 0 and again.cancelled_requests == 0
    assert _snapshot(session) == first


def test_data_written_after_the_first_run_is_taken_by_the_next(session):
    adoption.adopt(session, ADMIN)
    session.commit()
    seed_parents(session, tracks=["late"])
    session.add(UserFavorite(user_id=SYSTEM_USER_ID, track_id="late"))
    session.commit()

    adoption.adopt(session, ADMIN)
    session.commit()

    assert [f.track_id for f in owned_by(session, UserFavorite, ADMIN)] == ["late"]


def test_an_instance_with_nothing_to_adopt_adopts_nothing(session):
    report = adoption.adopt(session, ADMIN)

    assert report.total_moved == 0 and report.left == {} and report.moved == {}


# ---------------------------------------------------------------------------
# The owner's accounts
# ---------------------------------------------------------------------------


def _credential(session: Session, provider: str, kind: str, user_id: str | None, data: str = "sealed") -> None:
    session.add(ProviderCredential(provider=provider, credential_type=kind, user_id=user_id, data=data, external_id=None))


def test_only_the_owners_accounts_of_a_personal_only_provider_move(session):
    _credential(session, "likes", "oauth2_token", None)  # connected by the owner: stored as the shared row
    _credential(session, "likes", "oauth2_client", None)  # the app, always the instance's
    _credential(session, "mixed", "oauth2_token", None)  # personal_with_shared: the shared account stays shared
    _credential(session, "library", "credentials", None)  # system model
    session.commit()

    report = adoption.adopt(session, ADMIN, personal_only_providers=["likes"])
    session.commit()

    assert report.accounts_moved == 1
    owned = {(c.provider, c.credential_type): c.user_id for c in session.exec(select(ProviderCredential)).all()}
    assert owned == {
        ("likes", "oauth2_token"): ADMIN,
        ("likes", "oauth2_client"): None,
        ("mixed", "oauth2_token"): None,
        ("library", "credentials"): None,
    }


def test_no_personal_copy_of_a_shared_account_is_made(session):
    _credential(session, "mixed", "oauth2_token", None)
    session.commit()

    adoption.adopt(session, ADMIN, personal_only_providers=[])
    session.commit()

    assert [c.user_id for c in session.exec(select(ProviderCredential)).all()] == [None]


def test_an_account_the_administrator_connected_already_wins(session):
    _credential(session, "likes", "oauth2_token", None, data="owner")
    _credential(session, "likes", "oauth2_token", ADMIN, data="admin")
    session.commit()

    adoption.adopt(session, ADMIN, personal_only_providers=["likes"])
    session.commit()

    assert {(c.user_id, c.data) for c in session.exec(select(ProviderCredential)).all()} == {(None, "owner"), (ADMIN, "admin")}


def test_a_moved_account_keeps_its_sealed_text_and_never_an_external_id(session):
    row = ProviderCredential(provider="likes", credential_type="credentials", user_id=None, data="enc:v1:xyz", external_id="stale")
    session.add(row)
    session.commit()

    adoption.adopt(session, ADMIN, personal_only_providers=["likes"])
    session.commit()
    session.refresh(row)

    assert (row.user_id, row.data, row.external_id) == (ADMIN, "enc:v1:xyz", None)


# ---------------------------------------------------------------------------
# Who can take the data
# ---------------------------------------------------------------------------


def test_an_administrator_who_can_sign_in_is_active_approved_with_a_password_and_not_the_synthetic_user(engine):
    seed_system_user(engine)
    with Session(engine) as session:
        session.add_all([
            User(username="ok", password_hash="x", role="admin"),
            User(username="user", password_hash="x", role="user"),
            User(username="off", password_hash="x", role="admin", is_active=False),
            User(username="waiting", password_hash="x", role="admin", is_active=False, pending_approval=True),
            User(username="nopass", password_hash="", role="admin"),
        ])
        session.commit()

        assert [u.username for u in user_service.administrators(session)] == ["ok"]
        assert user_service.has_usable_administrator(session)


def test_the_synthetic_user_is_never_an_administrator_one_can_sign_in_as(engine):
    seed_system_user(engine)
    with Session(engine) as session:
        assert not user_service.has_usable_administrator(session)
        assert user_service.administrators(session) == []

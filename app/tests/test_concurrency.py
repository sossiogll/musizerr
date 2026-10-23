"""
Writers racing on PostgreSQL.

SQLite lets one writer at a time in, so a "look, then write" sequence never
saw another writer in between. PostgreSQL lets writers run in parallel: two of
them can both look, find nothing and both insert. Every find-or-create, upsert
and claim of the application is run here by several threads released at the
same moment, each with a connection of its own, many times over; a lost race
would show as a duplicate row, an exception (a unique violation) or two
winners of one claim.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from db import (
    favorite_store, library_job_store, pending_like_store, routine_run_store, settings_store, user_service,
)
from db.auth_store import CredentialStore
from db.download_store import add_follower
from db.models import (
    Download,
    DownloadFollower,
    DownloadRequest,
    LibraryEntry,
    PendingLike,
    Playlist,
    PlaylistTrack,
    ProviderCredential,
    RemovedFavorite,
    RoutineRun,
    Setting,
    TrackIdentifierRecord,
    TrackRecord,
    User,
    UserFavorite,
)
from db.track_store import add_identifiers, find_or_create_track
from services.config_manager import ConfigManager
from services import downloads, library_jobs, playlists
from workers import tasks

from tests.conftest import FakeRegistry, make_track
from tests.database import new_engine, seed_parents

pytestmark = pytest.mark.postgresql_only("concurrent writers: SQLite serializes them")

WORKERS = 6
ROUNDS = 12


@pytest.fixture
def engine():
    # Real connections: every thread has its own.
    return new_engine(addressable=True)


def race(task, *, workers: int = WORKERS) -> list:
    """``task(index)`` in ``workers`` threads released together; what each
    returned. An exception in any of them fails the test."""
    barrier = threading.Barrier(workers)
    results: list = [None] * workers
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = task(index)
        except BaseException as error:  # noqa: BLE001 — reported by the test
            errors.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors[:3]
    return results


def _count(engine, model, *where) -> int:
    with Session(engine) as session:
        return len(session.exec(select(model).where(*where)).all())


# ---------------------------------------------------------------------------
# Identity: find or create a track
# ---------------------------------------------------------------------------


def test_the_same_song_resolved_together_is_one_record(engine, monkeypatch):
    # The window between "no record found" and "created" is widened: without a
    # lock every thread gets through it.
    from db import track_store

    real = track_store._find_by_fuzzy_match

    def slow(*args, **kwargs):
        time.sleep(0.15)
        return real(*args, **kwargs)

    monkeypatch.setattr(track_store, "_find_by_fuzzy_match", slow)

    def resolve(_):
        with Session(engine) as session:
            return find_or_create_track(make_track("One Song", "One Artist", isrc="ISRC-ONE"), session).internal_id

    ids = race(resolve)

    assert len(set(ids)) == 1
    assert _count(engine, TrackRecord) == 1
    assert _count(engine, TrackIdentifierRecord) == 1


def test_an_identifier_added_by_two_writers_is_on_the_track_once(engine):
    with Session(engine) as session:
        record = TrackRecord(title="T", artist="A", album="B")
        session.add(record)
        session.commit()
        track_id = record.internal_id
    track = make_track("T", "A", isrc="ISRC-X", mbid="mbid-x")

    def add(_):
        with Session(engine) as session:
            add_identifiers(track_id, track.external_ids, session)
            session.commit()

    race(add)

    assert _count(engine, TrackIdentifierRecord, TrackIdentifierRecord.track_id == track_id) == 2


# ---------------------------------------------------------------------------
# Upserts and "add if missing"
# ---------------------------------------------------------------------------


def test_a_setting_written_by_two_writers_is_one_row(engine):
    for round_ in range(ROUNDS):
        def write(index):
            with Session(engine) as session:
                settings_store.SettingsRepository(session).set_system(f"key-{round_}", index)

        race(write)
        assert _count(engine, Setting, Setting.key == f"key-{round_}") == 1


def test_a_users_setting_written_by_two_writers_is_one_row(engine):
    with Session(engine) as session:
        seed_parents(session, users=["u1"])

    def write(index):
        with Session(engine) as session:
            settings_store.SettingsRepository(session).set_user("theme", "u1", f"v{index}")

    race(write)

    assert _count(engine, Setting, Setting.key == "theme") == 1


def test_a_credential_saved_by_two_writers_is_one_row(engine):
    def save(index):
        with Session(engine) as session:
            CredentialStore(session).save_api_key("provider-a", f"key-{index}")

    race(save)

    assert _count(engine, ProviderCredential, ProviderCredential.provider == "provider-a") == 1


def test_a_like_intent_registered_by_two_requests_is_one_marker(engine):
    with Session(engine) as session:
        seed_parents(session, users=["u1"], tracks=["t1"])

    for _ in range(ROUNDS):
        def register(_):
            with Session(engine) as session:
                return pending_like_store.ensure_pending_like(session, "u1", "t1")

        ids = race(register)
        assert len(set(ids)) == 1
        with Session(engine) as session:
            pending_like_store.delete_pending_like(session, "u1", "t1")


def test_a_favorite_added_by_two_requests_is_added_once(engine):
    with Session(engine) as session:
        seed_parents(session, users=["u1"], tracks=["t1"])

    for _ in range(ROUNDS):
        def heart(_):
            with Session(engine) as session:
                return favorite_store.add_favorite(session, "u1", "t1", "heart")

        added = race(heart)
        assert added.count(True) == 1
        assert _count(engine, UserFavorite) == 1
        with Session(engine) as session:
            favorite_store.remove_favorite(session, "u1", "t1")


# ---------------------------------------------------------------------------
# Download requests: one outcome per decision, one place per quota
# ---------------------------------------------------------------------------

TRACKS_PER_REQUEST = 4


class _Approval:
    """Authentication on, a user and two admins, a counter of what the gate queued."""

    def __init__(self, engine, monkeypatch):
        from services.config_manager import ConfigManager

        from tests.download_requests_support import make_user

        self.engine = engine
        self.config = ConfigManager(engine=engine)
        self.config.update({"auth_enabled": True})
        self.queued: list[dict] = []
        monkeypatch.setattr(downloads, "get_config", lambda: self.config)
        monkeypatch.setattr(downloads, "queue_download", lambda **kw: self.queued.append(kw))
        self.user = make_user(engine, "asker")
        self.admins = [make_user(engine, "admin-a", "admin"), make_user(engine, "admin-b", "admin")]

    def ask(self, tag: str, user=None, *, count: int = TRACKS_PER_REQUEST):
        """The user asks for a playlist of ``count`` tracks (a kind that waits by default)."""
        from musizerr_sdk.models import Playlist
        from services import download_requests
        from db.models import User

        from tests.conftest import FakeAuthManager, FakeRegistry
        from tests.download_requests_support import tracks_of

        user = user or self.user
        spec = download_requests.playlist_spec(
            Playlist(name=tag, provider="catalog", provider_id=tag, tracks=tracks_of(tag, count)),
        )
        with Session(self.engine) as session:
            return download_requests.request_download(
                session, session.get(User, user.id), spec, favorite_for=user.id, config=self.config,
                registry=FakeRegistry(), auth_manager=FakeAuthManager(),
            )

    def decide(self, action: str, request_id: str, who) -> str:
        """``approve``/``deny`` as an admin, ``cancel`` as the requester: the outcome."""
        from services import download_requests
        from db.models import User

        from tests.conftest import FakeAuthManager, FakeRegistry

        with Session(self.engine) as session:
            user = session.get(User, who.id)
            if action == "approve":
                result = download_requests.approve(
                    session, request_id, user, note=None, config=self.config, registry=FakeRegistry(),
                    auth_manager=FakeAuthManager(),
                )
            elif action == "deny":
                result = download_requests.deny(session, request_id, user, reason=None)
            else:
                result = download_requests.cancel(session, request_id, user)
            return result.outcome


def test_two_admins_approving_one_request_queue_it_once(engine, monkeypatch):
    world = _Approval(engine, monkeypatch)

    for round_ in range(ROUNDS // 2):
        request_id = world.ask(f"double-{round_}").request.id
        before = (_count(engine, Download), len(world.queued))

        outcomes = race(lambda index: world.decide("approve", request_id, world.admins[index % 2]), workers=WORKERS)

        assert sorted(outcomes) == ["decided"] + ["unchanged"] * (WORKERS - 1)
        assert _count(engine, Download) - before[0] == TRACKS_PER_REQUEST
        assert len(world.queued) - before[1] == TRACKS_PER_REQUEST
        with Session(engine) as session:
            request = session.get(DownloadRequest, request_id)
            assert (request.status, request.queued_tracks) == ("approved", TRACKS_PER_REQUEST)
    assert _count(engine, PendingLike) == TRACKS_PER_REQUEST * (ROUNDS // 2)


def test_an_approval_and_a_cancellation_of_one_request_have_one_outcome(engine, monkeypatch):
    world = _Approval(engine, monkeypatch)
    results = {"approved": 0, "cancelled": 0}

    for round_ in range(ROUNDS):
        request_id = world.ask(f"vs-{round_}").request.id
        before_downloads, before_intents = _count(engine, Download), _count(engine, PendingLike)

        outcomes = race(
            lambda index: world.decide("approve", request_id, world.admins[0]) if index == 0
            else world.decide("cancel", request_id, world.user),
            workers=2,
        )

        assert sorted(outcomes) == ["conflict", "decided"]
        with Session(engine) as session:
            request = session.get(DownloadRequest, request_id)
            results[request.status] += 1
        added = (_count(engine, Download) - before_downloads, _count(engine, PendingLike) - before_intents)
        # Approved: queued once, favorites registered. Cancelled: nothing at all.
        assert added == ((TRACKS_PER_REQUEST, TRACKS_PER_REQUEST) if request.status == "approved" else (0, 0))
    assert sum(results.values()) == ROUNDS
    assert len(world.queued) == results["approved"] * TRACKS_PER_REQUEST


def test_an_approval_and_a_denial_of_one_request_have_one_outcome(engine, monkeypatch):
    world = _Approval(engine, monkeypatch)

    for round_ in range(ROUNDS // 2):
        request_id = world.ask(f"deny-{round_}").request.id
        before = _count(engine, Download)

        outcomes = race(
            lambda index: world.decide("approve" if index == 0 else "deny", request_id, world.admins[index]),
            workers=2,
        )

        assert sorted(outcomes) == ["conflict", "decided"]
        with Session(engine) as session:
            status = session.get(DownloadRequest, request_id).status
        assert _count(engine, Download) - before == (TRACKS_PER_REQUEST if status == "approved" else 0)


def test_two_requests_of_one_user_never_both_fit_under_the_same_quota(engine, monkeypatch):
    from core import download_policy as dp
    from tests.download_requests_support import make_user

    world = _Approval(engine, monkeypatch)
    world.config.update({dp.mode_key(dp.KIND_PLAYLIST): dp.MODE_AUTO, dp.QUOTA_TRACKS_KEY: 6})

    for round_ in range(ROUNDS // 2):
        user = make_user(engine, f"quota-{round_}")

        outcomes = race(lambda index: world.ask(f"fit-{round_}-{index}", user, count=4), workers=2)

        assert sorted(o.pending for o in outcomes) == [False, True]       # 4 + 4 > 6: exactly one went at once
        waiting = next(o for o in outcomes if o.pending)
        assert waiting.request.reason == dp.REASON_OVER_QUOTA
        assert _count(engine, DownloadRequest, DownloadRequest.user_id == user.id, DownloadRequest.status == "approved") == 1
        assert _count(engine, DownloadRequest, DownloadRequest.user_id == user.id, DownloadRequest.status == "pending") == 1


def test_one_user_asking_twice_for_the_same_thing_at_once_has_one_waiting_request(engine, monkeypatch):
    world = _Approval(engine, monkeypatch)

    for round_ in range(ROUNDS // 2):
        outcomes = race(lambda _: world.ask(f"same-{round_}"), workers=WORKERS)

        assert all(o.pending for o in outcomes)
        assert len({o.request.id for o in outcomes}) == 1
        assert _count(engine, DownloadRequest, DownloadRequest.target_id == f"same-{round_}") == 1


def test_a_removal_recorded_by_two_requests_is_recorded_once(engine):
    with Session(engine) as session:
        seed_parents(session, users=["u1"])

    def remove(_):
        with Session(engine) as session:
            favorite_store.record_removal(session, "u1", ["isrc:A", "song:x|y"])

    race(remove)

    assert _count(engine, RemovedFavorite) == 2


def test_a_follower_attached_by_two_requests_is_attached_once(engine):
    with Session(engine) as session:
        seed_parents(session, users=["u1", "u2"], tracks=["t1"])
        download = Download(track_id="t1", requested_by="u1", status=ds.PENDING)
        session.add(download)
        session.commit()
        download_id = download.id

    def attach(_):
        with Session(engine) as session:
            add_follower(session, download_id, "u2")

    race(attach)

    assert _count(engine, DownloadFollower) == 1


def test_a_file_registered_by_two_workers_is_one_library_entry(engine, monkeypatch):
    monkeypatch.setattr(tasks, "engine", engine)
    with Session(engine) as session:
        seed_parents(session, tracks=["t1", "t2"])

    def register(index):
        tasks._register_library_entry(f"t{index % 2 + 1}", "A/song.flac", "local")

    race(register)

    assert _count(engine, LibraryEntry, LibraryEntry.file_path == "A/song.flac") == 1


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def test_two_runs_of_a_routine_do_not_both_claim_one_period(engine):
    for round_ in range(ROUNDS):
        key = f"routine-{round_}"
        with Session(engine) as session:
            run_ids = [routine_run_store.queue_run(session, key).id for _ in range(WORKERS)]

        def claim(index):
            with Session(engine) as session:
                return routine_run_store.claim_run(session, run_ids[index], period_seconds=3600) is not None

        claimed = race(claim)

        assert claimed.count(True) == 1, f"round {round_}"
        assert _count(engine, RoutineRun, RoutineRun.routine_key == key, RoutineRun.status == "running") == 1


def test_a_download_requested_twice_at_once_is_one_open_download(engine, monkeypatch):
    monkeypatch.setattr(downloads, "queue_download", lambda **kwargs: None)
    # The window between "no open download" and "added" is widened.
    real = downloads.find_open_download

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        time.sleep(0.15)
        return found

    monkeypatch.setattr(downloads, "find_open_download", slow)
    with Session(engine) as session:
        seed_parents(session, users=["u1", "u2"], tracks=["t1"])
        record = session.get(TrackRecord, "t1")

    def request(index):
        with Session(engine) as session:
            record = session.get(TrackRecord, "t1")
            return downloads.retry_track(session, record, f"u{index % 2 + 1}", None, None).status

    outcomes = race(request)

    assert outcomes.count(downloads.QUEUED) == 1
    assert outcomes.count(downloads.ATTACHED) == WORKERS - 1
    assert _count(engine, Download, Download.track_id == "t1") == 1


def test_tracks_added_to_a_playlist_at_once_never_pass_the_cap_and_keep_their_positions(engine):
    config = ConfigManager(engine=engine)
    config.update({"playlist_max_tracks": 4})
    with Session(engine) as session:
        seed_parents(session, users=["u1"], tracks=[f"t{n}" for n in range(WORKERS)])
        playlist_id = playlists.create(session, "u1", "Race").id

    def add(index):
        with Session(engine) as session:
            try:
                return playlists.add_tracks(session, session.get(Playlist, playlist_id), [f"t{index}"], config)["added"]
            except playlists.PlaylistError as error:
                return error.code

    outcomes = race(add)

    assert outcomes.count(1) == 4
    assert outcomes.count(playlists.FULL) == WORKERS - 4
    with Session(engine) as session:
        rows = session.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
                            .order_by(PlaylistTrack.position)).all()
    assert [row.position for row in rows] == [0, 1, 2, 3]


def test_a_playlist_followed_twice_at_once_is_one_follow(engine, monkeypatch):
    from musizerr_sdk.models import Playlist as SourcePlaylist

    monkeypatch.setattr(downloads, "queue_download", lambda **kwargs: None)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    registry = FakeRegistry()
    with Session(engine) as session:
        seed_parents(session, users=["u1"])
    tracks = [make_track(f"Song {n}", provider="p", provider_id=f"t{n}") for n in range(3)]

    def follow(_):
        source = SourcePlaylist(name="Mix", provider="p", provider_id="p1", tracks=list(tracks))
        with Session(engine) as session:
            user = session.get(User, "u1")
            return playlists.follow(session, user, source, config=config, registry=registry, auth_manager=None).created

    created = race(follow)

    assert created.count(True) == 1
    assert _count(engine, Playlist) == 1
    assert _count(engine, PlaylistTrack) == 3


def test_a_library_job_started_twice_at_once_is_one_run(engine, monkeypatch):
    monkeypatch.setattr(library_jobs, "worker_alive", lambda queue: True)
    with Session(engine) as session:
        seed_parents(session, users=["u1"])

    def start(_):
        with Session(engine) as session:
            run, created = library_jobs.start(session, "u1", library_job_store.KIND_LIBRARY_SYNC,
                                              dispatch=lambda *args: None)
            return run.id, created

    results = race(start)

    assert len({run_id for run_id, _ in results}) == 1
    assert [created for _, created in results].count(True) == 1


def test_a_user_registered_twice_at_once_is_refused_the_second_time(engine):
    def register(index):
        with Session(engine) as session:
            try:
                user_service.create_user(session, username="same", password_hash="x", email=f"{index}@x.org")
                return True
            except Exception as error:  # noqa: BLE001 — the unique index, whatever the driver names it
                session.rollback()
                return type(error).__name__

    outcomes = race(register)

    assert outcomes.count(True) == 1
    assert set(outcomes) - {True} == {"IntegrityError"}


# ---------------------------------------------------------------------------
# Sign-in through a provider
# ---------------------------------------------------------------------------


def test_two_first_sign_ins_of_the_same_account_create_one_user(engine):
    from musizerr_sdk.auth import AccountIdentity, AuthFlow
    from core.provider_login import POLICY_ACTIVE
    from db.models import User
    from services import provider_login

    from tests.loopback import as_proxy
    from tests.provider_login_support import OAuthLoginProvider

    provider = as_proxy(OAuthLoginProvider("orbit"))

    def sign_in(index):
        with Session(engine) as session:
            signed = provider_login.sign_in(
                session, provider, AccountIdentity("ext-1", "Alice"), AuthFlow.OAUTH2, {"access_token": str(index)}, POLICY_ACTIVE,
            )
            return signed.user.id

    users = race(sign_in)

    assert len(set(users)) == 1
    assert _count(engine, User) == 1
    assert _count(engine, ProviderCredential, ProviderCredential.provider == "orbit") == 1


def test_a_login_code_is_spent_by_exactly_one_of_the_exchanges(engine):
    from core import user_auth
    from db.models import ProviderLoginCode
    from services import provider_login

    with Session(engine) as session:
        seed_parents(session, users=["alice"])
        user = user_service.get_user_by_id(session, "alice")
        code = provider_login.issue_code(session, user)

    def exchange(index):
        with Session(engine) as session:
            try:
                provider_login.exchange_code(session, code)
                return True
            except Exception as error:  # noqa: BLE001 — the refusal is what is counted
                return type(error).__name__

    outcomes = race(exchange)

    assert outcomes.count(True) == 1
    assert set(outcomes) - {True} == {"LoginRefused"}
    assert _count(engine, ProviderLoginCode, ProviderLoginCode.code_hash == user_auth.hash_refresh_token(code)) == 1


# ---------------------------------------------------------------------------
# Turning authentication on, from several places at once
# ---------------------------------------------------------------------------


def test_authentication_turned_on_by_several_writers_adopts_the_owners_data_once(engine):
    from db.models import SYSTEM_USER_ID
    from services import auth_transition

    for round_ in range(ROUNDS):
        with Session(engine) as session:
            user_service.ensure_system_user(session)
            seed_parents(session, users=["admin-a", "admin-b"], tracks=[f"t{round_}"])
            session.exec(User.__table__.update().where(User.id.in_(["admin-a", "admin-b"])).values(role="admin"))
            session.add(UserFavorite(user_id=SYSTEM_USER_ID, track_id=f"t{round_}"))
            session.commit()
            settings_store.SettingsRepository(session).set_system("auth_enabled", False)

        def turn_on(index: int):
            with Session(engine) as session:
                # Each writer asks for another administrator: only the first to get the lock decides.
                return auth_transition.turn_on(
                    session, ConfigManager(engine=engine), FakeRegistry(), administrator_id=("admin-a", "admin-b")[index % 2],
                )

        reports = race(turn_on)

        assert sum(report is not None for report in reports) == 1, f"round {round_}"
        with Session(engine) as session:
            holders = {f.user_id for f in session.exec(select(UserFavorite)).all()}
        assert len(holders) == 1 and SYSTEM_USER_ID not in holders
        with Session(engine) as session:  # the next round starts from open mode again
            session.exec(UserFavorite.__table__.delete())
            session.commit()


# ---------------------------------------------------------------------------
# The lock itself
# ---------------------------------------------------------------------------


def test_the_lock_makes_a_look_and_a_write_one_writer_at_a_time(engine):
    from db.locks import xact_lock

    order: list[str] = []
    first_has_it = threading.Event()

    def first():
        with Session(engine) as session:
            xact_lock(session, "probe")
            first_has_it.set()
            time.sleep(0.4)
            order.append("first writes")
            session.commit()

    def second():
        first_has_it.wait(timeout=10)
        with Session(engine) as session:
            xact_lock(session, "probe")
            order.append("second writes")
            session.commit()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert order == ["first writes", "second writes"]


def test_locks_of_different_names_do_not_wait_for_each_other(engine):
    from db.locks import xact_lock

    with Session(engine) as one, Session(engine) as other:
        xact_lock(one, "name-a")
        started = time.monotonic()
        xact_lock(other, "name-b")
        assert time.monotonic() - started < 1.0
        one.commit()
        other.commit()

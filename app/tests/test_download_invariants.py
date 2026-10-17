"""
Invariants of the download lifecycle, under any interleaving of what can
happen to a track: a request from any provider row, a retry, "retry every
failed", the transient-failure routine, a worker run (landed, delegated,
definitive or transient failure), a webhook or reconcile tick of a
delegation, the track landing in the library by another way (scan, sync
provider), failures growing old.

- A track in the library never gets a new Download from any entry point,
  unless an admin forces it.
- A track in the library is always in phase done: never reported active,
  queued or failed (file state, phase filters, counters, live events).
- ``completed_at`` is set iff the state is terminal.
- No two library entries for the same track (tracks an admin forced aside).

Real case behind each one: see ``test_download_owned_tracks.py`` (owned
tracks downloaded again), ``test_download_status_precedence.py`` (a delegated
attempt shown inside "Completed", ``completed_at`` on a dispatch).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest import mock

from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from sqlmodel import Session, func, select

from core import download_states as ds
from musizerr_sdk.delegation import DelegationStatus
from core.download_resolver import ResolveResult
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import ISRC, PROVIDER_ID, Identifier
from musizerr_sdk.models import Track
from core.provider_cooldown import NoCooldown
from core.provider_slots import UnlimitedSlots
from db.download_store import track_status_ranks
from db.library_store import file_states, in_library
from db.models import Download, LibraryEntry, TrackIdentifierRecord, TrackRecord, User
from db.track_store import find_or_create_track
from db.upsert import insert_ignore
from routes.deps import Downloader
from routes.history import retry_failed_downloads
from services import downloads, progress
from services.config_manager import ConfigManager
from workers import maintenance, tasks

from tests.database import new_engine, release_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider

SYNC = "fakesync"
TRACKS = ("Taxi sulla Luna", "EVERYDAY", "Mil pasos")
PROVIDERS = ("primary", "secondary")
OUTCOMES = ("landed", "delegated", "definitive", "transient")


def _row(index: int, provider: str) -> Track:
    title = TRACKS[index]
    ids = [
        Identifier(type=PROVIDER_ID, value=f"{provider}-{index}", provider=provider),
        Identifier(type=ISRC, value=f"ISRC{index}"),
    ]
    return Track(title=title, artist="Artist", album="Album", duration_ms=200_000,
                 provider=provider, provider_id=f"{provider}-{index}", external_ids=ids)


class DownloadLifecycle(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.engine = new_engine()
        self.config = ConfigManager(engine=self.engine)
        self.config.update({"max_retries": 0, "transient_retry_after_hours": 1,
                            "transient_retry_max_rounds": 3, "transient_retry_batch_size": 10})
        self.registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
        self.queue: list[dict] = []
        self.events: list[dict] = []
        self.forced: set[str] = set()
        self.next_outcome = "landed"
        with Session(self.engine) as session:
            admin = User(username="admin", password_hash="x", role="admin")
            session.add(admin)
            session.commit()
            session.refresh(admin)
            self.admin = admin

        def resolve(resolver, track, workspace, on_trying=None):
            return {
                "landed": ResolveResult(success=True, provider="dl", file_name="song.ogg"),
                "delegated": ResolveResult(success=False, delegated=True, provider="ext", delegated_handle="h"),
                "definitive": ResolveResult(success=False, error="No provider could download it"),
                "transient": ResolveResult(success=False, error="upstream 503", retriable=True),
            }[self.next_outcome]

        def store(result, workspace, track, *args, **kwargs):
            return f"{track.internal_id}/{args[4]}.ogg"

        def sent(payload: str) -> None:
            """The real event, as ``services.progress`` sends it, checked
            against the library at the instant it leaves."""
            event = json.loads(payload)
            with Session(self.engine) as session:
                owned = event["track_id"] in in_library(session, [event["track_id"]], [sync_id_type(SYNC)])
            self.events.append({**event, "owned": owned})

        self.patches = [
            mock.patch.object(downloads, "get_config", lambda: self.config),
            mock.patch.object(downloads, "queue_download", lambda **kw: self.queue.append(kw)),
            mock.patch.object(tasks, "engine", self.engine),
            mock.patch.object(tasks, "get_config", lambda: self.config),
            mock.patch.object(tasks, "get_registry", lambda: self.registry),
            mock.patch.object(progress, "engine", self.engine),
            mock.patch.object(progress, "_publish", sent),
            mock.patch.object(tasks, "RedisProviderSlots", lambda **kw: UnlimitedSlots()),
            mock.patch.object(tasks, "RedisProviderCooldown", lambda **kw: NoCooldown()),
            mock.patch.object(tasks, "auth_manager_for", lambda store: FakeAuthManager()),
            mock.patch.object(tasks, "resolve_storage_provider", lambda registry, name: (object(), "disk")),
            mock.patch.object(tasks, "_store_download", store),
            mock.patch.object(tasks.DownloadResolver, "resolve", resolve),
            mock.patch.object(maintenance, "engine", self.engine),
            mock.patch.object(maintenance, "start_reconciliation", lambda *a, **k: True),
        ]
        for patch in self.patches:
            patch.start()

    def teardown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        release_engine(self.engine)  # one database per example: its connections go with it

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        return Session(self.engine)

    def _track_ids(self) -> list[str]:
        with self._session() as session:
            return list(session.exec(select(TrackRecord.internal_id)).all())

    def _owned(self) -> set[str]:
        with self._session() as session:
            return in_library(session, self._track_ids(), [sync_id_type(SYNC)])

    def _counts(self) -> dict[str, int]:
        with self._session() as session:
            return dict(session.exec(select(Download.track_id, func.count()).group_by(Download.track_id)).all())

    def _gate(self, action, forced_track: str | None = None) -> None:
        """Runs an entry point; a track in the library before it gets no new
        Download from it (except the one an admin forced)."""
        owned, before = self._owned(), self._counts()
        action()
        after = self._counts()
        for track_id in owned - {forced_track}:
            assert after.get(track_id, 0) == before.get(track_id, 0), f"new download for owned track {track_id}"

    def _track_id(self, index: int) -> str:
        with self._session() as session:
            return find_or_create_track(_row(index, "primary"), session).internal_id

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    @rule(index=st.integers(0, len(TRACKS) - 1), provider=st.sampled_from(PROVIDERS), force=st.booleans())
    def request(self, index, provider, force):
        forced = self._track_id(index) if force else None

        def act():
            with self._session() as session:
                result = downloads.enqueue_track(_row(index, provider), session, registry=self.registry, force=force)
                if result.status == downloads.QUEUED and force:
                    self.forced.add(result.library_id)

        self._gate(act, forced)

    @rule(index=st.integers(0, len(TRACKS) - 1), force=st.booleans())
    def retry(self, index, force):
        track_id = self._track_id(index)
        forced = track_id if force else None

        def act():
            with self._session() as session:
                result = downloads.retry_track(session, session.get(TrackRecord, track_id), None, None,
                                               self.registry, force=force)
                if result.status == downloads.QUEUED and force:
                    self.forced.add(result.library_id)

        self._gate(act, forced)

    @rule()
    def retry_every_failed(self):
        def act():
            with self._session() as session:
                retry_failed_downloads(session, Downloader(self.admin), self.registry, add_to_favorites=None)

        self._gate(act)

    @rule()
    def transient_routine(self):
        def act():
            with self._session() as session:
                downloads.retry_transient_failures(session, self.registry, self.config)

        self._gate(act)

    # ------------------------------------------------------------------
    # Worker, delegation, the library changing by other ways
    # ------------------------------------------------------------------

    @precondition(lambda self: self.queue)
    @rule(outcome=st.sampled_from(OUTCOMES), pick=st.integers(0, 10))
    def run_worker(self, outcome, pick):
        call = self.queue.pop(pick % len(self.queue))
        self.next_outcome = outcome
        seen = len(self.events)
        tasks.download_task.run(**call)
        self._events_match_the_file_state(seen)

    @rule(state=st.sampled_from([ds.DOWNLOADING, ds.IMPORTING, ds.DOWNLOADED, ds.FAILED, "max_age"]),
          pick=st.integers(0, 10))
    def delegation_tick(self, state, pick):
        with self._session() as session:
            dispatched = session.exec(select(Download.id).where(Download.status.not_in(tuple(ds.TERMINAL)),
                                                               Download.delegation_handle.is_not(None))).all()
        if not dispatched:
            return
        download_id = dispatched[pick % len(dispatched)]
        seen = len(self.events)
        if state == "max_age":
            maintenance._finalize(download_id, ds.FAILED, "delegation timed out (max-age exceeded)")
        else:
            maintenance._apply_delegation_status(download_id, None, None, DelegationStatus(state=state), self.config,
                                                 self.registry)
        self._events_match_the_file_state(seen)

    @rule(index=st.integers(0, len(TRACKS) - 1), how=st.sampled_from(["scan", "sync"]))
    def lands_by_another_way(self, index, how):
        track_id = self._track_id(index)
        with self._session() as session:
            if how == "scan":
                if session.exec(select(LibraryEntry).where(LibraryEntry.track_id == track_id)).first() is None:
                    session.add(LibraryEntry(track_id=track_id, file_path=f"scan/{track_id}.ogg", file_format="ogg",
                                             source="scan", storage_provider="disk"))
            else:
                insert_ignore(session, TrackIdentifierRecord(
                    track_id=track_id, id_type=sync_id_type(SYNC), value=f"nd-{index}"))
            session.commit()

    @rule()
    def failures_grow_old(self):
        with self._session() as session:
            for row in session.exec(select(Download).where(Download.status == ds.FAILED)).all():
                row.completed_at = row.completed_at - timedelta(hours=7)
                session.add(row)
            session.commit()

    def _events_match_the_file_state(self, since: int) -> None:
        for event in self.events[since:]:
            if event["owned"]:
                assert event["file_state"] == ds.DOWNLOADED and event["phase"] == "done", event
            else:
                assert event["file_state"] != ds.DOWNLOADED or event["status"] == ds.DOWNLOADED, event

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    @invariant()
    def completed_at_iff_terminal(self):
        with self._session() as session:
            for row in session.exec(select(Download)).all():
                assert (row.completed_at is not None) == (row.status in ds.TERMINAL), (row.status, row.completed_at)

    @invariant()
    def a_track_in_the_library_is_done_everywhere(self):
        owned = self._owned()
        types = [sync_id_type(SYNC)]
        with self._session() as session:
            states = file_states(session, self._track_ids(), types)
            ranks = track_status_ranks(None, types)
            phases = {track_id: ds.LABELS[ds.ORDER[rank]]["phase"]
                      for track_id, rank in session.exec(select(ranks.c.track_id, ranks.c.rank)).all()}
        for track_id in owned:
            assert states[track_id] == ds.DOWNLOADED
            assert phases.get(track_id, "done") == "done", (track_id, phases.get(track_id))

    @invariant()
    def one_library_entry_per_track(self):
        with self._session() as session:
            counts = session.exec(
                select(LibraryEntry.track_id, func.count()).group_by(LibraryEntry.track_id)
            ).all()
        for track_id, count in counts:
            if track_id not in self.forced:
                assert count <= 1, f"{count} library entries for {track_id}"


# Not derandomized: CI sets the derandomized profile by default, and with hypothesis 6.169 a derandomized
# state machine fails with FlakyStrategyDefinition ("Aborted test because unable to satisfy sampled_from([Rule...")
# on a source tree and passes on the next, whatever the code does.
DownloadLifecycle.TestCase.settings = settings(
    max_examples=200, stateful_step_count=30, deadline=None, derandomize=False,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
test_download_lifecycle = DownloadLifecycle.TestCase

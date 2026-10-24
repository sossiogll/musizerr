from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Column, Index, String, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from . import credential_cipher
from .types import UTCDateTime


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The predicate of the partial unique indexes that make a row unique among
# those with no user (``user_id IS NULL``): a plain UNIQUE constraint treats
# two NULLs as different, on SQLite and on PostgreSQL alike.
NO_USER = text("user_id IS NULL")

# The accounts that carry the id their provider gave them (``ProviderCredential``).
HAS_EXTERNAL_ID = text("external_id IS NOT NULL")

# The runs a user can have only one of per kind (``LibraryJobRun``).
_ACTIVE_JOB = text("status IN ('queued', 'running')")

# The requests a user can have only one of per target (``DownloadRequest``).
_PENDING_REQUEST = text("status = 'pending'")


# Id of the synthetic, no-login "system" user that routes/deps.py::
# get_current_user hands out for every request when `auth_enabled` is False.
# It is not an account, but it has a row in this table (``db.user_service.
# ensure_system_user``, at start): the favorites and likes written with no
# sign-in point at it, and a foreign key needs its target. Defined here (not
# in routes/deps.py) so any layer that needs to recognize "this is not a real
# account" (e.g. services/settings.py, deciding whether a scope='user' setting
# has an owner to write to) imports the SAME sentinel instead of duplicating
# the literal string.
SYSTEM_USER_ID = "__system__"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(default_factory=_uuid, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: Optional[str] = Field(default=None, unique=True, index=True)
    password_hash: str
    role: str = Field(default="user")  # "admin" | "user"
    is_active: bool = Field(default=True)
    # An account a sign-in through a provider created while the admin's policy
    # asks to approve them (``core/provider_login.py``): inactive until an admin
    # activates it, which clears this.
    pending_approval: bool = Field(default=False)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    # metadata_provider_priority used to live here as a column; migration
    # 0011 moved it to the `settings` table (scope='user').


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    # Stored as a SHA-256 hex digest of the raw token — never the raw value.
    token_hash: str = Field(unique=True, index=True)
    issued_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    expires_at: datetime = Field(sa_type=UTCDateTime)
    revoked_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    # The sign-in this token descends from (the id of its first token; NULL on
    # tokens issued before rotation tracking: the token is its own family).
    # Re-presenting a rotated token outside the grace window revokes it all.
    family_id: Optional[str] = Field(default=None, index=True)
    # Set when the token was rotated (revoked_at is then the rotation time);
    # NULL when it was revoked by a sign-out or a family revocation.
    replaced_by_id: Optional[str] = Field(default=None)


class ProviderLoginCode(SQLModel, table=True):
    """The one-time code a sign-in through a provider hands the browser to be
    exchanged for a session (``services/provider_login.py``): the authorization
    ends on a redirect, and a session must never travel in a URL. Only the
    SHA-256 of the code is stored; it lives ``LOGIN_CODE_EXPIRE_SECONDS`` and
    is claimed by one conditional UPDATE (``used_at IS NULL``)."""

    __tablename__ = "provider_login_codes"

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    code_hash: str = Field(unique=True, index=True)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    expires_at: datetime = Field(sa_type=UTCDateTime)
    used_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)


class ProviderCredential(SQLModel, table=True):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        # One credential per (user, provider, type); the global ones (no user)
        # are unique by the partial index.
        UniqueConstraint("user_id", "provider", "credential_type", name="uq_provider_credential_user"),
        Index(
            "uq_provider_credential_global", "provider", "credential_type", unique=True,
            sqlite_where=NO_USER, postgresql_where=NO_USER,
        ),
        # A provider's account is linked to ONE user: the id the provider gave it
        # (``musizeer_sdk.auth.AccountIdentity``) is unique per provider.
        Index(
            "uq_provider_credential_external", "provider", "external_id", unique=True,
            sqlite_where=HAS_EXTERNAL_ID, postgresql_where=HAS_EXTERNAL_ID,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    # nullable → credenziale globale (non legata a un utente specifico)
    user_id: Optional[str] = Field(default=None, foreign_key="users.id", index=True)
    provider: str = Field(index=True)
    # "oauth2_token" | "oauth2_client" | "api_key"
    credential_type: str
    # The credential as JSON, SEALED (``db/credential_cipher.py``: ``enc:v1:`` + the token of the
    # configured key). Read and written only by ``db/auth_store.CredentialStore``.
    data: str
    # The stable id of the account on its provider, for a personal account of a
    # provider that can sign users in (the sign-in finds the user by it).
    external_id: Optional[str] = Field(default=None)
    updated_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class TrackRecord(SQLModel, table=True):
    __tablename__ = "tracks"

    internal_id: str = Field(default_factory=_uuid, primary_key=True)
    title: str
    artist: str
    album: str
    duration_ms: int = Field(default=0)
    track_data: Optional[str] = Field(default=None)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class TrackIdentifierRecord(SQLModel, table=True):
    __tablename__ = "track_identifiers"
    __table_args__ = (
        Index("ix_track_identifier_type_value", "id_type", "value"),
        # An identifier is on a track once (``db.track_store.add_identifiers``).
        # The same identifier on two tracks is allowed: that is the ambiguity
        # ``core.identifiers.resolve_identity`` reports.
        UniqueConstraint("track_id", "id_type", "value", name="uq_track_identifier"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    track_id: str = Field(foreign_key="tracks.internal_id", index=True)
    id_type: str   # "isrc" | "mbid" | "<provider>_id"
    value: str


class Download(SQLModel, table=True):
    __tablename__ = "downloads"

    id: str = Field(default_factory=_uuid, primary_key=True)
    track_id: str = Field(foreign_key="tracks.internal_id", index=True)
    requested_by: Optional[str] = Field(default=None, foreign_key="users.id", index=True)
    # Vocabolario canonico in core.download_states:
    # pending | downloading | importing | conflict | retrying | dispatched | downloaded | failed
    status: str = Field(default="pending", index=True)
    provider_used: Optional[str] = Field(default=None)
    retry_count: int = Field(default=0)
    file_path: Optional[str] = Field(default=None)   # relativo a base_dir
    error_message: Optional[str] = Field(default=None)
    started_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    completed_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    # Handle opaco per la riconciliazione dei download delegati (es. id album
    # del sistema esterno). Interpretato solo dal provider che l'ha prodotto.
    delegation_handle: Optional[str] = Field(default=None)
    # Quanto il sistema esterno è stato davvero osservato su questa delega:
    # i poll a cui ha risposto (``delegation_answers``), l'ultimo
    # (``delegation_answered_at``) e il tempo coperto da quelle risposte
    # (``delegation_observed_seconds``: ogni intervallo fra due risposte
    # conta al più due poll, così un'attesa senza poll — coda piena, sistema
    # irraggiungibile — non consuma il max-age). Vedi
    # ``workers.maintenance.reconcile_delegation``.
    delegation_answers: int = Field(default=0)
    delegation_answered_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    delegation_observed_seconds: float = Field(default=0.0)
    # Su un download fallito: l'ultimo fallimento era transitorio (rete,
    # errori upstream: lo ritenta più tardi il routine
    # ``retry_transient_failures``) o definitivo. None = non noto (righe
    # fallite prima della migrazione 0023).
    retriable: Optional[bool] = Field(default=None)


class LibraryEntry(SQLModel, table=True):
    __tablename__ = "library_entries"

    id: str = Field(default_factory=_uuid, primary_key=True)
    # nullable → file scansionato non ancora abbinato a una Track
    track_id: Optional[str] = Field(default=None, foreign_key="tracks.internal_id", index=True)
    file_path: str = Field(unique=True)   # relativo a base_dir
    file_format: str                       # "mp3" | "flac" | "ogg"
    source: str = Field(default="download")  # "download" | "scan"
    # Nessun default hardcoded qui (Golden Rule, CLAUDE.md: nessun nome di
    # provider in db/) — ogni chiamante passa il nome del provider di storage
    # risolto (workers/tasks.py, services/library_scan.py). Il
    # server_default="local" della migrazione 0002 resta nello schema per le
    # righe già esistenti: non è codice applicativo che decide qualcosa.
    storage_provider: str
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    # Set by the library file reconciliation (``services/library_reconcile.py``)
    # when the storage no longer holds the file and it could not be found
    # elsewhere: the entry, and the downloads that landed on its path, stop
    # putting the track in the library (``db/library_store.py``). Cleared
    # when the file is back (a reconciliation or a download on that path).
    missing_since: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)


class DownloadFollower(SQLModel, table=True):
    """
    Utenti diversi dal richiedente originale che si sono agganciati a una
    richiesta di download già in corso (vedi
    ``services/downloads.py::attach_to_open_download``). Serve solo a
    far comparire quella richiesta nella cronologia (``GET /downloads``,
    ``routes/history.py``) anche per loro: ``Download.requested_by`` resta
    il richiedente originale, non viene mai riscritto.
    """
    __tablename__ = "download_followers"
    __table_args__ = (
        UniqueConstraint("download_id", "user_id", name="uq_download_follower"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    download_id: str = Field(foreign_key="downloads.id", index=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class UserSavedTrack(SQLModel, table=True):
    """A track the user liked on another provider (imported by the library
    sync), linked to its canonical track in the `tracks` table. Identity/dedup
    (ISRC/MBID/fuzzy) and metadata merging live on TrackRecord itself
    (see db/track_store.py) — this row only tracks the user<->track relation.

    Not a favorite by itself: the like matching (``services/favorites.py``)
    links it to a track in the library (``library_track_id``) and makes that
    track a favorite once. A like with no match yet is "liked elsewhere, not
    in library"."""
    __tablename__ = "user_saved_tracks"
    __table_args__ = (
        Index("ix_ust_user_track", "user_id", "track_id", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    track_id: str = Field(foreign_key="tracks.internal_id", index=True)
    saved_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)  # MIN(added_at) across providers
    # The library track this like was matched to, and when (set once: a
    # favorite removed later is not re-added by the next match).
    library_track_id: Optional[str] = Field(default=None, foreign_key="tracks.internal_id", index=True)
    matched_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)


class UserFavorite(SQLModel, table=True):
    """A user's favorite: always a track in the library (the routes and the
    like matching check it before writing). ``source`` says how it got here:
    ``heart`` (liked in Musizeer), ``download`` (a download with favorite
    landed), ``match`` (a like from another provider matched the library)."""
    __tablename__ = "user_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "track_id", name="uq_user_favorite"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    track_id: str = Field(foreign_key="tracks.internal_id", index=True)
    source: str = Field(default="heart")
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class RemovedFavorite(SQLModel, table=True):
    """A favorite the user took off, remembered by what names the song
    (``core.matching.song_keys``: its stable identifiers and its title
    identity with each artist), not by a record id: the same song comes back
    on another record after an identity merge or split, a re-import or a
    wipe of the imported likes, and must stay removed. One row per key; the
    keys of one removal share ``group_id``, so a new heart or download of the
    song clears them all. Automatic paths (the like matching, the likes
    download) honour it; only the user's own heart or download clears it."""
    __tablename__ = "removed_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_removed_favorite_key"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    key: str = Field(index=True)
    group_id: str = Field(index=True)
    removed_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class UserSavedTrackProvider(SQLModel, table=True):
    __tablename__ = "user_saved_track_providers"
    __table_args__ = (
        Index("ix_ustp_track_provider", "saved_track_id", "provider", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    saved_track_id: str = Field(foreign_key="user_saved_tracks.id", index=True)
    provider: str
    provider_track_id: str = Field(default="")
    added_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    synced_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    stream_url: Optional[str] = Field(default=None)


class LibraryJobRun(SQLModel, table=True):
    """One run of a user's library job (``db/library_job_store.py``): the
    import of their likes from the other providers, or the download of the
    likes not in the library. Created ``queued`` when it is requested, BEFORE
    the task is enqueued; the worker moves it to ``running`` and then to
    ``completed``/``failed``. At most one queued or running run per user and
    kind (partial unique index)."""
    __tablename__ = "library_job_runs"
    __table_args__ = (
        Index("ix_library_job_runs_user_kind_created", "user_id", "kind", "created_at"),
        Index(
            "uq_library_job_active", "user_id", "kind", unique=True,
            sqlite_where=_ACTIVE_JOB, postgresql_where=_ACTIVE_JOB,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    kind: str  # "library_sync" | "download_sync"
    status: str = Field(default="queued")  # "queued" | "running" | "completed" | "failed"
    task_id: Optional[str] = Field(default=None)
    params: Optional[str] = Field(default=None)  # JSON object: the job's arguments
    # Progress, generic: a phase key, the provider it is working on, a count.
    phase: Optional[str] = Field(default=None)
    provider: Optional[str] = Field(default=None)
    processed: int = Field(default=0)
    total: Optional[int] = Field(default=None)  # None = not known yet
    providers: Optional[str] = Field(default=None)  # JSON list: one entry per provider involved
    result: Optional[str] = Field(default=None)  # JSON object of counts
    error_code: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)  # readable
    error_detail: Optional[str] = Field(default=None)  # raw (redacted), admins only
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    started_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    heartbeat_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    completed_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)


class DownloadRequest(SQLModel, table=True):
    """A user's request to download something, and what the approval policy
    (``core/download_policy.py``, ``services/download_requests.py``) decided
    about it. Every request of a user the policy governs is recorded, the
    ones approved at once too: the admin reads the history here and the quota
    is computed from it.

    ``kind`` is ``track``/``album``/``playlist``/``artist``/``liked_elsewhere``.
    The target (``target_provider``, ``target_id``) and the display fields say
    what was asked for; ``payload`` holds what approval needs to run it later
    (the serialized tracks, and the album for a delegation), so a request that
    is never approved leaves nothing in the track catalog. ``needed_tracks`` is
    the tracks that needed a download when it was decided; ``queued_tracks``
    what the approved request actually queued (``NULL`` until it ran: the
    quota counts ``needed_tracks`` meanwhile). A request moves out of
    ``pending`` exactly once, by a conditional UPDATE: ``approved``,
    ``denied`` or ``cancelled``. At most one pending request per user and
    target (partial unique index)."""
    __tablename__ = "download_requests"
    __table_args__ = (
        Index("ix_download_requests_user_status_decided", "user_id", "status", "decided_at"),
        Index("ix_download_requests_status_created", "status", "created_at"),
        Index(
            "uq_download_request_pending", "user_id", "kind", "target_provider", "target_id", unique=True,
            sqlite_where=_PENDING_REQUEST, postgresql_where=_PENDING_REQUEST,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    # The requester.
    user_id: str = Field(foreign_key="users.id", index=True)
    kind: str
    target_provider: str = Field(default="")
    target_id: str = Field(default="")
    title: str = Field(default="")
    subtitle: str = Field(default="")
    cover_url: Optional[str] = Field(default=None)  # stored form: core.urls.to_host_ref
    payload: str = Field(default="{}")  # JSON: the tracks (and album) to run on approval
    needed_tracks: int = Field(default=0)
    queued_tracks: Optional[int] = Field(default=None)
    dispatched_to: Optional[str] = Field(default=None)  # an album handed to an external system
    add_to_favorites: bool = Field(default=True)
    only_missing: bool = Field(default=False)
    status: str = Field(default="pending")  # "pending" | "approved" | "denied" | "cancelled"
    auto: bool = Field(default=False)  # approved by the policy, not by an admin
    reason: str = Field(default="")  # core.download_policy.REASONS
    decided_by: Optional[str] = Field(default=None, foreign_key="users.id")
    decided_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    decision_note: Optional[str] = Field(default=None)  # the admin's note, or the denial reason
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class UserRequestPolicy(SQLModel, table=True):
    """What an admin changed, for one user, in the download approval policy
    (``core.download_policy.PolicyOverride``): ``rules`` is a JSON object
    ``{kind: {"mode", "threshold"}}`` and ``quota_tracks`` the user's own quota
    (``NULL`` inherits, ``0`` is no quota). One row per user; the general
    policy lives in the settings."""
    __tablename__ = "user_request_policies"

    user_id: str = Field(foreign_key="users.id", primary_key=True)
    rules: str = Field(default="{}")
    quota_tracks: Optional[int] = Field(default=None)
    updated_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class ProviderSource(SQLModel, table=True):
    """A source of providers: the URL of a catalog (``core/catalog.py``, version 2).

    ``credential`` is the token of a private catalog and of its images (None: none). It is
    stored SEALED, like the provider credentials (``db/credential_cipher.py``): the column
    ``credential`` is the attribute ``sealed_credential``, the property ``credential`` opens it
    (None when there is none, or when the configured keys cannot open it: then, as far as
    anything reading it is concerned, the source has no token, and a warning says so once) and
    ``set_credential`` seals. It is never returned by the API (the routes say only
    ``has_credential``). It is sent to the catalog's host and, through the gateway, to the
    registry of the images the admin installs from this source; never anywhere else.
    ``allow_insecure_registry`` lets the gateway speak plain HTTP to the image registry of
    THIS source's providers (a local registry of images under test); a source with a
    credential cannot have it."""
    __tablename__ = "provider_sources"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str                                          # display name from the catalog
    url: str = Field(unique=True)                      # URL of the catalog
    enabled: bool = Field(default=True)
    added_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    sealed_credential: Optional[str] = Field(default=None, sa_column=Column("credential", String, nullable=True))
    allow_insecure_registry: bool = Field(default=False)

    def __init__(self, **data) -> None:
        # SQLModel drops a keyword that is not a column without a word: a token given here would
        # silently not be stored. It is given to ``set_credential``, which seals it.
        if "credential" in data:
            raise TypeError("ProviderSource(credential=...) would store nothing: use set_credential(token)")
        super().__init__(**data)

    @property
    def credential(self) -> Optional[str]:
        if self.sealed_credential is None:
            return None
        return credential_cipher.unseal(self.sealed_credential, f"The token of source '{self.name}' ({self.id})")

    def set_credential(self, token: Optional[str]) -> None:
        self.sealed_credential = None if token is None else credential_cipher.seal(token)


class InstalledProvider(SQLModel, table=True):
    """A provider the admin installed: an image pinned by digest and what was APPROVED for it.

    This table is the only source of truth for what is installed: the host pushes it to the
    gateway (``services/gateway_sync.py``) and the gateway refuses an image whose labels
    differ from ``roles`` / ``permissions``. ``permissions`` is the JSON form of the SDK's
    ``Permissions`` (``core.catalog.permissions_to_dict``), ``roles`` a JSON list. A newer
    digest of the catalog is NOT stored: whether an update exists is decided when the
    catalog is read (it cannot go stale, and nothing has to refresh it). ``approved_by`` is
    the id of the admin (NULL: approved from the host's shell) and has no foreign key on
    purpose: it is an audit trail that outlives the account."""
    __tablename__ = "installed_providers"

    id: str = Field(default_factory=_uuid, primary_key=True)
    source_id: str = Field(foreign_key="provider_sources.id", index=True)
    name: str = Field(unique=True)                     # provider name (e.g. "words")
    image: str                                         # registry/repository, no tag
    digest: str                                        # sha256:<64 hex>
    provider_api_version: int
    label: str
    roles: str                                         # JSON list: ["lyrics"]
    permissions: str                                   # JSON: the permissions the admin approved
    approved_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    approved_by: Optional[str] = Field(default=None)
    installed_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class Setting(SQLModel, table=True):
    """
    Unico store per OGNI impostazione: d'app, di provider e override
    per-utente. ``key`` è già namespaced dove serve
    (``provider.<nome>.<short_key>``, vedi ``core/settings_resolver.py``);
    ``db/settings_store.py`` (``SettingsRepository``) risolve il valore
    effettivo con una cascata riga-utente -> riga-di-sistema -> default
    dichiarato nel catalogo (``core/settings_catalog.py``).

    Le CREDENZIALI (token OAuth2, client id/secret, api key, coppie
    utente/password, account di utility esterne) restano in
    ``provider_credentials`` e NON migrano qui: questa tabella è
    deliberatamente riservata a valori non-segreti — mescolarli è
    esattamente il problema che questa fase risolve.

    ATTENZIONE (riuso di tabella): ``settings`` esisteva già PRIMA di questa
    fase per uno scopo estraneo — il flag one-shot ``library_scanned``
    scritto da ``scripts/library_scan.py`` con lo schema originale
    (``key`` come PRIMARY KEY, ``value``). La migrazione ``0010`` trasforma
    quella tabella sul posto SENZA perdere quella riga (diventa
    ``key='library_scanned', scope='system', user_id=NULL``);
    ``scripts/library_scan.py`` è stato aggiornato di conseguenza per il
    nuovo schema.

    Unicità: il vincolo su (key, scope, user_id) vale per le righe
    scope='user' (due utenti diversi = righe distinte ammesse, valori
    duplicati per lo STESSO utente no). Per le righe scope='system' due
    ``user_id`` NULL sono "diversi" per un UNIQUE (SQLite e PostgreSQL), quindi
    le righe di sistema sono uniche per l'indice parziale
    ``uq_settings_system_key``: l'upsert di ``SettingsRepository`` è un
    ``INSERT ... ON CONFLICT`` su di esso, sicuro anche con due scrittori.
    """

    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("key", "scope", "user_id", name="uq_settings_key_scope_user"),
        Index("ix_settings_key_scope", "key", "scope"),
        Index(
            "uq_settings_system_key", "key", "scope", unique=True,
            sqlite_where=NO_USER, postgresql_where=NO_USER,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    key: str = Field(index=True)
    scope: str = Field(default="system")  # "system" | "user"
    user_id: Optional[str] = Field(default=None, foreign_key="users.id", index=True)
    value: str  # JSON
    updated_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class PendingLike(SQLModel, table=True):
    """
    Stato desiderato durevole di un "mi piace" non ancora applicato, in due
    fasi:

    - ``sync_provider`` NULL: l'utente ha chiesto il download CON preferito;
      quando la traccia arriva in libreria diventa un ``UserFavorite``
      (``services/favorites.apply_favorite_intents``). Se il download
      fallisce l'intento resta, per il nuovo tentativo.
    - ``sync_provider`` valorizzato: il preferito c'è, resta da propagare il
      like al sync provider; il reconciler (``workers.maintenance.
      reconcile_like``) converge finché la traccia non è indicizzata.

    La sua esistenza è l'intento, la sua cancellazione il completamento.
    Un solo intento per (utente, traccia). Provider-agnostico.

    ``next_attempt_at`` rende il reconciler single-flight: finché è nel
    futuro un tentativo è in corso o già schedulato, e ogni altro innesco
    per lo stesso marcatore si spegne subito (``pending_like_store.
    claim_pending_like``). NULL = nessuna chain viva: lo sweep lo riprende.
    """

    __tablename__ = "pending_likes"
    __table_args__ = (
        UniqueConstraint("user_id", "track_id", name="uq_pending_like"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    track_id: str = Field(foreign_key="tracks.internal_id", index=True)
    sync_provider: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    attempts: int = Field(default=0)
    next_attempt_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None, index=True)


class RoutineRun(SQLModel, table=True):
    """
    Cronologia di esecuzione di un "system routine" (vedi
    ``core/routines.py``) — una riga per ogni dispaccio, creata ``queued``
    da ``workers.scheduler.dispatch_routine`` PRIMA di accodare il task e
    portata a ``running`` dal worker quando la esegue davvero
    (``db/routine_run_store.claim_run``). ``routine_key`` è la chiave stabile
    del routine (``RoutineDef.key``), stringa opaca per questa tabella
    (nessun routine concreto cablato qui, stesso principio di ``Setting.key``).

    Tre istanti: ``queued_at`` (il dispaccio), ``started_at`` (l'avvio reale
    nel worker, None finché la run aspetta in coda o se non è mai partita) e
    ``finished_at``. L'attesa in coda è ``started_at - queued_at``, la durata
    ``finished_at - started_at``.

    Fa anche da lock "non è già in coda né in esecuzione" (vedi
    ``db/routine_run_store.active_run``): sopravvive ai riavvii del worker
    perché è nel DB, non in un lock Redis/in-memory legato al processo.
    Potata alle ultime N righe per routine (``prune_runs``): è una
    cronologia diagnostica, non un log da conservare per sempre.
    """

    __tablename__ = "routine_runs"
    __table_args__ = (
        Index("ix_routine_runs_key_queued", "routine_key", "queued_at"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    routine_key: str = Field(index=True)
    queued_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    started_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    finished_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    status: str = Field(default="queued")  # queued | running | ok | error | skipped
    detail: Optional[str] = Field(default=None)
    # While it runs, a routine that reports progress (``RoutineDef.reports_progress``):
    # JSON {phase, provider, processed, total}, and the time of the last report.
    progress: Optional[str] = Field(default=None)
    heartbeat_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)


class DuplicateGroup(SQLModel, table=True):
    """
    Gruppo di duplicati di libreria CONFERMATI per identità reale (stesso
    recording id (MBID) via fingerprint audio — mai per semplice
    somiglianza di titolo/artista, vedi ``core.duplicates``). Scritto solo
    dal routine ``library_duplicates_scan`` (``workers/duplicates.py``);
    risolto/dismesso solo dall'admin (``routes/library_duplicates.py``).

    ``recording_id`` è la chiave di riconciliazione fra run successivi dello
    scan: un gruppo non ancora risolto per quel recording id viene
    aggiornato sul posto (membri ricalcolati), mai duplicato — vedi
    ``db/duplicate_store.py::upsert_group_for_recording``. Un gruppo già
    risolto/dismesso resta storico e non viene mai più toccato dallo scan;
    un nuovo duplicato dello stesso recording apparso in seguito crea un
    nuovo gruppo.
    """

    __tablename__ = "duplicate_groups"
    __table_args__ = (
        Index("ix_duplicate_groups_recording_status", "recording_id", "status"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    recording_id: str = Field(index=True)
    status: str = Field(default="proposed", index=True)  # proposed | needs_review | resolved | dismissed
    reason: Optional[str] = Field(default=None)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    updated_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    resolved_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    # JSON list dei path effettivamente cancellati alla risoluzione — "cosa è
    # stato cancellato", richiesto esplicitamente per audit (vedi CLAUDE.md).
    deleted_paths: Optional[str] = Field(default=None)


class DuplicateMember(SQLModel, table=True):
    """
    Un file membro di un ``DuplicateGroup``. Ricreato per intero a ogni
    aggiornamento del gruppo (vedi ``upsert_group_for_recording``) così un
    rescan non lascia mai righe orfane con dati tecnici stantii.

    ``origin``/``download_id``: un file è cancellabile da musizeer SOLO se
    ``download_id`` punta a un ``Download`` con ``status == 'downloaded'`` il
    cui ``file_path`` normalizzato combacia con ``path`` (vedi
    ``db/duplicate_store.py::downloaded_file_paths``) — un file di origine
    ``unknown`` (piazzato da un sistema esterno o da uno scan) non viene mai
    proposto per la cancellazione (CLAUDE.md: mai cancellare dietro le spalle
    di un sistema esterno che potrebbe ri-scaricarlo).
    """

    __tablename__ = "duplicate_members"

    id: str = Field(default_factory=_uuid, primary_key=True)
    group_id: str = Field(foreign_key="duplicate_groups.id", index=True)
    sync_id: str
    path: str = Field(index=True)  # relativo, normalizzato (core.duplicates.normalize_library_path)
    title: str
    artist: str
    album: str = Field(default="")
    year: Optional[int] = Field(default=None)
    duration_ms: int = Field(default=0)
    size: int = Field(default=0, sa_type=BigInteger)  # bytes: a file can pass 2 GiB
    suffix: str = Field(default="")
    bitrate: int = Field(default=0)
    sample_rate: int = Field(default=0)
    lossless: Optional[bool] = Field(default=None)
    quality_known: bool = Field(default=False)
    fingerprint_score: Optional[float] = Field(default=None)
    origin: str = Field(default="unknown")  # "musizeer" | "unknown"
    download_id: Optional[str] = Field(default=None, foreign_key="downloads.id")
    is_keeper: bool = Field(default=False)
    deleted_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)


class DuplicateFingerprintCache(SQLModel, table=True):
    """
    Cache di identità per fingerprint, chiave (path, size): un file
    invariato (stesso path E stessa dimensione) non viene ri-fingerprintato
    da uno scan successivo. Una dimensione diversa (file sostituito/aggiornato)
    invalida naturalmente la cache — nessuna scadenza esplicita necessaria.
    Cache anche l'esito NEGATIVO (``recording_id is None`` — non
    identificabile) così un file irrimediabilmente non riconoscibile non
    viene ritentato a ogni run.
    """

    __tablename__ = "duplicate_fingerprint_cache"
    __table_args__ = (
        UniqueConstraint("path", "size", name="uq_duplicate_fingerprint_path_size"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    path: str = Field(index=True)
    size: int = Field(default=0, sa_type=BigInteger)
    recording_id: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    artist: Optional[str] = Field(default=None)
    score: Optional[float] = Field(default=None)
    computed_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


# What a playlist is (``Playlist.kind``): one the user keeps, or one of a
# metadata provider the user follows (read-only, mirrors its source).
PLAYLIST_OWN = "own"
PLAYLIST_FOLLOWED = "followed"


class Playlist(SQLModel, table=True):
    """A playlist of a user, kept by Musizeer (private: its owner only).

    ``own`` is the user's to edit. ``followed`` mirrors a playlist of a
    metadata provider (``source_provider`` / ``source_id``): read-only, its
    tracks are replaced when the source changes (``snapshot_id``: the version
    marker the provider gave it, ``checked_at``: when the host last looked).
    ``auto_download``: the tracks the source gains are downloaded (through the
    approval policy). ``refresh_error_code``: why the last look at the source
    failed (``NULL`` when it did not). ``tracks_unavailable_*``: the provider
    would not give the tracks (``musizeer_sdk.models.TracksUnavailable``). The tracks are
    in ``playlist_tracks``; the copies on the sync providers in
    ``playlist_syncs``. A user follows a source once (unique per user)."""
    __tablename__ = "playlists"
    __table_args__ = (
        UniqueConstraint("user_id", "source_provider", "source_id", name="uq_playlist_source"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    name: str
    description: str = Field(default="")
    kind: str = Field(default=PLAYLIST_OWN)
    source_provider: Optional[str] = Field(default=None)
    source_id: Optional[str] = Field(default=None)
    snapshot_id: Optional[str] = Field(default=None)
    cover_url: Optional[str] = Field(default=None)  # stored form: core.urls.to_host_ref
    auto_download: bool = Field(default=False)
    tracks_unavailable_code: Optional[str] = Field(default=None)
    tracks_unavailable_message: Optional[str] = Field(default=None)
    refresh_error_code: Optional[str] = Field(default=None)
    checked_at: Optional[datetime] = Field(sa_type=UTCDateTime, default=None)
    created_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)
    updated_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class PlaylistTrack(SQLModel, table=True):
    """One track of a playlist, at ``position`` (0-based, contiguous: the
    store rewrites them on every change). A track is in a playlist once.
    ``download_requested``: the track was not in the library when the playlist
    asked for its download (a followed playlist): the one the user may choose to
    delete with the playlist, if nothing else keeps it."""
    __tablename__ = "playlist_tracks"
    __table_args__ = (
        UniqueConstraint("playlist_id", "track_id", name="uq_playlist_track"),
        Index("ix_playlist_tracks_playlist_position", "playlist_id", "position"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    playlist_id: str = Field(foreign_key="playlists.id")
    track_id: str = Field(foreign_key="tracks.internal_id", index=True)
    position: int = Field(default=0)
    download_requested: bool = Field(default=False)
    added_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


class PlaylistSync(SQLModel, table=True):
    """The copy of a playlist on a sync provider, on its owner's own account:
    ``remote_id`` is the id the provider gave it and ``content_hash`` what was
    last pushed (the name and the provider's ids of the tracks, in order), so an
    unchanged playlist costs no call. ``playlist_id`` is NOT a foreign key: the
    row outlives its playlist until the routine has deleted the copy on the
    provider (``services/playlist_sync.py``)."""
    __tablename__ = "playlist_syncs"

    playlist_id: str = Field(primary_key=True)
    sync_provider: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    remote_id: str
    content_hash: str = Field(default="")
    synced_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)


SCHEMA_INFO_KEY = "schema"


class SchemaInfo(SQLModel, table=True):
    """What the last migration left behind (ADR-0007 §4): ONE row, key ``schema``. The code that migrated writes
    it (``services/database_init.py``) and every start reads it (``core/schema_guard.py`` judges): an app whose
    code does not know a revision of the database runs on it only when what it does not know is expand-only.

    ``revision``: the revisions the database was at after that run ("+"-joined when there are several);
    ``app_version``: the version that ran it; ``min_app_version``: the oldest version that can run on the
    result (raised by a contract migration, ``None`` until one is recorded); ``contract_revision``: the newest
    contract migration applied; ``phase``: ``contract`` when that run applied one, else ``expand``."""
    __tablename__ = "schema_info"

    id: str = Field(default=SCHEMA_INFO_KEY, primary_key=True)
    revision: str
    app_version: str
    min_app_version: Optional[str] = Field(default=None)
    contract_revision: Optional[str] = Field(default=None)
    phase: str = Field(default="expand")
    migrated_at: datetime = Field(sa_type=UTCDateTime, default_factory=_now)

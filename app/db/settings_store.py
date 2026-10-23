"""
Unico punto di accesso alla tabella ``settings``: impostazioni d'app, di
provider e override per utente.

PURA data-access: nessuna policy (``can_read``/``can_write``), nessuna
coercizione/validazione, nessun namespacing di chiave provider — tutto questo
resta in ``core/settings_resolver.py``/``services/settings.py``. Questo
modulo sa solo leggere/scrivere una ``key`` (già eventualmente namespaced dal
chiamante) per uno scope ``system`` o ``user``.

Provider-agnostico: nessun nome di provider concreto compare qui — le chiavi
namespaced (``provider.<nome>.<short_key>``) sono stringhe opache per questo
repository.

SECRET settings (``SettingDef.secret``: a catalog entry, or one a provider declares) are stored
SEALED (``db/credential_cipher.py``, docs/credentials.md): the JSON value of the row is the envelope
``{"$sealed": "enc:v1:..."}`` (``core.secret_box``) instead of the value. Whether a key is secret is
the declaration's: the catalog's for a catalog key (decided here), the caller's for a key a provider
declared (``secret=``, from the schema it holds; the repository knows no provider). READING needs no
declaration: a sealed row is opened wherever it is read. A sealed row the configured keys cannot
open reads as if the key had no row (the default), with a warning once; a catalog secret found in
clear is treated the same way (only the migration accepts it).
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from sqlalchemy import update
from sqlmodel import Session, select

from core import secret_box
from core.settings_catalog import get_def
from . import credential_cipher
from .models import NO_USER, Setting, _now
from .upsert import upsert

SCOPE_SYSTEM = "system"
SCOPE_USER = "user"

# What a row that cannot be read is, between ``_read`` and the callers that skip it.
_UNREADABLE = object()


def _catalog_secret(key: str) -> bool | None:
    """Whether the catalog declares ``key`` secret; None when the catalog does not know the key
    (a provider's, whose declaration only the caller holds)."""
    declared = get_def(key)
    return declared.secret if declared is not None else None


def _read(row: Setting) -> Any:
    """The value of a row, or ``_UNREADABLE``."""
    document = json.loads(row.value)
    token = secret_box.enveloped_token(document)
    if token is None:
        if _catalog_secret(row.key):
            # A catalog secret in clear: only the migration that seals them accepts it.
            credential_cipher.unseal(row.value, f"The setting '{row.key}'")
            return _UNREADABLE
        return document
    plaintext = credential_cipher.unseal(token, f"The setting '{row.key}'")
    return _UNREADABLE if plaintext is None else json.loads(plaintext)


class SettingsRepository:
    """Unico punto di accesso alla tabella ``settings``.

    Ogni scrittura è un ``INSERT ... ON CONFLICT DO UPDATE``: due scrittori
    della stessa chiave producono una sola riga (il vincolo UNIQUE per le righe
    utente, l'indice parziale per quelle di sistema: vedi ``Setting`` in
    ``db/models.py``).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Lettura
    # ------------------------------------------------------------------

    def get_system(self, key: str, default: Any = None) -> Any:
        return self._value(self._find(key, SCOPE_SYSTEM, None), default)

    def get_user(self, key: str, user_id: str, default: Any = None) -> Any:
        return self._value(self._find(key, SCOPE_USER, user_id), default)

    def resolve(self, key: str, user_id: Optional[str], default: Any = None) -> Any:
        """Cascata: riga dell'utente (se ``user_id`` è dato) -> riga di
        sistema -> ``default`` (tipicamente il default dichiarato nel
        catalogo). Usata sia per le impostazioni ``system_with_user_override``
        sia per qualunque altra lettura con fallback a cascata."""
        if user_id is not None:
            row = self._find(key, SCOPE_USER, user_id)
            if row is not None:
                value = _read(row)
                if value is not _UNREADABLE:
                    return value
        return self._value(self._find(key, SCOPE_SYSTEM, None), default)

    def all_system(self) -> dict[str, Any]:
        """{key: value} per OGNI riga scope='system'. Usato da ``ConfigManager``
        per popolare la sua cache in un colpo solo (una query, non una per
        property) e da ``SettingsService`` per ricostruire la config di un
        provider filtrando per prefisso di chiave. A row that cannot be read is
        left out, like a key with no row."""
        rows = self._session.exec(select(Setting).where(Setting.scope == SCOPE_SYSTEM)).all()
        return self._values(rows)

    def all_user(self, user_id: str) -> dict[str, Any]:
        rows = self._session.exec(
            select(Setting).where(Setting.scope == SCOPE_USER, Setting.user_id == user_id)
        ).all()
        return self._values(rows)

    # ------------------------------------------------------------------
    # Scrittura
    # ------------------------------------------------------------------

    def set_system(self, key: str, value: Any, *, secret: bool | None = None, commit: bool = True) -> None:
        """``secret``: the declaration of a key a provider declared (the schema's ``secret``); left
        out, the catalog decides. ``commit=False``: the write joins the caller's transaction, which
        the caller commits (the switch of authentication, with the adoption of the owner's data)."""
        self._upsert(key, SCOPE_SYSTEM, None, value, secret, commit)

    def set_user(self, key: str, user_id: str, value: Any, *, secret: bool | None = None) -> None:
        self._upsert(key, SCOPE_USER, user_id, value, secret)

    def seal_system_in_clear(self, keys: Iterable[str]) -> int:
        """Seal the system rows of ``keys`` that are stored in clear; how many. The legacy intake
        for the secret settings only the registry knows (a provider's: the migration cannot tell
        which of them are secret). Idempotent, and safe against a writer: a row is sealed only if
        it still holds what was read."""
        sealed = 0
        for key in keys:
            row = self._find(key, SCOPE_SYSTEM, None)
            if row is None or secret_box.enveloped_token(json.loads(row.value)) is not None:
                continue
            replaced = self._session.exec(
                update(Setting)
                .where(Setting.id == row.id, Setting.value == row.value)
                .values(value=self._encode(json.loads(row.value), True), updated_at=_now())
            )
            sealed += replaced.rowcount
        self._session.commit()
        return sealed

    # ------------------------------------------------------------------
    # Helper interni
    # ------------------------------------------------------------------

    def _find(self, key: str, scope: str, user_id: Optional[str]) -> Setting | None:
        stmt = select(Setting).where(
            Setting.key == key, Setting.scope == scope, Setting.user_id == user_id,
        )
        return self._session.exec(stmt).first()

    @staticmethod
    def _value(row: Setting | None, default: Any) -> Any:
        if row is None:
            return default
        value = _read(row)
        return default if value is _UNREADABLE else value

    @staticmethod
    def _values(rows: Iterable[Setting]) -> dict[str, Any]:
        values = {row.key: _read(row) for row in rows}
        return {key: value for key, value in values.items() if value is not _UNREADABLE}

    @staticmethod
    def _encode(value: Any, secret: bool) -> str:
        text = json.dumps(value)
        return json.dumps(secret_box.envelope(credential_cipher.seal(text))) if secret else text

    def _upsert(
        self, key: str, scope: str, user_id: Optional[str], value: Any, secret: bool | None, commit: bool = True,
    ) -> None:
        declared = _catalog_secret(key) if secret is None else secret
        upsert(
            self._session,
            Setting(key=key, scope=scope, user_id=user_id, value=self._encode(value, bool(declared))),
            conflict=("key", "scope") if user_id is None else ("key", "scope", "user_id"),
            where=NO_USER if user_id is None else None,
            update=("value", "updated_at"),
        )
        if commit:
            self._session.commit()

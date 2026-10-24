"""
Moving the stored credentials to a new key (docs/credentials.md, ``scripts/rotate_credentials_key.py``).

A rotation is: make the new key ``MUSIZEER_CREDENTIALS_KEY``, keep the one it replaces in
``MUSIZEER_CREDENTIALS_KEY_PREVIOUS`` (the application then reads both and seals with the new one), run this to
seal every value again with the new key, then take the old key off ``MUSIZEER_CREDENTIALS_KEY_PREVIOUS``. This
module does the middle step, for every place a sealed value lives: ``provider_credentials.data``,
``provider_sources.credential`` and the settings rows whose value is a sealed envelope (the secret
settings of the catalog and of the providers).

It reports COUNTS, never a value. A value that no configured key opens is left exactly as it is (and
counted), as is one found in clear in a column that is always sealed: this is not the place that
encrypts, the migration is. Each row is rewritten by a conditional UPDATE, so a writer that changed
it meanwhile is not overwritten (that row is counted as ``skipped`` and the next run takes it).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa
from sqlmodel import Session

from core import secret_box
from db import credential_cipher
from db.models import ProviderCredential, ProviderSource, Setting


@dataclass
class TableRotation:
    table: str
    current: int = 0      # already sealed with the primary key
    rewritten: int = 0    # sealed with another configured key: sealed again (or, in a dry run, would be)
    unreadable: int = 0   # sealed with a key that is not configured: left as it is
    in_clear: int = 0     # not sealed, in a column that always is: left as it is
    skipped: int = 0      # changed by someone else while the run was going: left for the next run

    @property
    def needs_attention(self) -> bool:
        return bool(self.unreadable or self.in_clear or self.skipped)


def rotate(session: Session, *, apply: bool) -> list[TableRotation]:
    """Count (and, with ``apply``, rewrite) the sealed values of every table that holds some, with
    the primary key. ``CredentialsKeyError`` if the configured key is unusable."""
    box = credential_cipher.box()
    return [
        _rotate_column(session, box, "provider_credentials", ProviderCredential.__table__, "data", apply, sealed=_plain),
        _rotate_column(session, box, "provider_sources", ProviderSource.__table__, "credential", apply, sealed=_plain),
        _rotate_column(session, box, "settings", Setting.__table__, "value", apply, sealed=_enveloped),
    ]


def _plain(stored: str) -> str | None:
    """The sealed text a column holds when it holds the text itself."""
    return stored if secret_box.is_sealed(stored) else None


def _enveloped(stored: str) -> str | None:
    try:
        return secret_box.enveloped_token(json.loads(stored))
    except ValueError:
        return None


def _rotate_column(session, box, name, table, column, apply, *, sealed) -> TableRotation:
    report = TableRotation(name)
    always_sealed = name != "settings"  # a setting is sealed only when it is secret
    col = table.c[column]
    query = sa.select(table.c.id, col)
    if name == "provider_sources":
        query = query.where(col.is_not(None))
    for row_id, stored in session.execute(query).all():
        token = sealed(stored)
        if token is None:
            if always_sealed:
                report.in_clear += 1
            continue
        if box.is_current(token):
            report.current += 1
            continue
        try:
            fresh = box.reseal(token)
        except secret_box.Unreadable:
            report.unreadable += 1
            continue
        if not apply:
            report.rewritten += 1
            continue
        new = json.dumps(secret_box.envelope(fresh)) if name == "settings" else fresh
        changed = session.execute(
            sa.update(table).where(table.c.id == row_id, col == stored).values({column: new})
        ).rowcount
        report.rewritten += changed
        report.skipped += 1 - changed
    session.commit()
    return report

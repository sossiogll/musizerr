"""
Authentication goes from off to on (``docs/permissions.md``, "Turning authentication on").

The write of ``auth_enabled: true`` is not an ordinary setting write: in the SAME transaction the
open-mode owner's data becomes an administrator's (``db.adoption``), so a failure half way leaves
the instance exactly as it was (authentication off, the data with the owner). Going from on to off
is an ordinary write: nothing moves back (the administrator's data stays theirs, open mode starts
from what the synthetic user still holds).

Called only by ``SettingsService.write_values``, after the ``guard_on`` checks of the setting
passed; the checks are asked again here under the lock, since an administrator may have been
deactivated or deleted since.
"""
from __future__ import annotations

import logging

from sqlmodel import Session

from db import adoption, user_service
from db.locks import xact_lock
from db.models import User
from db.settings_store import SettingsRepository
from musizerr_sdk.auth import AccountModel
from services.config_manager import ConfigManager
from services.linked_accounts import unique_providers

logger = logging.getLogger(__name__)

SETTING = "auth_enabled"
LOCK = "authentication-transition"


class TransitionRefused(Exception):
    """The switch cannot be made as asked: ``error_code`` is the stable, translatable code."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def personal_only_providers(registry) -> list[str]:
    """The providers whose accounts are personal only (``AccountModel.PERSONAL``): the ones whose
    owner's account moves with the owner's data."""
    return [p.name for p in unique_providers(registry) if p.auth.account_model == AccountModel.PERSONAL]


def choose_administrator(session: Session, administrator_id: str | None) -> User:
    """The administrator who takes the open-mode data: the one asked for, who must be able to sign
    in; else the oldest that can."""
    candidates = user_service.administrators(session)
    if administrator_id is None:
        if not candidates:
            raise TransitionRefused(
                "no_admin_account",
                "Authentication cannot be turned on: no active administrator account exists to sign in with.",
            )
        return candidates[0]
    for candidate in candidates:
        if candidate.id == administrator_id:
            return candidate
    raise TransitionRefused(
        "adoption_target_invalid",
        "The data of the instance can only go to an active administrator account that can sign in.",
    )


def turn_on(
    session: Session, config: ConfigManager, registry, *, administrator_id: str | None = None,
) -> adoption.Adoption | None:
    """Writes ``auth_enabled: true`` and adopts the owner's data into ``administrator_id`` (the
    oldest administrator that can sign in when not given), in ONE transaction. Returns what was
    adopted, or None when authentication was already on (nothing to adopt: the value is written
    again, harmlessly). Raises ``TransitionRefused`` with nothing changed."""
    repository = SettingsRepository(session)
    try:
        xact_lock(session, LOCK)
        if repository.get_system(SETTING, False):
            return None
        administrator = choose_administrator(session, administrator_id)
        report = adoption.adopt(session, administrator.id, personal_only_providers=personal_only_providers(registry))
        repository.set_system(SETTING, True, commit=False)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    config.committed({SETTING: True})
    logger.info(
        "Authentication turned on; the data of the instance's owner went to '%s': %d row(s) moved%s.",
        administrator.username, report.total_moved,
        f", {sum(report.left.values())} left (the administrator had their own)" if report.left else "",
    )
    return report

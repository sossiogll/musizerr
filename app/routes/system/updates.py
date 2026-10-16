"""
GET /admin/updates: the version this instance runs and the newer releases of Musizerr, each with what
updating to it implies now (ADR-0007 §5.1, ``services/update_check.py``). Admin only; reads only. The
check itself is the routine ``check_for_updates`` (its switch, its "Run now").
"""
from __future__ import annotations

from fastapi import APIRouter

from core.routines import enabled_setting_key, get_routine
from routes.deps import AdminUserDep, ConfigDep, RegistryDep, SessionDep
from services import update_check
from services.settings import SettingsService

router = APIRouter()


@router.get("/admin/updates")
def get_updates(admin: AdminUserDep, config: ConfigDep, registry: RegistryDep, session: SessionDep):
    routine = get_routine(update_check.ROUTINE)
    values = SettingsService(session, config, registry).effective_values(admin, include_providers=False)
    enabled = bool(values.get(enabled_setting_key(update_check.ROUTINE), routine.default_enabled if routine else False))
    return update_check.view(session, check_enabled=enabled)

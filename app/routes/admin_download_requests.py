"""The admin's side of the download approval policy: the requests that wait
(and the history), approving and denying them (always by ids, one or many) and
the general policy (``services/download_requests.py``,
``services/request_policy.py``). A user's own overrides are with the user's
other admin routes (``routes/admin.py``)."""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from core import download_policy as policy
from db import download_request_store as store
from routes._pagination import paginated_response
from routes.deps import AdminUserDep, AuthManagerDep, ConfigDep, RegistryDep, SessionDep
from services import download_requests, request_policy
from services.settings import SettingsWriteError

logger = logging.getLogger(__name__)

RequestStatus = Literal[policy.STATUSES]  # type: ignore[valid-type]
RequestKind = Literal[policy.KINDS]  # type: ignore[valid-type]
Mode = Literal[policy.MODES]  # type: ignore[valid-type]

# The most requests one batch decides.
MAX_BATCH = 100
# What a batch answers for a request whose decision raised (it stays as it was).
FAILED = "failed"

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveBody(_Body):
    ids: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    note: str | None = Field(None, max_length=500)


class DenyBody(_Body):
    ids: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    reason: str | None = Field(None, max_length=500)


class RulePatch(_Body):
    mode: Mode | None = None
    threshold: int | None = Field(None, ge=1)


class QuotaPatch(_Body):
    tracks: int | None = Field(None, ge=0)
    window_days: int | None = Field(None, ge=1)


class GeneralPolicyPatch(_Body):
    rules: dict[RequestKind, RulePatch] | None = None
    quota: QuotaPatch | None = None


@router.get("/download-requests")
def list_requests(
    admin: AdminUserDep,  # noqa: ARG001
    session: SessionDep,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    status: Annotated[list[RequestStatus] | None, Query()] = None,
    kind: Annotated[list[RequestKind] | None, Query()] = None,
    user_id: str | None = Query(None),
):
    """Every user's download requests, newest first, with who asked and who
    decided. ``status`` and ``kind`` (repeatable) and ``user_id`` filter."""
    items, total = download_requests.list_for_admin(
        session, statuses=status or [], kinds=kind or [], user_id=user_id, page=page, limit=limit,
    )
    return paginated_response(items, total=total, page=page, limit=limit)


@router.get("/download-requests/pending-count")
def pending_count(admin: AdminUserDep, session: SessionDep):  # noqa: ARG001
    """How many requests wait for an admin (cheap: the badge)."""
    return {"pending": store.count_pending(session)}


def _decision_item(session, request_id: str, result) -> dict:
    return {
        "id": request_id,
        "outcome": result.outcome,
        "request": download_requests.admin_view(session, result.request) if result.request else None,
    }


@router.post("/download-requests/approve")
def approve_many(
    body: ApproveBody, admin: AdminUserDep, session: SessionDep, config: ConfigDep, registry: RegistryDep,
    auth_manager: AuthManagerDep,
):
    """Approves one or many requests, one by one: the tracks of each go through
    the gate exactly once and its favorite on arrival is registered now. The
    answer lists each id with its ``outcome`` — ``decided`` (this call approved
    it), ``unchanged`` (it was already approved), ``conflict`` (decided
    otherwise), ``not_found``, or ``failed`` (queueing it raised: it stays
    pending)."""
    results = []
    for request_id in dict.fromkeys(body.ids):
        try:
            result = download_requests.approve(
                session, request_id, admin, note=body.note, config=config, registry=registry, auth_manager=auth_manager,
            )
        except Exception as exc:  # noqa: BLE001 — one request failing does not stop the others
            logger.warning("Approving download request %s failed: %s", request_id, exc)
            session.rollback()
            results.append({"id": request_id, "outcome": FAILED, "request": None})
            continue
        results.append(_decision_item(session, request_id, result))
    return {"results": results}


@router.post("/download-requests/deny")
def deny_many(body: DenyBody, admin: AdminUserDep, session: SessionDep):
    """Denies one or many requests: nothing was queued and no favorite
    registered (same answer as the approval)."""
    return {"results": [
        _decision_item(session, request_id, download_requests.deny(session, request_id, admin, reason=body.reason))
        for request_id in dict.fromkeys(body.ids)
    ]}


@router.get("/download-policy")
def get_policy(admin: AdminUserDep, config: ConfigDep):  # noqa: ARG001
    """The general approval policy: the mode and threshold of each kind of
    request and the per-user quota (the settings of the ``download_requests``
    group)."""
    return request_policy.read_general(config)


@router.patch("/download-policy")
def update_policy(
    body: GeneralPolicyPatch, admin: AdminUserDep, session: SessionDep, config: ConfigDep, registry: RegistryDep,
):
    """Changes the general policy (every field optional), through the settings
    catalogue: validated like any setting, all or nothing."""
    try:
        return request_policy.write_general(session, config, registry, admin, body.model_dump(exclude_none=True))
    except SettingsWriteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

"""Admin endpoints for user management."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from core import download_policy, user_auth
from db import user_service
from db.models import SYSTEM_USER_ID
from services import request_policy
from .deps import AdminUserDep, ConfigDep, SessionDep
from ._pagination import paginated_response

router = APIRouter()


class UserPublic(BaseModel):
    id: str
    username: str
    email: Optional[str]
    role: str
    is_active: bool
    # Created by a sign-in through a provider and waiting for an admin to activate it.
    pending_approval: bool = False


class UserPatch(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


def _managed_user(session, user_id: str):
    """The account ``user_id`` names; the system user is not an account."""
    user = user_service.get_user_by_id(session, user_id)
    if not user or user.id == SYSTEM_USER_ID:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/users")
def list_users(admin: AdminUserDep, session: SessionDep):  # noqa: ARG001
    items = [
        UserPublic(
            id=u.id, username=u.username, email=u.email,
            role=u.role, is_active=u.is_active, pending_approval=u.pending_approval,
        ).model_dump()
        for u in user_service.list_users(session)
    ]
    return paginated_response(items)


@router.patch("/users/{user_id}", response_model=UserPublic)
def update_user(user_id: str, body: UserPatch, admin: AdminUserDep, session: SessionDep):
    user = _managed_user(session, user_id)

    if user.id == admin.id and body.role and body.role != "admin":
        raise HTTPException(status_code=400, detail="Cannot remove your own admin role")

    fields: dict = {}
    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")
        fields["role"] = body.role
    if body.is_active is not None:
        if user.id == admin.id and not body.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
        fields["is_active"] = body.is_active
        if body.is_active:
            fields["pending_approval"] = False  # activating an account is approving it
    if body.password is not None:
        fields["password_hash"] = user_auth.hash_password(body.password)

    user = user_service.update_user(session, user, **fields)
    return UserPublic(
        id=user.id, username=user.username, email=user.email,
        role=user.role, is_active=user.is_active, pending_approval=user.pending_approval,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, admin: AdminUserDep, session: SessionDep):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user_service.delete_user(session, _managed_user(session, user_id))


# ------------------------------------------------------------------
# A user's download approval policy (the general one: routes/admin_download_requests.py)
# ------------------------------------------------------------------

class UserRulePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Optional[Literal[download_policy.MODES]] = None  # type: ignore[valid-type]
    threshold: Optional[int] = Field(None, ge=1)


class UserDownloadPolicy(BaseModel):
    """What an admin changes, for one user, in the download approval policy; a
    ``null`` (or a missing field) inherits the general policy, and
    ``quota_tracks: 0`` means no quota for this user."""
    model_config = ConfigDict(extra="forbid")

    rules: Optional[dict[Literal[download_policy.KINDS], UserRulePolicy]] = None  # type: ignore[valid-type]
    quota_tracks: Optional[int] = Field(None, ge=0)


@router.get("/users/{user_id}/download-policy")
def get_user_download_policy(user_id: str, admin: AdminUserDep, session: SessionDep, config: ConfigDep):  # noqa: ARG001
    """What the admin changed for this user (``override``) and the policy that
    results (``effective``)."""
    _managed_user(session, user_id)
    return request_policy.read_user(session, config, user_id)


@router.put("/users/{user_id}/download-policy")
def set_user_download_policy(
    user_id: str, body: UserDownloadPolicy, admin: AdminUserDep, session: SessionDep, config: ConfigDep,  # noqa: ARG001
):
    """Replaces the user's override with the body (an empty one clears it). A
    user cannot change their own policy: this route is admin-only."""
    _managed_user(session, user_id)
    try:
        return request_policy.write_user(session, config, user_id, body.model_dump(), datetime.now(timezone.utc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/users/{user_id}/download-policy")
def clear_user_download_policy(user_id: str, admin: AdminUserDep, session: SessionDep, config: ConfigDep):  # noqa: ARG001
    """Clears the user's override: they follow the general policy again."""
    _managed_user(session, user_id)
    return request_policy.clear_user(session, config, user_id)

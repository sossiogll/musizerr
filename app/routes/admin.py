"""Admin endpoints for user management."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core import download_policy
from db import user_service
from db.models import SYSTEM_USER_ID
from services import request_policy
from ._password import new_password_hash
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


class UserCreate(BaseModel):
    """A new account made by an administrator (Administration › Users)."""
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=150)
    password: str = Field(max_length=1024)  # the rule is user_auth.hash_new_password's, answered as a coded 422
    email: Optional[str] = Field(None, max_length=320)
    role: Literal["admin", "user"] = "user"

    @field_validator("username")
    @classmethod
    def _username_is_text(cls, value: str) -> str:
        value = value.strip()
        if not value or value == SYSTEM_USER_ID:
            raise ValueError("username is not valid")
        return value

    @field_validator("email")
    @classmethod
    def _email_or_nothing(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("email is not valid")
        return value or None


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


@router.post("/users", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, admin: AdminUserDep, session: SessionDep):  # noqa: ARG001
    """A new, active account with the role asked, whether authentication is on or off (with it off
    the accounts wait for it to be turned on, and the first administrator is what turning it on
    needs). The username and the email must be free (409 ``username_taken`` / ``email_taken``);
    the password is hashed like every other."""
    try:
        user = user_service.create_account(
            session, username=body.username, password_hash=new_password_hash(body.password),
            email=body.email, role=body.role,
        )
    except user_service.AccountExists as exists:
        code = "email_taken" if exists.field == "email" else "username_taken"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": code, "error_message": f"The {exists.field} is already taken."},
        ) from None
    return UserPublic(
        id=user.id, username=user.username, email=user.email,
        role=user.role, is_active=user.is_active, pending_approval=user.pending_approval,
    )


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
        fields["password_hash"] = new_password_hash(body.password)

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

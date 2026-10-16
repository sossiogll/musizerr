"""User authentication endpoints: register, login, refresh, logout, me."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from core import user_auth
from db import user_service
from db.models import User
from services import download_requests, refresh_tokens
from services.capabilities import capabilities_for, viewer_mode

from .deps import ConfigDep, CurrentUserDep, OptionalUserDep, SessionDep

_bearer_optional = HTTPBearer(auto_error=False)

router = APIRouter()


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------

class RegisterBody(BaseModel):
    username: str
    password: str
    email: Optional[str] = None


class LoginBody(BaseModel):
    username: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserPublic(BaseModel):
    id: str
    username: str
    email: Optional[str]
    role: str
    is_active: bool


# ------------------------------------------------------------------
# Global auth status
# ------------------------------------------------------------------

@router.get("/status")
def auth_status(config: ConfigDep, user: OptionalUserDep, session: SessionDep):
    """Stato dell'autenticazione e, per chi sta guardando, modalità e
    capability (``services/capabilities.py``). Un token assente, scaduto o di
    un utente disattivato vale come guest.

    ``download_policy`` dice alla UI cosa succederebbe ORA a una richiesta di
    download di chi guarda, per tipo (``services/download_requests.py``):
    ``applies`` è falso per un admin e in modalità aperta; ``null`` per un
    guest, che non scarica. Il server resta l'unico giudice al momento della
    richiesta."""
    mode = viewer_mode(config.auth_enabled, user)
    return {
        "auth_enabled": config.auth_enabled,
        "registration_open": config.registration_open,
        "mode": mode,
        "capabilities": capabilities_for(mode).as_dict(),
        "download_policy": None if mode == "guest" else download_requests.policy_status(session, user, config),
    }


# ------------------------------------------------------------------
# Register
# ------------------------------------------------------------------

def _optional_admin(
    session: SessionDep,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer_optional)] = None,
) -> Optional[User]:
    """Return the calling User if they're an admin, else None. Never raises."""
    if credentials is None:
        return None
    try:
        payload = user_auth.decode_access_token(credentials.credentials)
    except Exception:
        return None
    user = user_service.get_user_by_id(session, payload["sub"])
    if user and user.is_active and user.role == "admin":
        return user
    return None


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterBody,
    session: SessionDep,
    config: ConfigDep,
    caller: Annotated[Optional[User], Depends(_optional_admin)] = None,
):
    if not config.auth_enabled:
        raise HTTPException(status_code=400, detail="Authentication is disabled")

    if not config.registration_open and not caller:
        raise HTTPException(status_code=403, detail="Registration is closed — admin only")

    if user_service.get_user_by_username(session, body.username):
        raise HTTPException(status_code=409, detail="Username already taken")
    if body.email and user_service.get_user_by_email(session, body.email):
        raise HTTPException(status_code=409, detail="Email already registered")

    hashed = user_auth.hash_password(body.password)
    try:
        user = user_service.create_user(
            session,
            username=body.username,
            password_hash=hashed,
            email=body.email,
        )
    except IntegrityError:  # the same username or email registered meanwhile
        session.rollback()
        raise HTTPException(status_code=409, detail="Username or email already taken") from None
    return UserPublic(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
    )


# ------------------------------------------------------------------
# Login
# ------------------------------------------------------------------

@router.post("/login", response_model=TokenResponse)
def login(body: LoginBody, session: SessionDep, config: ConfigDep):
    if not config.auth_enabled:
        raise HTTPException(status_code=400, detail="Authentication is disabled")

    user = user_service.get_user_by_username(session, body.username)
    if not user or not user_auth.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    pair = refresh_tokens.start_session(session, user)
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


# ------------------------------------------------------------------
# Refresh
# ------------------------------------------------------------------

@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshBody, session: SessionDep, config: ConfigDep):
    """Rotates the refresh token (``services/refresh_tokens.py``): a tab
    presenting the token another tab rotated moments ago gets the same pair."""
    if not config.auth_enabled:
        raise HTTPException(status_code=400, detail="Authentication is disabled")
    try:
        pair = refresh_tokens.refresh_session(session, body.refresh_token)
    except refresh_tokens.RefreshRefused as refused:
        raise HTTPException(status_code=401, detail=refused.reason)
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


# ------------------------------------------------------------------
# Stream session — HttpOnly cookie that authorizes the full-audio routes
# (routes/stream.py) for the <audio> element, which cannot carry a bearer
# header. See routes/deps.get_stream_user and docs/permissions.md.
# ------------------------------------------------------------------

def cookie_is_secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


@router.post("/stream-session", status_code=status.HTTP_204_NO_CONTENT)
def start_stream_session(request: Request, response: Response, user: CurrentUserDep):
    """Mint a short-lived stream token and set it as an HttpOnly cookie.

    Called by the frontend right after login and after every access-token
    refresh (see musizerr-ui/src/lib/api-client.ts), so the cookie stays
    valid for as long as the signed-in session does."""
    token = user_auth.create_stream_token(user.id)
    response.set_cookie(
        key=user_auth.STREAM_COOKIE_NAME,
        value=token,
        max_age=user_auth.STREAM_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        path="/",
    )


@router.delete("/stream-session", status_code=status.HTTP_204_NO_CONTENT)
def end_stream_session(response: Response):
    response.delete_cookie(user_auth.STREAM_COOKIE_NAME, path="/")


# ------------------------------------------------------------------
# Logout
# ------------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshBody, session: SessionDep, response: Response):
    token_hash = user_auth.hash_refresh_token(body.refresh_token)
    user_service.revoke_refresh_token(session, token_hash)
    # La sessione di streaming (vedi sopra) va chiusa insieme al login vero e
    # proprio — altrimenti il cookie di streaming resterebbe valido fino alla
    # sua scadenza anche dopo un logout esplicito.
    response.delete_cookie(user_auth.STREAM_COOKIE_NAME, path="/")


# ------------------------------------------------------------------
# Me
# ------------------------------------------------------------------

@router.get("/me", response_model=UserPublic)
def me(user: CurrentUserDep):
    return UserPublic(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
    )


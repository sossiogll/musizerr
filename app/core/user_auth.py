"""Password hashing and JWT token generation for Musizerr user auth.

Uses bcrypt for passwords and PyJWT for tokens.  All secrets come from
environment variables — nothing is hardcoded.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7


MIN_SECRET_LENGTH = 32
# The shortest password an administrator may give a new account (Administration › Users).
MIN_PASSWORD_LENGTH = 8
# The values that ship in examples and old compose files: public, so anyone can forge a token with them.
PLACEHOLDER_SECRETS = frozenset({"change-me-in-production", "change-me", "secret"})


class InsecureAuthSecret(RuntimeError):
    """``AUTH_SECRET`` cannot sign sessions: it is missing, too short or a published placeholder.
    The message says what is wrong and how to fix it, and never contains the value."""


def _get_secret() -> str:
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        raise RuntimeError("AUTH_SECRET environment variable is not set")
    return secret


def require_strong_secret() -> None:
    """Raise ``InsecureAuthSecret`` unless ``AUTH_SECRET`` is safe to sign sessions with: set, at
    least ``MIN_SECRET_LENGTH`` characters and not one of ``PLACEHOLDER_SECRETS``. The one rule,
    asked when authentication is on at startup (``main.lifespan``) and before a write turns it on
    (the ``guard_on`` of the ``auth_enabled`` setting)."""
    secret = os.environ.get("AUTH_SECRET", "").strip()
    if not secret:
        problem = "is not set or is empty"
    elif secret.lower() in PLACEHOLDER_SECRETS:
        problem = "is a public placeholder: anyone could forge an administrator's session token with it"
    elif len(secret) < MIN_SECRET_LENGTH:
        problem = f"is too short ({len(secret)} characters, at least {MIN_SECRET_LENGTH} are required)"
    else:
        return
    raise InsecureAuthSecret(
        f"AUTH_SECRET {problem}. Sessions are signed with it. "
        f"Set AUTH_SECRET to a long random value (at least {MIN_SECRET_LENGTH} characters), e.g. in .env: "
        "AUTH_SECRET=$(openssl rand -hex 32). Every user will have to sign in again."
    )


# ------------------------------------------------------------------
# Password hashing
# ------------------------------------------------------------------

# bcrypt reads at most 72 bytes of a password. Up to 4.x it ignored the rest; 5.0 raises ValueError instead,
# which would be a 500 on sign-in for anyone whose password is longer. The bytes bcrypt ignored are cut here,
# so the hashes already stored (of any password length) keep verifying.
_BCRYPT_MAX_BYTES = 72


def _password_bytes(plain: str) -> bytes:
    return plain.encode()[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_password_bytes(plain), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(_password_bytes(plain), hashed.encode())


# ------------------------------------------------------------------
# JWT
# ------------------------------------------------------------------

def create_access_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def create_refresh_token() -> tuple[str, str, datetime]:
    """Return (raw_token, sha256_hex_hash, expires_at)."""
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return raw, token_hash, expires_at


def successor_refresh_token(raw: str) -> tuple[str, str, datetime]:
    """The token that replaces ``raw`` when it is rotated: (raw, hash, expires_at).

    Derived from ``raw`` with the server secret instead of drawn at random,
    so the server can hand the same successor again to a second tab that
    presents ``raw`` within the rotation grace window, without ever storing
    a token in clear. Only who holds ``raw`` AND the secret can compute it."""
    digest = hmac.new(_get_secret().encode(), b"refresh-successor\0" + raw.encode(), hashlib.sha256).digest()
    successor = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return successor, hash_refresh_token(successor), expires_at


def decode_access_token(token: str) -> dict:
    """Decode and validate an access token. Raises jwt.PyJWTError on failure."""
    payload = jwt.decode(token, _get_secret(), algorithms=[_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ------------------------------------------------------------------
# OAuth state token — short-lived JWT carrying user_id through the
# OAuth2 authorize → callback browser redirect.
# ------------------------------------------------------------------

_OAUTH_STATE_EXPIRE_MINUTES = 10


def create_oauth_state_token(user_id: str, target: str) -> str:
    """The state of an authorization in flight: who started it and which
    account (``core.auth.AccountTarget`` value) the token will become."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "target": target,
        "type": "oauth_state",
        "iat": now,
        "exp": now + timedelta(minutes=_OAUTH_STATE_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def decode_oauth_state_token(token: str) -> tuple[str, str]:
    """Decode an oauth_state token into ``(user_id, target)``. Raises jwt.PyJWTError on failure."""
    payload = jwt.decode(token, _get_secret(), algorithms=[_ALGORITHM])
    if payload.get("type") != "oauth_state" or "target" not in payload:
        raise jwt.InvalidTokenError("Not an oauth_state token")
    return payload["sub"], payload["target"]


# ------------------------------------------------------------------
# Sign-in through a provider (core/provider_login.py). The callback of the
# authorization is shared with the linking flow, and the state tells them
# apart: ``oauth_state`` carries a signed-in user, ``oauth_login_state``
# carries nobody. Nobody is signed in, so the state alone cannot say the
# browser that comes back is the one that left: it carries the hash of a
# random nonce the start also put in an HttpOnly cookie, and the callback
# needs both (otherwise an attacker could have a victim finish the attacker's
# own authorization and land in the attacker's account).
# ------------------------------------------------------------------

LOGIN_NONCE_COOKIE_NAME = "musizerr_login_nonce"
LOGIN_STATE_EXPIRE_MINUTES = _OAUTH_STATE_EXPIRE_MINUTES
# The one-time code the callback hands the browser, and how long it lives: the
# browser exchanges it for a session at once.
LOGIN_CODE_EXPIRE_SECONDS = 60


def new_login_nonce() -> str:
    return secrets.token_urlsafe(32)


def hash_login_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def create_login_state_token(provider: str, nonce: str) -> str:
    """The state of a sign-in authorization in flight: the provider it was
    started for and the hash of the nonce held by the browser that started it."""
    now = datetime.now(timezone.utc)
    payload = {
        "provider": provider,
        "nid": hash_login_nonce(nonce),
        "type": "oauth_login_state",
        "iat": now,
        "exp": now + timedelta(minutes=LOGIN_STATE_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def state_token_type(token: str) -> str:
    """The ``type`` of a verified state token (expired or forged ones raise
    jwt.PyJWTError): which flow the callback it came back to belongs to."""
    return str(jwt.decode(token, _get_secret(), algorithms=[_ALGORITHM]).get("type", ""))


def verify_login_state_token(token: str, *, provider: str, nonce: str | None) -> None:
    """Raises jwt.PyJWTError unless ``token`` is a sign-in state started for
    ``provider`` by the browser that holds ``nonce``."""
    payload = jwt.decode(token, _get_secret(), algorithms=[_ALGORITHM])
    if payload.get("type") != "oauth_login_state":
        raise jwt.InvalidTokenError("Not a login state token")
    if payload.get("provider") != provider:
        raise jwt.InvalidTokenError("State started for another provider")
    if not nonce or not hmac.compare_digest(str(payload.get("nid", "")), hash_login_nonce(nonce)):
        raise jwt.InvalidTokenError("State does not belong to this browser")


def new_login_code() -> tuple[str, str]:
    """(raw code, its SHA-256 hex): only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


# ------------------------------------------------------------------
# Stream token — cookie di sessione streaming per gli endpoint audio
# completi (routes/stream.py). Un <audio> non puo' portare un header
# Authorization, quindi l'identita' per quegli endpoint arriva da un cookie
# HttpOnly invece che da un bearer. `type` distinto ("stream") cosi' un
# access token non funziona come cookie di streaming e uno stream token non
# funziona come bearer — vedi routes/deps.get_stream_user e
# docs/permissions.md.
# ------------------------------------------------------------------

STREAM_COOKIE_NAME = "musizerr_stream_token"
# Stessa vita del token di accesso: il frontend rinnova il cookie ad ogni
# refresh (vedi musizerr-ui/src/lib/api-client.ts), quindi non serve una
# scadenza indipendente ne' un'impostazione dedicata.
STREAM_TOKEN_EXPIRE_MINUTES = ACCESS_TOKEN_EXPIRE_MINUTES


def create_stream_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": "stream",
        "iat": now,
        "exp": now + timedelta(minutes=STREAM_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def decode_stream_token(token: str) -> dict:
    """Decode and validate a stream token. Raises jwt.PyJWTError on failure."""
    payload = jwt.decode(token, _get_secret(), algorithms=[_ALGORITHM])
    if payload.get("type") != "stream":
        raise jwt.InvalidTokenError("Not a stream token")
    return payload

from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

_ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

from core import user_auth
from db import credential_cipher
from db.engine import init_db
from routes.deps import get_config, init_singletons
from routes.errors import install_error_handlers
from services import app_version
from services.log_redaction import install_log_redaction
from routes.auth import router as auth_router
from routes.users_auth import router as users_auth_router
from routes.provider_login import router as provider_login_router
from routes.admin import router as admin_router
from routes.admin_download_requests import router as admin_download_requests_router
from routes.download_requests import router as download_requests_router
from routes.search import router as search_router
from routes.content.track import router as track_router
from routes.content.album import router as album_router
from routes.content.playlist import router as playlist_router
from routes.playlists import router as playlists_router
from routes.content.artist import router as artist_router
from routes.config import router as config_router
from routes.sources import router as sources_router
from routes.history import router as history_router
from routes.stream import router as stream_router
from routes.system.progress import router as progress_router
from routes.system.health import router as health_router
from routes.system.routines import router as routines_router
from routes.system.updates import router as updates_router
from routes.library import router as library_router
from routes.library_duplicates import router as library_duplicates_router
from routes.discover import router as discover_router
from routes.external_utility import router as external_utility_router
from routes.delegation_webhook import router as delegation_webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
install_log_redaction()
logger = logging.getLogger(__name__)


def _bootstrap_admin() -> None:
    """Create an admin user on first boot if no users exist and env vars
    ADMIN_BOOTSTRAP_EMAIL / ADMIN_BOOTSTRAP_PASSWORD are set."""
    admin_email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL")
    admin_password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD")
    if not admin_email or not admin_password:
        return

    from db.engine import get_session as _get_session
    from db import user_service

    session = next(_get_session())
    try:
        if user_service.get_user_count(session) > 0:
            return
        try:
            hashed = user_auth.hash_new_password(admin_password)
        except user_auth.WeakPassword as weak:
            logger.error("ADMIN_BOOTSTRAP_PASSWORD was not used, no administrator was created: %s", weak)
            return
        user = user_service.create_user(
            session,
            username=admin_email.split("@")[0],
            password_hash=hashed,
            email=admin_email,
            role="admin",
        )
        logger.info("Bootstrap admin created: %s (id=%s)", user.username, user.id)
    finally:
        session.close()


def _ensure_system_user() -> None:
    """The user rows written with no sign-in (authentication off) belong to."""
    from db.engine import get_session as _get_session
    from db import user_service

    session = next(_get_session())
    try:
        user_service.ensure_system_user(session)
    finally:
        session.close()


def _require_auth_secret() -> None:
    """With authentication enabled the sessions are signed with ``AUTH_SECRET``: refuse to start
    on a missing, short or placeholder one. The ONE check, with no switch to skip it; it only runs
    when authentication is on (see ``tests/test_auth_secret_startup.py``)."""
    if get_config().auth_enabled:
        user_auth.require_strong_secret()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Musizerr...")
    # Before the database is touched: the migration that encrypts the stored credentials needs the
    # key, and nothing may run on credentials it cannot read or write (docs/credentials.md).
    credential_cipher.require_credentials_key()
    init_db()
    init_singletons()
    _require_auth_secret()
    _ensure_system_user()
    _bootstrap_admin()
    logger.info("Musizerr ready.")
    yield
    logger.info("Shutting down Musizerr.")


app = FastAPI(
    title="Musizerr",
    version=app_version.VERSION,
    lifespan=lifespan,
)

install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users_auth_router, prefix="/auth/account", tags=["user-auth"])
app.include_router(provider_login_router, prefix="/auth/account", tags=["user-auth"])
app.include_router(admin_router,     prefix="/admin",        tags=["admin"])
app.include_router(admin_download_requests_router, prefix="/admin", tags=["admin"])
app.include_router(auth_router,      prefix="/auth",         tags=["auth"])
app.include_router(search_router,   prefix="",          tags=["search"])
app.include_router(track_router,    prefix="",          tags=["track"])
app.include_router(album_router,    prefix="",          tags=["album"])
app.include_router(playlist_router, prefix="",          tags=["playlist"])
app.include_router(playlists_router, prefix="",         tags=["playlists"])
app.include_router(artist_router,   prefix="",          tags=["artist"])
app.include_router(config_router,   prefix="",          tags=["config"])
app.include_router(sources_router,  prefix="",          tags=["sources"])
app.include_router(history_router,  prefix="",          tags=["history"])
app.include_router(download_requests_router, prefix="", tags=["history"])
app.include_router(stream_router,   prefix="",          tags=["stream"])
app.include_router(progress_router, prefix="",          tags=["system"])
app.include_router(health_router,    prefix="",          tags=["system"])
app.include_router(routines_router,  prefix="",          tags=["system"])
app.include_router(updates_router,   prefix="",          tags=["system"])
app.include_router(library_router,   prefix="",          tags=["library"])
app.include_router(library_duplicates_router, prefix="", tags=["library"])
app.include_router(discover_router,  prefix="",          tags=["discover"])
app.include_router(external_utility_router, prefix="", tags=["external-utility"])
# Il proxy del frontend (vite in dev, nginx in prod) STRIPPA il prefisso /api
# prima di inoltrare: una chiamata del browser a /api/delegation/... arriva qui
# come /delegation/... . Il sistema esterno invece chiama public_base_url, che
# punta al backend diretto, mantenendo /api. Servono quindi entrambi i mount,
# come già fatto sopra per l'intake delle credenziali di un'utility esterna.
app.include_router(delegation_webhook_router, prefix="", tags=["delegation-webhook"])

# An external utility hardcodes the `/api/` prefix: mount its intake (routes/external_utility.py,
# the paths come from the provider's declaration) under /api too, so it works
# whether pointed at the backend port or through nginx.
app.include_router(auth_router,      prefix="/api/auth", tags=["auth"], include_in_schema=False)
app.include_router(external_utility_router, prefix="/api", tags=["external-utility"], include_in_schema=False)
app.include_router(delegation_webhook_router, prefix="/api", tags=["delegation-webhook"], include_in_schema=False)

_OPENAPI_YAML = Path(__file__).resolve().parent / "openapi.yaml"

@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml():
    """Serve the handcrafted OpenAPI spec (more detailed than the auto-generated one)."""
    return FileResponse(_OPENAPI_YAML, media_type="application/x-yaml")

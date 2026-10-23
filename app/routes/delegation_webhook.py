"""
Endpoint inbound generico per le notifiche push di download provider esterni
che delegano il download a un sistema terzo (es. un webhook del sistema esterno).

Provider-agnostico (Golden Rule): il nome del provider arriva come parametro
di PATH, dato applicativo, non un letterale hardcoded. Il modulo non conosce
il vocabolario di eventi né la forma del payload di alcun sistema specifico —
quella conoscenza vive dentro ``DownloadProvider.delegation_event_handle()``,
implementato da ciascun provider (vedi musizerr_sdk.interfaces). Converge sulla
stessa logica di riconciliazione del polling: un evento fa solo da trigger,
poi ``reconcile_delegation`` interroga il sistema esterno e aggiorna lo stato.

Sicurezza: token condiviso obbligatorio; il payload è trattato come DATO NON
FIDATO — ogni provider legge solo i propri campi strutturati attesi, mai
istruzioni.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import select

from core import download_states as ds
from db.models import Download
from routes.deps import AdminUserDep, ConfigDep, RegistryDep, SessionDep
from services.auth import auth_manager_for
from services.credentials import shared_store
from services.errors import public_error
from services.log_redaction import REDACTED, redact

logger = logging.getLogger(__name__)

router = APIRouter()

# Path pubblico dell'endpoint inbound (usato sia dalla route sotto, sia per
# costruire il callback_url passato a register_push_endpoint).
_INBOUND_PATH = "/delegation/webhook"


@router.post("/delegation/webhook/{provider_name}")
async def delegation_webhook(
    provider_name: str,
    request: Request,
    config: ConfigDep,
    session: SessionDep,
    registry: RegistryDep,
    token: str = "",
):
    expected = config.delegation_webhook_token
    if not config.delegation_webhook_enabled:
        raise HTTPException(status_code=404, detail="webhook disabled")
    # Confronto a tempo costante: l'endpoint è deliberatamente esposto su
    # internet (è il punto di public_base_url), quindi un confronto con `!=`
    # farebbe trapelare il prefisso del token via timing. `not expected` resta
    # un fail-closed esplicito (nessun token configurato → sempre rifiutato),
    # e str(token) garantisce che compare_digest non sollevi TypeError anche
    # se un input insolito arrivasse come non-str dal query param.
    if not expected or not secrets.compare_digest(str(token), expected):
        raise HTTPException(status_code=401, detail="invalid token")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — payload non fidato
        raise HTTPException(status_code=400, detail="invalid payload")

    provider = registry.get_download_provider(provider_name)
    if provider is None:
        return {"ok": True, "unknown_provider": provider_name}

    try:
        handle = provider.delegation_event_handle(payload)
    except Exception as exc:  # noqa: BLE001 — un provider (anche esterno) non deve poter far cadere l'endpoint
        logger.warning("delegation_event_handle('%s') fallito: %s", provider_name, exc)
        return {"ok": True, "ignored": True}

    if handle is None:
        return {"ok": True, "no_handle": True}

    rows = session.exec(
        select(Download).where(
            Download.delegation_handle == handle,
            Download.status.not_in(tuple(ds.TERMINAL)),  # type: ignore[attr-defined]
        )
    ).all()

    from workers.maintenance import reconcile_delegation
    triggered = 0
    for row in rows:
        reconcile_delegation.apply_async(kwargs={"download_id": row.id})
        triggered += 1

    logger.info("Delegation webhook '%s' handle=%s → %d riconciliazioni", provider_name, handle, triggered)
    return {"ok": True, "provider": provider_name, "triggered": triggered}


# What a provider's registration came to (the ``state`` of a result).
STATE_REGISTERED = "registered"
STATE_NOT_SUPPORTED = "not_supported"
STATE_FAILED = "failed"

# The provider implements ``register_push_endpoint`` and returned None: it did not say it registered anything,
# and it gave no reason. Not "not supported": it can push, it just did not confirm.
NO_OUTCOME_CODE = "registration_not_confirmed"
NO_OUTCOME_MESSAGE = "The provider did not confirm the registration and gave no reason."


def _scrubbed(text: str, token: str) -> str:
    """``text`` without the credentials it may quote: the callback URL carries the shared token of the inbound
    webhook, and a provider's (or its upstream's) message may repeat the URL."""
    if token:
        text = text.replace(token, REDACTED)
    return redact(text)


def _registration_result(provider, callback_url: str, token: str, auth) -> dict:
    """One provider's registration, as a result: registered, not supported, or failed with a classified
    ``error_code`` and the provider's own reason (``error_detail``, for the administrator who asked)."""
    entry = {"provider": provider.name, "label": provider.label}
    # Declared by the provider's manifest: "not supported" is only ever the provider saying it has no push
    # registration, never the host's reading of a failure or of an empty answer.
    if not provider.implements("register_push_endpoint"):
        return {**entry, "ok": False, "state": STATE_NOT_SUPPORTED, "detail": STATE_NOT_SUPPORTED}
    try:
        prov_session = auth.authenticate(provider)
        outcome = provider.register_push_endpoint(callback_url, prov_session)
    except Exception as exc:  # noqa: BLE001 — un provider non deve poter interrompere gli altri
        body = public_error(exc, "download_provider", show_detail=True)
        body["error_detail"] = _scrubbed(body["error_detail"], token)
        logger.warning(
            "register_push_endpoint('%s') failed (%s): %s", provider.name, body["error_code"], body["error_detail"],
        )
        return {**entry, "ok": False, "state": STATE_FAILED, "detail": STATE_FAILED, **body}
    if outcome is None:
        logger.warning("register_push_endpoint('%s') returned no outcome and no reason", provider.name)
        return {
            **entry, "ok": False, "state": STATE_FAILED, "detail": STATE_FAILED,
            "error_code": NO_OUTCOME_CODE, "error_message": NO_OUTCOME_MESSAGE,
        }
    return {**entry, "ok": True, "state": STATE_REGISTERED, "detail": _scrubbed(str(outcome), token)}


@router.post("/delegation/register-webhook")
def register_delegation_webhooks(config: ConfigDep, session: SessionDep, registry: RegistryDep, admin: AdminUserDep):  # noqa: ARG001
    """
    Registers musizerr's push callback on EVERY download provider, one result each: ``registered``,
    ``not_supported`` (the provider does not implement ``register_push_endpoint``) or ``failed`` with the
    classified ``error_code`` and the provider's own reason. Requires ``public_base_url``; generates a token
    when there is none. Admin-only.
    """
    base = config.public_base_url.rstrip("/")
    if not base:
        raise HTTPException(status_code=400, detail="public_base_url non configurato")

    token = config.delegation_webhook_token
    if not token:
        token = secrets.token_urlsafe(24)
        config.set("delegation_webhook_token", token)

    store = shared_store(session)
    auth = auth_manager_for(store)

    results = []
    for provider in registry.all_download():
        callback_url = f"{base}/api{_INBOUND_PATH}/{provider.name}?token={token}"
        results.append(_registration_result(provider, callback_url, token, auth))

    return {"results": results}


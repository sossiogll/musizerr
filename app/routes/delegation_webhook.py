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


@router.post("/delegation/register-webhook")
def register_delegation_webhooks(config: ConfigDep, session: SessionDep, registry: RegistryDep, admin: AdminUserDep):  # noqa: ARG001
    """
    Auto-registra il callback push di musizerr su OGNI download provider che
    supporta ``register_push_endpoint`` (le altre vengono saltate). Richiede
    ``public_base_url`` configurato; genera un token se assente. Admin-only.
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
        try:
            prov_session = auth.authenticate(provider)
            outcome = provider.register_push_endpoint(callback_url, prov_session)
        except Exception as exc:  # noqa: BLE001 — un provider non deve poter interrompere gli altri
            logger.warning("register_push_endpoint('%s') fallito: %s", provider.name, exc)
            results.append({"provider": provider.name, "label": provider.label, "ok": False, "detail": str(exc)})
            continue
        if outcome is None:
            results.append({"provider": provider.name, "label": provider.label, "ok": False, "detail": "not_supported"})
        else:
            results.append({"provider": provider.name, "label": provider.label, "ok": True, "detail": outcome})

    return {"results": results}

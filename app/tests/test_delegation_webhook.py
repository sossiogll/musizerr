"""
Copre la Fase 2b: la vecchia route hardcoded a un solo download provider
(`routes/lidarr_webhook.py`) e' stata sostituita da una capability opzionale
sul contratto (`DownloadProvider.delegation_event_handle` /
`register_push_endpoint`, in `musizeer_sdk.interfaces`) piu' una route generica
(`routes/delegation_webhook.py`) che itera sui provider registrati.

Regola d'oro rispettata: nessun provider reale qui. `FakeDownloadProvider`
(default del contratto, nessuna capability implementata) e
`FakeDelegatingDownloadProvider` (implementa le due capability con un
vocabolario di eventi inventato) vivono in conftest.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from services.config_manager import ConfigManager
from core import download_states as ds
from db.models import Download, TrackRecord
from routes.delegation_webhook import delegation_webhook, register_delegation_webhooks
from tests.database import new_engine
from tests.conftest import FakeDelegatingDownloadProvider, FakeDownloadProvider, FakeRegistry


class _FakeRequest:
    """Sostituto minimale di fastapi.Request: espone solo l'async .json()
    che la route usa davvero."""

    def __init__(self, payload=None, *, raises: bool = False):
        self._payload = payload
        self._raises = raises

    async def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def session():
    engine = new_engine()
    with Session(engine) as s:
        yield s


@pytest.fixture
def config():
    # Fase 4a: ConfigManager e' una facade sopra la tabella `settings` e, se
    # non gli si passa un engine esplicito, risolve quello di PRODUZIONE
    # (db.engine.engine) — necessario per i call-site reali (zero-arg), ma
    # qui servirebbe un engine dedicato e isolato: altrimenti questo test
    # leggerebbe/scriverebbe il DB reale del container. Un engine SQLite
    # in-memory throwaway per ogni istanza basta; i valori si scrivono
    # tramite l'API pubblica (`update`), non più via config.json (che non è
    # più letto a runtime).
    def _make(**overrides):
        data = {
            "delegation_webhook_enabled": True,
            "delegation_webhook_token": "s3cr3t-token",
            "public_base_url": "",
        }
        data.update(overrides)
        isolated_engine = new_engine()
        cm = ConfigManager(engine=isolated_engine)
        cm.update(data)
        return cm
    return _make


def _make_download(session, *, delegation_handle: str, status: str = "dispatched") -> Download:
    track = TrackRecord(title="T", artist="A", album="Al")
    session.add(track)
    session.commit()
    session.refresh(track)
    dl = Download(track_id=track.internal_id, status=status, delegation_handle=delegation_handle)
    session.add(dl)
    session.commit()
    session.refresh(dl)
    return dl


# ------------------------------------------------------------------
# Contract defaults (Golden Rule: optional method, never abstract)
# ------------------------------------------------------------------

def test_download_provider_default_has_no_push_capability():
    provider = FakeDownloadProvider("plain")
    assert provider.delegation_event_handle({"anything": "goes"}) is None
    assert provider.register_push_endpoint("https://example/cb", session=object()) is None


# ------------------------------------------------------------------
# Inbound webhook
# ------------------------------------------------------------------

def test_inbound_rejects_bad_token(config, session):
    cfg = config()
    registry = FakeRegistry(downloads=[FakeDelegatingDownloadProvider()])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(delegation_webhook(
            "fakedelegating", _FakeRequest({}), cfg, session, registry, token="wrong",
        ))
    assert exc.value.status_code == 401


def test_inbound_accepts_correct_token_rejects_wrong_token(config, session):
    # Pin sia l'esito (accettato/rifiutato) sia la difesa: un confronto in
    # tempo costante (secrets.compare_digest) al posto di `!=` su un segreto,
    # perche' l'endpoint e' deliberatamente esposto su internet e un
    # confronto short-circuit farebbe trapelare il prefisso del token via
    # timing. Non deve poter regredire silenziosamente a `!=`.
    cfg = config()
    registry = FakeRegistry(downloads=[FakeDelegatingDownloadProvider()])

    result = asyncio.run(delegation_webhook(
        "fakedelegating", _FakeRequest({"event": "irrelevant"}), cfg, session, registry,
        token="s3cr3t-token",
    ))
    assert result["ok"] is True

    with pytest.raises(HTTPException) as exc:
        asyncio.run(delegation_webhook(
            "fakedelegating", _FakeRequest({"event": "irrelevant"}), cfg, session, registry,
            token="s3cr3t-tokeX",  # un solo byte diverso, in coda
        ))
    assert exc.value.status_code == 401


def test_inbound_token_comparison_uses_compare_digest(config, session, monkeypatch):
    import secrets as secrets_module

    calls = []
    real_compare_digest = secrets_module.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr("routes.delegation_webhook.secrets.compare_digest", _spy)

    cfg = config()
    registry = FakeRegistry(downloads=[FakeDelegatingDownloadProvider()])
    asyncio.run(delegation_webhook(
        "fakedelegating", _FakeRequest({"event": "irrelevant"}), cfg, session, registry,
        token="s3cr3t-token",
    ))

    assert calls == [("s3cr3t-token", "s3cr3t-token")]


def test_inbound_rejects_when_disabled(config, session):
    cfg = config(delegation_webhook_enabled=False)
    registry = FakeRegistry(downloads=[FakeDelegatingDownloadProvider()])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(delegation_webhook(
            "fakedelegating", _FakeRequest({}), cfg, session, registry, token="s3cr3t-token",
        ))
    assert exc.value.status_code == 404


def test_inbound_rejects_unparseable_payload(config, session):
    cfg = config()
    registry = FakeRegistry(downloads=[FakeDelegatingDownloadProvider()])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(delegation_webhook(
            "fakedelegating", _FakeRequest(raises=True), cfg, session, registry, token="s3cr3t-token",
        ))
    assert exc.value.status_code == 400


def test_inbound_unknown_provider_is_benign(config, session):
    cfg = config()
    registry = FakeRegistry(downloads=[])
    result = asyncio.run(delegation_webhook(
        "ghost", _FakeRequest({"event": "done", "job_id": 1}), cfg, session, registry, token="s3cr3t-token",
    ))
    assert result["ok"] is True
    assert result["unknown_provider"] == "ghost"


def test_inbound_payload_with_no_handle_is_ignored(config, session):
    cfg = config()
    provider = FakeDelegatingDownloadProvider()
    registry = FakeRegistry(downloads=[provider])
    result = asyncio.run(delegation_webhook(
        "fakedelegating", _FakeRequest({"event": "irrelevant"}), cfg, session, registry, token="s3cr3t-token",
    ))
    assert result["ok"] is True
    assert result.get("no_handle") is True


def test_inbound_triggers_reconciliation_when_handle_matches(config, session, monkeypatch):
    cfg = config()
    provider = FakeDelegatingDownloadProvider()
    registry = FakeRegistry(downloads=[provider])
    dl = _make_download(session, delegation_handle="42", status=ds.DISPATCHED)
    # Un download terminale con lo stesso handle non deve essere ritoccato.
    _make_download(session, delegation_handle="42", status=ds.DOWNLOADED)

    triggered_ids = []

    class _FakeTask:
        def apply_async(self, kwargs):
            triggered_ids.append(kwargs["download_id"])

    monkeypatch.setattr("workers.maintenance.reconcile_delegation", _FakeTask())

    result = asyncio.run(delegation_webhook(
        "fakedelegating", _FakeRequest({"event": "done", "job_id": 42}), cfg, session, registry, token="s3cr3t-token",
    ))

    assert result["ok"] is True
    assert result["triggered"] == 1
    assert triggered_ids == [dl.id]


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

def test_register_requires_public_base_url(config, session):
    cfg = config(public_base_url="")
    registry = FakeRegistry(downloads=[FakeDelegatingDownloadProvider()])
    with pytest.raises(HTTPException) as exc:
        register_delegation_webhooks(cfg, session, registry, admin=object())
    assert exc.value.status_code == 400


def test_register_iterates_all_providers_with_per_provider_outcomes(config, session):
    cfg = config(public_base_url="http://musizeer.local")
    supporting = FakeDelegatingDownloadProvider("fakedelegating", register_outcome="created")
    non_supporting = FakeDownloadProvider("plain")
    registry = FakeRegistry(downloads=[supporting, non_supporting])

    data = register_delegation_webhooks(cfg, session, registry, admin=object())

    by_name = {r["provider"]: r for r in data["results"]}
    assert by_name["fakedelegating"]["ok"] is True
    assert by_name["fakedelegating"]["detail"] == "created"
    assert by_name["fakedelegating"]["label"] == supporting.label
    assert by_name["plain"]["ok"] is False
    assert by_name["plain"]["detail"] == "not_supported"

    # Il callback URL passato al provider porta il nome del provider stesso
    # (path param, non un letterale hardcoded) e il token condiviso.
    assert supporting.registered_urls == [
        f"http://musizeer.local/api/delegation/webhook/fakedelegating?token={cfg.delegation_webhook_token}"
    ]


def test_register_skips_provider_that_raises_without_aborting_loop(config, session):
    cfg = config(public_base_url="http://musizeer.local")

    class _BoomProvider(FakeDownloadProvider):
        def register_push_endpoint(self, callback_url, session):
            raise RuntimeError("external system unreachable")

    boom = _BoomProvider("boom")
    healthy = FakeDelegatingDownloadProvider("fakedelegating")
    registry = FakeRegistry(downloads=[boom, healthy])

    data = register_delegation_webhooks(cfg, session, registry, admin=object())

    by_name = {r["provider"]: r for r in data["results"]}
    assert by_name["boom"]["ok"] is False
    assert by_name["fakedelegating"]["ok"] is True


class _Upstream(Exception):
    """A failure of the external system the way an HTTP library shows it: a status on ``response``."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status, headers={})


class _RaisingProvider(FakeDownloadProvider):
    """Implements ``register_push_endpoint`` and fails with whatever it is given."""

    def __init__(self, name: str, error: Exception) -> None:
        super().__init__(name)
        self._error = error

    def register_push_endpoint(self, callback_url, session):
        raise self._error


def _register(config, session, *providers):
    cfg = config(public_base_url="http://musizeer.local")
    data = register_delegation_webhooks(cfg, session, FakeRegistry(downloads=list(providers)), admin=object())
    return cfg, {r["provider"]: r for r in data["results"]}


def test_a_registered_provider_is_state_registered(config, session):
    _, results = _register(config, session, FakeDelegatingDownloadProvider("fakedelegating"))

    assert results["fakedelegating"]["state"] == "registered"
    assert results["fakedelegating"]["ok"] is True
    assert "error_code" not in results["fakedelegating"]


def test_not_supported_is_only_a_provider_that_does_not_implement_the_method(config, session):
    _, results = _register(config, session, FakeDownloadProvider("plain"))

    assert results["plain"]["state"] == "not_supported"
    assert results["plain"]["detail"] == "not_supported"
    assert results["plain"]["ok"] is False
    assert "error_code" not in results["plain"]


def test_a_provider_that_implements_it_and_answers_nothing_is_not_reported_as_not_supported(config, session):
    """The old behaviour of a provider that swallowed a refusal as ``None``: the admin must not read "not supported"."""
    silent = FakeDelegatingDownloadProvider("silent", register_outcome=None)
    _, results = _register(config, session, silent)

    result = results["silent"]
    assert result["state"] == "failed"
    assert result["detail"] != "not_supported"
    assert result["error_code"] == "registration_not_confirmed"
    assert silent.registered_urls  # it was called: it implements the method


@pytest.mark.parametrize("status, code", [
    (400, "download_provider_failed"),
    (401, "download_provider_auth_failed"),
    (404, "download_provider_not_found"),
    (500, "download_provider_upstream_error"),
])
def test_a_refusal_of_the_external_system_is_a_failure_with_its_classified_code_and_reason(config, session, status, code):
    refused = _RaisingProvider("refusing", _Upstream("Url: Unable to send test message", status))
    _, results = _register(config, session, refused)

    result = results["refusing"]
    assert result["state"] == "failed" and result["ok"] is False
    assert result["error_code"] == code
    assert result["error_message"].startswith("The download provider")
    assert result["error_detail"] == "Url: Unable to send test message"


def test_an_external_system_that_cannot_be_reached_is_unreachable(config, session):
    down = _RaisingProvider("down", ConnectionError("[Errno -2] Name or service not known"))
    _, results = _register(config, session, down)

    assert results["down"]["state"] == "failed"
    assert results["down"]["error_code"] == "download_provider_unreachable"
    assert "Name or service not known" in results["down"]["error_detail"]


def test_one_failing_provider_does_not_hide_the_others(config, session):
    _, results = _register(
        config, session,
        _RaisingProvider("refusing", _Upstream("no", 400)),
        FakeDelegatingDownloadProvider("fakedelegating"),
        FakeDownloadProvider("plain"),
    )

    assert {name: r["state"] for name, r in results.items()} == {
        "refusing": "failed", "fakedelegating": "registered", "plain": "not_supported",
    }


def test_the_callback_token_is_never_returned_even_when_the_provider_quotes_the_url(config, session):
    cfg = config(public_base_url="http://musizeer.local")
    token = cfg.delegation_webhook_token

    class _Quoting(FakeDownloadProvider):
        def register_push_endpoint(self, callback_url, session):
            raise _Upstream(f"cannot reach {callback_url} (token {token} refused)", 400)

    data = register_delegation_webhooks(cfg, session, FakeRegistry(downloads=[_Quoting("quoting")]), admin=object())

    detail = data["results"][0]["error_detail"]
    assert token not in detail
    assert "token=***" in detail
    assert token not in str(data)


def test_register_generates_and_persists_token_when_absent(config, session):
    cfg = config(public_base_url="http://musizeer.local", delegation_webhook_token="")
    registry = FakeRegistry(downloads=[])

    data = register_delegation_webhooks(cfg, session, registry, admin=object())

    assert data["results"] == []
    assert cfg.delegation_webhook_token != ""


def test_router_is_mounted_both_with_and_without_the_api_prefix():
    """Regressione reale: il proxy del frontend (vite in dev, nginx in prod)
    STRIPPA il prefisso /api prima di inoltrare, quindi una chiamata del browser
    a /api/delegation/... arriva al backend come /delegation/... . Il sistema
    esterno invece chiama public_base_url, che punta al backend diretto e
    mantiene /api. Montare un solo prefisso rende l'endpoint irraggiungibile da
    uno dei due lati, in silenzio: e' esattamente il bug che ha fatto fallire il
    pulsante di registrazione. Entrambi i mount devono esistere.
    """
    import main

    from tests.route_support import all_routes

    paths = {route.path for route in all_routes()}
    for suffix in ("/delegation/register-webhook", "/delegation/webhook/{provider_name}"):
        assert suffix in paths, f"manca il mount senza prefisso per {suffix}"
        assert f"/api{suffix}" in paths, f"manca il mount /api per {suffix}"

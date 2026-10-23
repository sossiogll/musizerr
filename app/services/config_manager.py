from __future__ import annotations
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.settings_catalog import catalog_defaults, fail_safe_defaults

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Unica sorgente dei default: il catalogo dichiarativo (nessun magic number qui).
_DEFAULTS: dict[str, Any] = catalog_defaults()

# Valori serviti mentre lo store non è MAI stato letto con successo (vedi
# SettingDef.fail_safe) — solo le chiavi che lo dichiarano.
_FAIL_SAFE: dict[str, Any] = fail_safe_defaults()

# Finestra di cache: il valore letto dalla tabella `settings` resta in memoria
# per questo tanto prima di essere ri-letto dal DB. Serve a non interrogare il
# DB a ogni singolo accesso a una property — ConfigManager viene letto anche
# nelle hot path di ricerca — pur restando abbastanza fresco da vedere entro
# pochi secondi una modifica fatta dall'altro processo (app vs worker).
_CACHE_TTL_SECONDS = 5.0


class ConfigManager:
    """
    Facade sopra ``SettingsRepository`` (tabella ``settings``, scope
    'system') per le impostazioni d'app: ``get``/``set``/``update``/``all``
    e una property per impostazione, usata da worker, dependency layer e hot
    path di ricerca.

    Una chiave assente dal DB serve il default dichiarato nel catalogo (o il
    ``fail_safe``, finché lo store non è mai stato caricato) — vedi
    ``_refresh_cache_now`` per cosa succede se il DB è irraggiungibile.
    """

    def __init__(self, engine: "Engine | None" = None) -> None:
        # Iniettabile SOLO per i test (che vogliono condividere lo stesso DB
        # in-memory di SettingsService/SettingsRepository): nessun call-site
        # di produzione lo passa, quindi resta lazy-risolto sull'engine
        # condiviso di db.engine — comportamento identico a `ConfigManager()`
        # di prima per ogni chiamante esistente.
        self._engine = engine
        self._cache: dict[str, Any] = {}
        self._cache_loaded_at: float = 0.0
        # True dal primo caricamento RIUSCITO dello store da parte di QUESTA
        # istanza (vedi _refresh_cache_now). Una scrittura riuscita (_write_system)
        # NON lo mette a True: prova che il DB è raggiungibile, ma non carica
        # le altre chiavi — un fail_safe esisterebbe apposta per il caso in cui
        # quelle altre chiavi non sono mai state lette, e una scrittura isolata
        # non lo cambia.
        self._loaded = False

    # ------------------------------------------------------------------
    # Accesso ai valori
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        self._maybe_refresh_cache()
        if key in self._cache:
            return self._cache[key]
        if not self._loaded and key in _FAIL_SAFE:
            return _FAIL_SAFE[key]
        return _DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._write_system(key, value)

    def update(self, updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            self._write_system(key, value)

    def committed(self, updates: dict[str, Any]) -> None:
        """``updates`` were written (and committed) by the caller in a transaction of its own
        (``SettingsRepository.set_system(commit=False)``): this instance sees them at once."""
        self._cache.update(updates)
        self._cache_loaded_at = time.monotonic()

    def all(self) -> dict[str, Any]:
        self._maybe_refresh_cache()
        if not self._loaded:
            return {**_DEFAULTS, **_FAIL_SAFE, **self._cache}
        return {**_DEFAULTS, **self._cache}

    # ------------------------------------------------------------------
    # Shortcut per le proprietà più usate
    # ------------------------------------------------------------------

    @property
    def workspace_dir(self) -> Path:
        return Path(self.get("workspace_dir"))

    @property
    def max_concurrent_downloads(self) -> int:
        return int(self.get("max_concurrent_downloads"))

    @property
    def max_retries(self) -> int:
        return int(self.get("max_retries"))

    @property
    def retry_delay_seconds(self) -> int:
        return int(self.get("retry_delay_seconds"))

    @property
    def retry_max_delay_seconds(self) -> int:
        return int(self.get("retry_max_delay_seconds"))

    @property
    def provider_cooldown_errors(self) -> int:
        return int(self.get("provider_cooldown_errors"))

    @property
    def provider_cooldown_seconds(self) -> int:
        return int(self.get("provider_cooldown_seconds"))

    @property
    def metadata_provider_priority(self) -> list[str]:
        return self.get("metadata_provider_priority")

    @property
    def download_provider_order(self) -> list[str]:
        return self.get("download_provider_order")

    @property
    def track_format(self) -> str:
        return self.get("track_format")

    @property
    def save_cover(self) -> bool:
        return bool(self.get("save_cover"))

    @property
    def storage_provider(self) -> str | None:
        return self.get("storage_provider")

    @property
    def sync_provider(self) -> str | None:
        return self.get("sync_provider")

    @property
    def http_timeout(self) -> int:
        return int(self.get("http_timeout"))

    @property
    def download_timeout(self) -> int:
        return int(self.get("download_timeout"))

    @property
    def connect_timeout(self) -> int:
        return int(self.get("connect_timeout"))

    @property
    def stream_start_timeout(self) -> int:
        return int(self.get("stream_start_timeout"))

    @property
    def storage_stream_cooldown_seconds(self) -> int:
        return int(self.get("storage_stream_cooldown_seconds"))

    @property
    def held_upload_max_age_hours(self) -> int:
        return int(self.get("held_upload_max_age_hours"))

    @property
    def token_expiry_buffer(self) -> int:
        return int(self.get("token_expiry_buffer"))

    @property
    def auth_enabled(self) -> bool:
        return bool(self.get("auth_enabled"))

    @property
    def registration_open(self) -> bool:
        """The ``registration_open`` setting as it was set. Whether a visitor can register right
        now is ``registration_available``."""
        return bool(self.get("registration_open"))

    @property
    def registration_available(self) -> bool:
        """A visitor can create an account: authentication is on AND registration is open. With
        authentication off there are no accounts to create (the instance has one owner), whatever
        the setting says."""
        return self.auth_enabled and self.registration_open

    @property
    def provider_login_new_accounts(self) -> str:
        """What a first sign-in through a provider does (``core.provider_login``)."""
        return str(self.get("provider_login_new_accounts"))

    @property
    def playlist_max_tracks(self) -> int:
        """The most tracks a playlist may hold (``services.playlists``)."""
        return int(self.get("playlist_max_tracks"))

    @property
    def full_sync_probe_timeout_seconds(self) -> int:
        return int(self.get("full_sync_probe_timeout_seconds"))

    @property
    def full_sync_probe_interval_seconds(self) -> int:
        return int(self.get("full_sync_probe_interval_seconds"))

    # ----- Download time-limits (worker) -----

    @property
    def download_soft_time_limit_seconds(self) -> int:
        return int(self.get("download_soft_time_limit_seconds"))

    @property
    def download_hard_time_limit_seconds(self) -> int:
        return int(self.get("download_hard_time_limit_seconds"))

    @property
    def provider_slot_wait_seconds(self) -> int:
        return int(self.get("provider_slot_wait_seconds"))

    # ----- Integrity -----

    @property
    def verify_integrity_enabled(self) -> bool:
        return bool(self.get("verify_integrity_enabled"))

    @property
    def verify_duration_tolerance_seconds(self) -> int:
        return int(self.get("verify_duration_tolerance_seconds"))

    @property
    def verify_after_upload(self) -> str:
        return str(self.get("verify_after_upload"))

    # ----- Reconciliation -----

    @property
    def delegation_webhook_enabled(self) -> bool:
        return bool(self.get("delegation_webhook_enabled"))

    @property
    def delegation_poll_interval_seconds(self) -> int:
        return int(self.get("delegation_poll_interval_seconds"))

    @property
    def delegation_max_age_hours(self) -> int:
        return int(self.get("delegation_max_age_hours"))

    @property
    def delegation_max_wait_hours(self) -> int:
        return int(self.get("delegation_max_wait_hours"))

    @property
    def delegation_webhook_token(self) -> str:
        return str(self.get("delegation_webhook_token") or "")

    @property
    def public_base_url(self) -> str:
        return str(self.get("public_base_url") or "")

    # ----- Identification -----

    @property
    def identification_enabled(self) -> bool:
        return bool(self.get("identification_enabled"))

    @property
    def identification_confidence_threshold(self) -> float:
        # Parametro GENERICO (soglia di confidenza), non legato a un provider.
        # L'API key / rate-limit specifici vivono nella config del provider.
        return float(self.get("identification_confidence_threshold"))

    # ----- Ricerca -----
    @property
    def search_default_limit(self) -> int:
        return int(self.get("search_default_limit"))

    @property
    def search_provider_budget_seconds(self) -> float:
        return float(self.get("search_provider_budget_seconds"))

    @property
    def enrichment_budget_seconds(self) -> float:
        return float(self.get("enrichment_budget_seconds"))

    @property
    def search_stream_enrich_workers(self) -> int:
        return int(self.get("search_stream_enrich_workers"))

    @property
    def sync_availability_cache_ttl_seconds(self) -> float:
        return float(self.get("sync_availability_cache_ttl_seconds"))

    @property
    def anti_mislabel_enabled(self) -> bool:
        return bool(self.get("anti_mislabel_enabled"))

    # ----- Dedup -----

    @property
    def dedup_enabled(self) -> bool:
        return bool(self.get("dedup_enabled"))

    @property
    def dedup_prefer_lossless(self) -> bool:
        return bool(self.get("dedup_prefer_lossless"))

    # ------------------------------------------------------------------
    # I/O — la tabella `settings` è l'UNICA fonte di verità a runtime
    # ------------------------------------------------------------------

    def _resolved_engine(self) -> "Engine":
        # ATTENZIONE (test): senza `engine=` esplicito questo risolve
        # sull'engine di PRODUZIONE condiviso (db.engine.engine — lo stesso
        # database del container reale). Necessario perché
        # ogni call-site di produzione costruisce `ConfigManager()` senza
        # argomenti; un test che fa lo stesso senza passare un engine di test
        # dedicato leggerebbe/scriverebbe silenziosamente il DB reale invece
        # di uno isolato — vedi la fixture `config` di test_delegation_webhook.py
        # per il pattern corretto (un database dedicato, tests/database.py).
        if self._engine is not None:
            return self._engine
        from db.engine import engine as _prod_engine

        return _prod_engine

    def _maybe_refresh_cache(self) -> None:
        if time.monotonic() - self._cache_loaded_at < _CACHE_TTL_SECONDS:
            return
        self._refresh_cache_now()

    def _refresh_cache_now(self) -> None:
        try:
            from sqlmodel import Session

            from db.settings_store import SettingsRepository

            with Session(self._resolved_engine()) as session:
                self._cache = SettingsRepository(session).all_system()
            self._loaded = True
        except Exception as exc:
            # Tabella assente/DB irraggiungibile (es. avvio prima che la
            # migrazione sia stata applicata): degrada ai default dichiarati
            # nel catalogo (o al fail_safe, per le chiavi che lo dichiarano,
            # finché questa istanza non ha MAI completato un caricamento —
            # vedi self._loaded/get()), mai un raise (ConfigManager viene
            # costruito all'avvio dell'app e dal worker) e mai un file — un
            # fallback su config.json servirebbe silenziosamente un valore
            # stantio, dato che nulla lo scrive più. Aggiorna comunque il
            # timestamp per non ribattere sul DB a ogni singola property finché
            # non torna raggiungibile (stesso TTL come backoff).
            if self._loaded:
                logger.warning("SettingsRepository non raggiungibile, mantengo la cache precedente: %s", exc)
            else:
                logger.warning(
                    "SettingsRepository non raggiungibile, store mai caricato: "
                    "in vigore i valori fail-safe (dove dichiarati) e i default del catalogo: %s",
                    exc,
                )
        finally:
            self._cache_loaded_at = time.monotonic()

    def _write_system(self, key: str, value: Any) -> None:
        from sqlmodel import Session

        from db.settings_store import SettingsRepository

        with Session(self._resolved_engine()) as session:
            SettingsRepository(session).set_system(key, value)
        # Invalidazione esplicita: il prossimo `get` su QUESTA istanza vede
        # subito il nuovo valore senza aspettare la finestra di cache —
        # stesso ruolo che il controllo su mtime svolgeva per le scritture
        # fatte attraverso questa stessa istanza.
        self._cache[key] = value
        self._cache_loaded_at = time.monotonic()


# Istanza condivisa (lazy) del ConfigManager, riusabile dal percorso caldo
# (ricerca/enrichment) senza ri-interrogare il DB a ogni chiamata (vedi
# _CACHE_TTL_SECONDS). deps/celery mantengono i loro accessor init-guardati.
_shared_config: "ConfigManager | None" = None


def get_config() -> "ConfigManager":
    global _shared_config
    if _shared_config is None:
        _shared_config = ConfigManager()
    return _shared_config

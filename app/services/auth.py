"""
Costruzione di ``core.auth_manager.AuthManager`` con le impostazioni
correnti dell'host: il solo punto in cui routes/ e workers/ ne creano uno.
"""
from __future__ import annotations

from core.auth_manager import AuthManager
from services.config_manager import get_config


def auth_manager_for(credential_store) -> AuthManager:
    """AuthManager sul ``credential_store`` dato, con la politica di scadenza
    dei token letta ora dalla configurazione."""
    return AuthManager(credential_store, token_expiry_buffer=get_config().token_expiry_buffer)

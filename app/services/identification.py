"""
Applica la policy di confidenza sull'identità restituita da un provider di
identificazione: la soglia (``config.identification_confidence_threshold``,
generica e dichiarata nel catalogo core) è una decisione dell'host, mai del
provider — ``IdentificationProvider.identify()`` restituisce sempre il
miglior match col proprio score grezzo, non filtrato.
"""
from __future__ import annotations

from services.config_manager import ConfigManager
from musizerr_sdk.interfaces import Identity, IdentificationProvider, WorkspaceFile


def identify_confidently(
    provider: IdentificationProvider, audio: WorkspaceFile, config: ConfigManager,
) -> Identity | None:
    """Identifica ``audio`` con ``provider`` e applica la soglia di
    confidenza generica (``confident``)."""
    return confident(provider.identify(audio), config)


def confident(identity: Identity | None, config: ConfigManager) -> Identity | None:
    """``identity`` se il suo score raggiunge
    ``config.identification_confidence_threshold``, altrimenti None."""
    if identity is None or identity.score < config.identification_confidence_threshold:
        return None
    return identity

"""
`sync_id_type()` deriva l'id_type di `track_identifiers` dal nome del sync
provider attivo invece di un letterale hardcoded (Golden Rule).

Regressione critica: le righe reali di `track_identifiers` hanno
`id_type == "<nome del sync provider>_sync_id"`, prodotte quando il sync
provider attivo aveva quel nome. La derivazione per un nome deve restare
byte-identica al vecchio letterale, altrimenti quelle righe restano orfane
senza alcuna migrazione dati a segnalarlo.
"""

from __future__ import annotations

from core.identifiers import sync_id_type


def test_the_derivation_is_the_legacy_literal_of_the_name():
    # Letterale intenzionale: è la stringa già presente sulle righe del DB di
    # produzione ("<nome>_sync_id"), non un nome di provider hardcoded nella logica.
    assert sync_id_type("mediaserver") == "mediaserver_sync_id"


def test_derivation_follows_provider_name_generically():
    assert sync_id_type("othersync") == "othersync_sync_id"
    assert sync_id_type("fake") == "fake_sync_id"

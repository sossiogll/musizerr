"""Vocabolario stati download: invarianti strutturali."""

from __future__ import annotations

from core import download_states as ds


def test_order_covers_every_state_exactly_once():
    all_states = {
        ds.PENDING, ds.DOWNLOADING, ds.IMPORTING, ds.CONFLICT,
        ds.RETRYING, ds.DISPATCHED, ds.DOWNLOADED, ds.FAILED,
    }
    assert set(ds.ORDER) == all_states
    assert len(ds.ORDER) == len(all_states)  # nessun duplicato


def test_terminal_states():
    assert ds.DOWNLOADED in ds.TERMINAL
    assert ds.FAILED in ds.TERMINAL
    # gli stati in volo non sono terminali
    assert ds.TERMINAL.isdisjoint(ds.IN_FLIGHT)


def test_in_flight_states_are_worker_owned():
    assert ds.DOWNLOADING in ds.IN_FLIGHT
    assert ds.IMPORTING in ds.IN_FLIGHT
    assert ds.PENDING not in ds.IN_FLIGHT  # in coda, non in volo


def test_downloaded_is_most_advanced_in_order():
    # L'aggregazione multi-tentativo prende "il più avanti": DOWNLOADED per primo.
    assert ds.ORDER[0] == ds.DOWNLOADED


def test_public_states_shape_matches_order_and_labels():
    public = ds.public_states()
    assert [s["key"] for s in public] == list(ds.ORDER)
    for entry in public:
        assert entry["key"] in ds.LABELS
        assert "label" in entry and "tone" in entry
        assert entry["tone"] in {"neutral", "info", "warning", "success", "danger"}
        assert entry["phase"] in ds.PHASES
        assert entry["label_key"] == f"download_states.{entry['key']}"


def test_phases_group_what_the_user_can_do():
    phase = {s["key"]: s["phase"] for s in ds.public_states()}
    assert phase[ds.DOWNLOADED] == "done"
    assert phase[ds.FAILED] == "failed"
    assert {phase[s] for s in ds.IN_FLIGHT} == {"active"}
    assert phase[ds.PENDING] == "queued"


def test_every_state_has_a_label():
    for state in ds.ORDER:
        assert state in ds.LABELS

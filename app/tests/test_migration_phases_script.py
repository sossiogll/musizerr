"""
``scripts/migration_phases.py``: what a release does to the database, for its ``release.json`` (ADR-0007 §4,
§5.1). It reads the migrations' source and the standard library only, so the release workflow runs it on a
bare runner.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import schema_guard
from scripts import migration_phases

APP = Path(__file__).resolve().parent.parent


def test_the_source_of_every_migration_declares_a_valid_phase():
    found = migration_phases.migrations_here()

    assert len(found) >= 44
    assert {name: m["phase"] for name, m in found.items() if m["phase"] not in migration_phases.PHASES} == {}
    assert all(m["revision"] for m in found.values())


def test_the_whole_history_has_contract_migrations_and_the_flagged_irreversible_ones():
    report = migration_phases.report()

    assert report["contract_migrations"] is True
    assert "0041_encrypt_credentials" in report["contract"]
    assert "0043_schema_info" in report["expand"]
    assert "0042_open_mode_accounts" in report["irreversible"]
    assert report["min_upgrade_from"] == schema_guard.MIN_UPGRADE_FROM
    assert len(report["contract"]) + len(report["expand"]) == len(migration_phases.migrations_here())


def _history(*phases: tuple[str, str, bool]) -> dict:
    return {
        f"{n:04d}_{name}.py": {"revision": f"{n:04d}_{name}", "phase": phase, "irreversible": irreversible}
        for n, (name, phase, irreversible) in enumerate(phases, start=1)
    }


def test_a_release_that_adds_only_expand_migrations_has_no_contract_migrations(monkeypatch):
    found = _history(("a", "expand", False), ("b", "contract", True), ("c", "expand", False), ("d", "expand", False))
    monkeypatch.setattr(migration_phases, "migrations_here", lambda: found)
    monkeypatch.setattr(migration_phases, "names_at", lambda ref: {"0001_a.py", "0002_b.py"})

    report = migration_phases.report("v1.4.0")

    assert report["contract_migrations"] is False
    assert (report["contract"], report["expand"], report["irreversible"]) == ([], ["0003_c", "0004_d"], [])


def test_a_release_that_adds_a_contract_migration_says_so(monkeypatch):
    found = _history(("a", "expand", False), ("b", "contract", True), ("c", "expand", False))
    monkeypatch.setattr(migration_phases, "migrations_here", lambda: found)
    monkeypatch.setattr(migration_phases, "names_at", lambda ref: {"0001_a.py"})

    report = migration_phases.report("v1.4.0")

    assert report["contract_migrations"] is True
    assert (report["contract"], report["expand"], report["irreversible"]) == (["0002_b"], ["0003_c"], ["0002_b"])


def test_without_a_previous_release_every_migration_is_new(monkeypatch):
    found = _history(("a", "expand", False), ("b", "contract", False))
    monkeypatch.setattr(migration_phases, "migrations_here", lambda: found)

    assert migration_phases.report()["contract_migrations"] is True


def test_a_migration_without_a_phase_stops_the_release(monkeypatch):
    found = migration_phases.migrations_here()
    found["0099_oops.py"] = {"revision": "0099_oops", "phase": None, "irreversible": False}
    monkeypatch.setattr(migration_phases, "migrations_here", lambda: found)

    with pytest.raises(SystemExit, match="0099_oops.py"):
        migration_phases.report()


def test_the_declared_values_are_read_from_the_source_without_importing_it():
    source = 'revision = "0099_x"\nphase = "contract"\nirreversible = True\nraise RuntimeError("never imported")\n'

    assert migration_phases.declared(source) == {"revision": "0099_x", "phase": "contract", "irreversible": True}
    assert migration_phases.declared("x = 1") == {"revision": None, "phase": None, "irreversible": False}


def test_the_minimum_upgrade_version_is_read_from_the_rules_module(monkeypatch, tmp_path):
    assert migration_phases.min_upgrade_from() == schema_guard.MIN_UPGRADE_FROM
    guard = tmp_path / "schema_guard.py"
    guard.write_text('MIN_UPGRADE_FROM: str | None = "1.9.0"\n')
    monkeypatch.setattr(migration_phases, "GUARD", guard)

    assert migration_phases.min_upgrade_from() == "1.9.0"
    guard.write_text("MIN_UPGRADE_FROM: str | None = None\n")
    assert migration_phases.min_upgrade_from() is None


def test_the_script_runs_alone_with_the_standard_library_and_prints_json():
    done = subprocess.run(
        [sys.executable, "-I", str(APP / "scripts" / "migration_phases.py")],
        capture_output=True, text=True, cwd=APP, check=True,
    )

    assert set(json.loads(done.stdout)) == {
        "contract_migrations", "contract", "expand", "irreversible", "min_upgrade_from",
    }

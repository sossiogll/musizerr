"""
The pure rules of the database guard (``core/schema_guard.py``, ADR-0007 §4): which schema a version can use, the
upgrade path, what a contract migration raises, and how the backups before a migration are named, pruned and found.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import schema_guard as guard
from core.schema_guard import SchemaRecord

OLD = "0042_open_mode_accounts"
HEAD = "0043_schema_info"
KNOWN = {OLD, HEAD}


def record(**changes) -> SchemaRecord:
    base = dict(revision=HEAD, app_version="1.5.0", min_app_version=None, contract_revision=OLD, phase="expand")
    return SchemaRecord(**{**base, **changes})


def judge(applied, rec, version="1.5.0", **kwargs):
    return guard.judge(applied, KNOWN, {HEAD}, rec, version, **kwargs)


# ---------------------------------------------------------------------------
# The verdict on a schema
# ---------------------------------------------------------------------------


def test_a_database_at_the_head_is_current():
    assert judge({HEAD}, record()).status == guard.CURRENT


def test_a_database_behind_the_code_is_pending_and_the_message_names_init():
    verdict = judge({OLD}, None)

    assert verdict.status == guard.PENDING
    assert OLD in verdict.message and HEAD in verdict.message and "init" in verdict.message


def test_a_database_that_was_never_migrated_is_pending():
    verdict = judge(set(), None)

    assert verdict.status == guard.PENDING
    assert guard.NO_REVISION in verdict.message


def test_a_database_ahead_by_expand_migrations_only_is_the_allowed_rollback():
    verdict = judge({"0050_a_later_expand"}, record(revision="0050_a_later_expand"), version="1.4.0")

    assert verdict.status == guard.ROLLBACK
    assert verdict.unknown == ("0050_a_later_expand",)
    assert "expand-only" in verdict.message and "nothing is migrated" in verdict.message


def test_a_database_ahead_by_a_contract_migration_this_code_lacks_is_refused():
    verdict = judge(
        {"0100_drop_something"},
        record(revision="0100_drop_something", contract_revision="0100_drop_something", min_app_version="2.0.0",
               app_version="2.0.0", phase="contract"),
        version="1.9.0",
    )

    assert verdict.status == guard.TOO_NEW
    assert "0100_drop_something" in verdict.message
    assert "Musizeer 2.0.0 or later" in verdict.message
    assert "1.9.0" in verdict.message


def test_a_database_ahead_with_no_schema_info_is_refused_it_cannot_be_told_safe():
    verdict = judge({"0050_unknown"}, None)

    assert verdict.status == guard.TOO_NEW
    assert "schema_info" in verdict.message


def test_the_message_of_a_refusal_names_the_backup_to_restore():
    older = guard.backup_name(datetime(2026, 10, 1, tzinfo=timezone.utc), OLD, HEAD, "dump")
    newer = guard.backup_name(datetime(2026, 10, 2, tzinfo=timezone.utc), HEAD, "0100_drop_something", "dump")

    verdict = judge(
        {"0100_drop_something"},
        record(revision="0100_drop_something", contract_revision="0100_drop_something", min_app_version="2.0.0"),
        version="1.9.0", backups=[older, newer],
    )

    assert verdict.status == guard.TOO_NEW
    # The newest backup whose source schema this code has every revision of: both qualify, the newer wins.
    assert newer in verdict.message


def test_a_version_below_the_minimum_the_database_records_is_refused_even_on_known_revisions():
    verdict = judge({HEAD}, record(min_app_version="2.0.0", contract_revision=HEAD), version="1.9.3+7.abc1234")

    assert verdict.status == guard.TOO_NEW


@pytest.mark.parametrize("version", ["2.0.0", "2.3.1", "dev"])
def test_a_version_at_or_above_the_minimum_or_with_no_version_is_not_refused_by_it(version):
    verdict = judge({HEAD}, record(min_app_version="2.0.0"), version=version)

    assert verdict.status == guard.CURRENT


def test_a_development_build_is_judged_on_revisions_alone():
    refused = judge({"0100_x"}, record(contract_revision="0100_x", min_app_version="2.0.0"), version="dev")
    allowed = judge({"0050_x"}, record(contract_revision=OLD, min_app_version="2.0.0"), version="dev")

    assert refused.status == guard.TOO_NEW  # the contract revision is not in its code
    assert allowed.status == guard.ROLLBACK  # a version it cannot compare does not refuse


# ---------------------------------------------------------------------------
# The upgrade path
# ---------------------------------------------------------------------------


def test_no_minimum_means_any_database_may_be_migrated(monkeypatch):
    monkeypatch.setattr(guard, "MIN_UPGRADE_FROM", None)

    assert guard.upgrade_path_problem(record(app_version="0.1.0"), "9.0.0") is None
    assert guard.upgrade_path_problem(None, "9.0.0") is None


def test_the_minimum_of_the_code_is_the_default(monkeypatch):
    monkeypatch.setattr(guard, "MIN_UPGRADE_FROM", "1.9.0")

    assert guard.upgrade_path_problem(record(app_version="1.4.2"), "2.0.0") is not None
    assert guard.upgrade_path_problem(record(app_version="1.9.0"), "2.0.0") is None


@pytest.mark.parametrize("last", ["1.9.0", "1.9.4", "2.0.0", "dev"])
def test_a_database_migrated_by_the_minimum_or_later_may_be_migrated(last):
    assert guard.upgrade_path_problem(record(app_version=last), "2.0.0", "1.9.0") is None


def test_a_database_migrated_by_an_older_version_must_go_through_the_minimum_first():
    problem = guard.upgrade_path_problem(record(app_version="1.4.2"), "2.0.0", "1.9.0")

    assert problem is not None
    assert "1.9.0" in problem and "1.4.2" in problem and "MUSIZEER_VERSION=1.9.0" in problem


def test_a_database_from_before_the_guard_is_older_than_any_minimum():
    problem = guard.upgrade_path_problem(None, "2.0.0", "1.9.0")

    assert problem is not None and "older than the database guard" in problem


# ---------------------------------------------------------------------------
# What a run records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "contract", "version", "expected"),
    [
        (None, False, "1.5.0", None),                  # expand only: nothing to raise
        ("2.0.0", False, "2.3.0", "2.0.0"),            # kept
        (None, True, "2.0.0", "2.0.0"),                # a contract migration of the 2.x line
        (None, True, "2.3.1+4.abc1234", "2.0.0"),      # build metadata ranks as its release
        ("2.0.0", True, "3.0.0", "3.0.0"),             # raised
        ("3.0.0", True, "2.7.0", "3.0.0"),             # never lowered
        ("2.0.0", True, "dev", "2.0.0"),               # a development build cannot raise it
        (None, True, "dev", None),
    ],
)
def test_the_minimum_version_is_raised_by_a_contract_migration_and_never_lowered(previous, contract, version, expected):
    assert guard.min_app_version_after(previous, contract, version) == expected


# ---------------------------------------------------------------------------
# The backups
# ---------------------------------------------------------------------------


def _name(day: int, source=OLD, target=HEAD, extension="dump", hour=10) -> str:
    return guard.backup_name(datetime(2026, 10, day, hour, 15, 0, tzinfo=timezone.utc), source, target, extension)


def test_a_backup_name_carries_the_time_and_the_revisions_from_and_to():
    name = _name(24)

    assert name == "premigration-20261024T101500Z-from-0042_open_mode_accounts-to-0043_schema_info.dump"
    parsed = guard.parse_backup_name(name)
    assert (parsed.stamp, parsed.source, parsed.target, parsed.extension) == (
        "20261024T101500Z", OLD, HEAD, "dump",
    )


def test_several_revisions_and_an_odd_character_make_a_safe_name():
    name = guard.backup_name(datetime(2026, 10, 24, tzinfo=timezone.utc), "a+b", "c/d e", "sqlite3")

    assert name.endswith("-from-a+b-to-c_d_e.sqlite3")
    assert guard.parse_backup_name(name) is not None


@pytest.mark.parametrize("name", ["notes.txt", "premigration-x.dump", "musizeer.db", _name(1) + ".partial"])
def test_a_file_that_is_not_a_backup_of_ours_does_not_parse(name):
    assert guard.parse_backup_name(name) is None


def test_the_last_n_backups_are_kept_by_time_not_by_directory_order():
    names = [_name(day) for day in (9, 3, 7, 1, 5, 2, 8)]  # in no particular order

    assert sorted(guard.backups_to_prune(names, 5)) == sorted([_name(1), _name(2)])
    assert guard.backups_to_prune(names, 7) == []
    assert guard.backups_to_prune(names, 100) == []


def test_pruning_never_removes_the_last_backup_and_never_a_foreign_file():
    names = [_name(1), _name(2), "keep-me.txt", "premigration-broken.dump"]

    assert guard.backups_to_prune(names, 0) == [_name(1)]
    assert guard.backups_to_prune(names, -3) == [_name(1)]
    assert guard.backups_to_prune(["keep-me.txt"], 1) == []


def test_two_backups_in_the_same_second_are_ordered_by_name_and_both_counted():
    first = _name(5, source="a", target="b")
    second = _name(5, source="b", target="c")

    assert guard.backups_to_prune([second, first], 1) == [first]


def test_the_backup_to_restore_is_the_newest_from_a_schema_the_code_has():
    unknown = _name(9, source="0100_future", target="0101_future")
    from_empty = _name(8, source=guard.NO_REVISION, target=HEAD)
    wanted = _name(7, source=OLD, target=HEAD)
    older = _name(2, source=OLD, target=HEAD)

    assert guard.restorable_backup([older, wanted, unknown, from_empty, "x.txt"], KNOWN) == wanted
    assert guard.restorable_backup([unknown, from_empty], KNOWN) is None
    assert guard.restorable_backup([], KNOWN) is None

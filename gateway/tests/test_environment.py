"""The gateway's environment (ADR-0009): its registry, the empty value that means unset, the old names and the typo guard."""
from __future__ import annotations

import logging

import pytest

from musizeer_gateway import environment
from musizeer_gateway.config import Ceilings, ConfigError, GatewayConfig, from_environment


def test_the_defaults_of_the_registry_are_the_defaults_of_the_configuration():
    config = from_environment({})
    defaults = GatewayConfig()

    assert config == GatewayConfig(
        state_dir=defaults.state_dir, run_dir=defaults.run_dir, workspace_dir=defaults.workspace_dir,
        sandbox_uid=defaults.sandbox_uid, sandbox_gid=defaults.sandbox_gid, ceilings=Ceilings(), filesystem_roots=(),
    )


def test_every_variable_is_read_under_its_new_name():
    config = from_environment({
        "MUSIZEER_GATEWAY_STATE_DIR": "/state", "MUSIZEER_GATEWAY_RUN_DIR": "/run/x", "MUSIZEER_GATEWAY_WORKSPACE_DIR": "/work",
        "MUSIZEER_GATEWAY_SANDBOX_UID": "20001", "MUSIZEER_GATEWAY_SANDBOX_GID": "20002",
        "MUSIZEER_GATEWAY_MAX_MEMORY_MB": "512", "MUSIZEER_GATEWAY_MAX_OPEN_FILES": "256", "MUSIZEER_GATEWAY_MAX_PROCESSES": "64",
        "MUSIZEER_GATEWAY_FILESYSTEM_ROOTS": "/downloads",
    })

    assert (str(config.state_dir), str(config.run_dir), str(config.workspace_dir)) == ("/state", "/run/x", "/work")
    assert (config.sandbox_uid, config.sandbox_gid) == (20001, 20002)
    assert (config.ceilings.memory_mb, config.ceilings.open_files, config.ceilings.processes) == (512, 256, 64)
    assert config.filesystem_roots == ("/downloads",)


def test_an_old_name_is_never_read_as_a_value():
    config = from_environment({
        "GATEWAY_STATE_DIR": "/old", "GATEWAY_MAX_MEMORY_MB": "1", "GATEWAY_FILESYSTEM_ROOTS": "/old-root", "GATEWAY_WORKSPACE_DIR": "/old-work",
    })

    assert config == from_environment({})


def test_an_empty_value_is_an_unset_one():
    """Compose passes a variable it was not given as an empty one (`${MUSIZEER_GATEWAY_MAX_MEMORY_MB:-}`)."""
    config = from_environment({"MUSIZEER_GATEWAY_MAX_MEMORY_MB": "", "MUSIZEER_GATEWAY_STATE_DIR": "", "MUSIZEER_GATEWAY_FILESYSTEM_ROOTS": ""})

    assert config == from_environment({})


def test_a_value_that_is_not_a_number_names_its_variable():
    with pytest.raises(ConfigError, match="MUSIZEER_GATEWAY_MAX_PROCESSES"):
        from_environment({"MUSIZEER_GATEWAY_MAX_PROCESSES": "many"})


def test_the_registry_has_no_duplicates_and_every_name_is_prefixed():
    names = [entry.name for entry in environment.ENTRIES]

    assert len(names) == len(set(names))
    assert all(name.startswith(environment.PREFIX) for name in names)
    assert all(entry.old_name and not entry.old_name.startswith(environment.PREFIX) for entry in environment.ENTRIES)


def test_an_old_name_that_is_set_is_an_error_that_names_the_new_one():
    findings = environment.check({"GATEWAY_MAX_MEMORY_MB": "9999", "GATEWAY_STATE_DIR": "/secret-looking-path"})

    assert sorted(findings) == [
        ("error", "GATEWAY_MAX_MEMORY_MB is no longer read: rename it to MUSIZEER_GATEWAY_MAX_MEMORY_MB"),
        ("error", "GATEWAY_STATE_DIR is no longer read: rename it to MUSIZEER_GATEWAY_STATE_DIR"),
    ]
    assert "9999" not in str(findings) and "secret-looking-path" not in str(findings), "a value is never printed"


def test_an_old_name_that_is_empty_is_not_reported():
    assert environment.check({"GATEWAY_MAX_MEMORY_MB": ""}) == []


def test_the_old_product_prefix_is_reported_with_the_new_name():
    old = environment.OLD_PREFIX

    assert environment.check({old + "VERSION": "1.0"}) == [("error", f"{old}VERSION is no longer read: rename it to MUSIZEER_VERSION")]
    assert environment.check({old + "GATEWAY_STATE_DIR": "/x"}) == [("warning", f"{old}GATEWAY_STATE_DIR is not read; did you mean MUSIZEER_GATEWAY_STATE_DIR?")]


def test_a_mistyped_variable_gets_a_suggestion():
    findings = environment.check({"MUSIZEER_GATEWAY_STAT_DIR": "/x", "MUSIZEER_GATEWAY_MAX_MEMORY_MBB": "1"})

    assert ("warning", "unknown variable MUSIZEER_GATEWAY_STAT_DIR; did you mean MUSIZEER_GATEWAY_STATE_DIR?") in findings
    assert ("warning", "unknown variable MUSIZEER_GATEWAY_MAX_MEMORY_MBB; did you mean MUSIZEER_GATEWAY_MAX_MEMORY_MB?") in findings


def test_the_variables_of_the_application_are_not_reported_by_the_gateway():
    """The application has a registry of its own; the gateway can only judge what is close to its names."""
    assert environment.check({"MUSIZEER_AUTH_SECRET": "x", "MUSIZEER_DATA_DIR": "/data", "MUSIZEER_PUBLIC_URL": "http://x"}) == []


def test_the_report_at_start_goes_to_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger=environment.logger.name):
        environment.report_at_start({"GATEWAY_RUN_DIR": "/old", "MUSIZEER_GATEWAY_RUN_DIRR": "/typo"})

    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("ERROR", "GATEWAY_RUN_DIR is no longer read: rename it to MUSIZEER_GATEWAY_RUN_DIR"),
        ("WARNING", "unknown variable MUSIZEER_GATEWAY_RUN_DIRR; did you mean MUSIZEER_GATEWAY_RUN_DIR?"),
    ]

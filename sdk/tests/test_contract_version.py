"""The contract version a provider declares is the major version of the SDK."""
from __future__ import annotations

import tomllib
from pathlib import Path

import musizerr_sdk
from musizerr_sdk.interfaces import PROVIDER_API_VERSION


def test_the_contract_version_is_the_major_version_of_the_package():
    pyproject = Path(next(iter(musizerr_sdk.__path__))).parent.parent / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    assert PROVIDER_API_VERSION == 5
    assert int(version.split(".")[0]) == PROVIDER_API_VERSION

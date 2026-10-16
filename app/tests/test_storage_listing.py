"""
``StorageProvider.list_files``: the relative path of every stored file, or
``None`` for a provider that cannot list them (the contract's default). How a
real storage walks its folder or its server is tested in its own repository.
"""
from __future__ import annotations

from tests.conftest import FakeStorageProvider


def test_a_provider_that_does_not_list_its_files_says_so():
    assert FakeStorageProvider().list_files("", session=None) is None

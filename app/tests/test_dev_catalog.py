"""scripts/dev_catalog.py: the entries of a development catalog are made from an image's labels, and are valid."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from core.catalog import parse_catalog, parse_entry
from musizeer_sdk.context import StaticProviderContext
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizeer_sdk.labels import labels_for
from musizeer_sdk.manifest import build
from musizeer_sdk.permissions import Permissions, TcpAccess

NO_AUTH = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_catalog.py"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
IMAGE = "oci-registry:5000/musizeer-provider-demo"


@pytest.fixture(scope="module")
def dev_catalog():
    spec = importlib.util.spec_from_file_location("dev_catalog_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def labels_text(name="demo", label="Demo provider", permissions=None, extra=()) -> str:
    """What scripts/build-provider-image.sh writes: the labels a real provider prints, one per line."""
    class Demo(LyricsProvider):
        provider_api_version = PROVIDER_API_VERSION
        auth = NO_AUTH

        def validate_config(self, config):
            return True

        def test_connection(self, session):
            return True

        def get_lyrics(self, track, session):
            return None

    Demo.name, Demo.label = name, label
    Demo.permissions = permissions or Permissions(egress=("api.example.org:443",))
    lines = [f"{key}={value}" for key, value in labels_for(build(Demo(StaticProviderContext()))).items()]
    return "\n".join([*lines, *extra]) + "\n"


def test_an_entry_is_made_of_the_labels_of_the_image_and_is_a_valid_catalog_entry(dev_catalog):
    text = labels_text(extra=("org.opencontainers.image.description=Does demo things.", "org.opencontainers.image.url=https://demo.example"))

    entry = dev_catalog.entry_from_labels(dev_catalog.read_labels(text), IMAGE, DIGEST)
    parsed = parse_entry(entry)

    assert entry["name"] == "demo" and entry["label"] == "Demo provider" and entry["description"] == "Does demo things."
    assert entry["homepage"] == "https://demo.example" and entry["image"] == IMAGE and entry["digest"] == DIGEST
    assert entry["provider_api_version"] == PROVIDER_API_VERSION and entry["roles"] == ["lyrics"]
    assert parsed.compatible and parsed.permissions == Permissions(egress=("api.example.org:443",)) and parsed.registry == "oci-registry:5000"


def test_without_the_oci_labels_the_description_is_empty_and_there_is_no_homepage(dev_catalog):
    entry = dev_catalog.entry_from_labels(dev_catalog.read_labels(labels_text()), IMAGE, DIGEST)

    assert entry["description"] == "" and "homepage" not in entry
    parse_entry(entry)


def test_a_homepage_that_is_not_a_web_address_is_left_out(dev_catalog):
    text = labels_text(extra=("org.opencontainers.image.url=javascript:alert(1)",))

    assert "homepage" not in dev_catalog.entry_from_labels(dev_catalog.read_labels(text), IMAGE, DIGEST)


def test_wildcards_and_raw_tcp_pass_through_the_labels_unchanged(dev_catalog):
    permissions = Permissions(egress=("*.cdn.example:443",), tcp_from_settings=(TcpAccess("host", "any"),))

    entry = dev_catalog.entry_from_labels(dev_catalog.read_labels(labels_text(permissions=permissions)), IMAGE, DIGEST)

    assert parse_entry(entry).permissions == permissions


def test_an_image_whose_labels_do_not_describe_a_provider_makes_no_entry(dev_catalog):
    with pytest.raises(SystemExit, match="lack"):
        dev_catalog.entry_from_labels({"org.musizeer.provider.name": "x"}, IMAGE, DIGEST)
    with pytest.raises(SystemExit, match="not readable"):
        dev_catalog.entry_from_labels(
            {**dev_catalog.read_labels(labels_text()), "org.musizeer.provider.permissions": "{"}, IMAGE, DIGEST,
        )


def test_a_new_entry_is_appended_and_an_existing_one_is_replaced_where_it_stands(dev_catalog):
    first = dev_catalog.entry_from_labels(dev_catalog.read_labels(labels_text("first")), "oci-registry:5000/musizeer-provider-first", DIGEST)
    second = dev_catalog.entry_from_labels(dev_catalog.read_labels(labels_text("second")), "oci-registry:5000/musizeer-provider-second", DIGEST)

    catalog = dev_catalog.merge(dev_catalog.merge(None, first, "Mine"), second)
    updated = dev_catalog.merge(catalog, {**first, "digest": OTHER})

    assert catalog["name"] == "Mine" and catalog["version"] == 2 and [p["name"] for p in catalog["providers"]] == ["first", "second"]
    assert [(p["name"], p["digest"]) for p in updated["providers"]] == [("first", OTHER), ("second", DIGEST)]
    assert parse_catalog(updated).rejected == () and [e.name for e in parse_catalog(updated).entries] == ["first", "second"]


def test_a_file_that_is_not_a_version_2_catalog_is_never_overwritten(dev_catalog):
    entry = dev_catalog.entry_from_labels(dev_catalog.read_labels(labels_text()), IMAGE, DIGEST)

    for document in ({"version": 1, "providers": []}, {"providers": []}, [], {"version": 2, "providers": {}}):
        with pytest.raises(SystemExit, match="not a version 2 catalog"):
            dev_catalog.merge(document, entry)


def test_the_command_writes_the_catalog_and_running_it_again_moves_the_digest_on(dev_catalog, tmp_path, capsys):
    labels = tmp_path / "labels"
    labels.write_text(labels_text())
    catalog = tmp_path / "catalogs" / "registry.json"
    arguments = ["--catalog", str(catalog), "--image", IMAGE, "--labels", str(labels)]

    assert dev_catalog.main([*arguments, "--digest", DIGEST]) == 0
    assert "added demo" in capsys.readouterr().out
    assert dev_catalog.main([*arguments, "--digest", OTHER]) == 0
    assert "updated demo" in capsys.readouterr().out

    document = json.loads(catalog.read_text())
    assert [(p["name"], p["digest"]) for p in document["providers"]] == [("demo", OTHER)]
    assert document["name"] == "Musizeer development catalog"
    assert parse_catalog(document).rejected == ()
    assert stat.S_IMODE(os.stat(catalog).st_mode) == 0o644, "the catalog server reads it as another user"
    assert [p.name for p in catalog.parent.iterdir()] == ["registry.json"], "no temporary file is left"

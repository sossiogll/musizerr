"""From an installed entry to an unpacked image: what the labels must say, and what is cached."""
from __future__ import annotations

import json
import os

import pytest

from musizerr_gateway.config import GatewayConfig
from musizerr_gateway.images import MOUNTPOINTS, ImageStore
from musizerr_gateway.oci import ImageError
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.permissions import FilesystemAccess, Permissions, ResourceLimits, canonical
from support import FakeImage, FakeRegistry, Tar, labels_for


def store(tmp_path, registry: FakeRegistry) -> ImageStore:
    return ImageStore(GatewayConfig(state_dir=tmp_path / "state"), http=registry, architecture="amd64")


def publish(registry: FakeRegistry, **kwargs):
    image = FakeImage([Tar().file("usr/local/bin/python", b"py", 0o755), Tar().file("app/provider.py", b"x")], **kwargs).build()
    return image, registry.add_image(image)


def test_an_image_is_pulled_unpacked_and_ready_to_run(tmp_path):
    registry = FakeRegistry()
    _, pinned = publish(registry)

    prepared = store(tmp_path, registry).prepare(registry.entry(pinned, roles=("lyrics",)))

    assert (prepared.rootfs / "app/provider.py").read_bytes() == b"x"
    assert all((prepared.rootfs / mountpoint).is_dir() for mountpoint in MOUNTPOINTS)
    assert prepared.declared.name == "demo" and prepared.declared.roles == ("lyrics",)
    assert prepared.declared.permissions.egress == ("lrclib.net:443",)
    assert prepared.command == ["python", "-m", "musizerr_sdk.runner"]


@pytest.mark.parametrize("labels, entry_kwargs, message", [
    (labels_for(name="other"), {}, "declares the provider 'other'"),
    (labels_for(version=PROVIDER_API_VERSION + 1), {}, "provider API version"),
    (labels_for(version=PROVIDER_API_VERSION - 1), {}, "provider API version"),
    (labels_for(roles="metadata"), {"roles": ("lyrics",)}, "declares the roles"),
    ({}, {}, "lacks the labels"),
    (labels_for(roles="teleport"), {}, "roles"),
    (labels_for(permissions="{not json"), {}, "unreadable permissions"),
])
def test_an_image_that_does_not_describe_itself_as_the_entry_says_is_refused(tmp_path, labels, entry_kwargs, message):
    registry = FakeRegistry()
    _, pinned = publish(registry, labels=labels)

    with pytest.raises(ImageError, match=message):
        store(tmp_path, registry).prepare(registry.entry(pinned, **entry_kwargs))


def test_the_labels_are_checked_before_any_layer_is_downloaded(tmp_path):
    registry = FakeRegistry()
    _, pinned = publish(registry, labels=labels_for(name="other"))

    with pytest.raises(ImageError):
        store(tmp_path, registry).prepare(registry.entry(pinned))

    assert not any("/blobs/" in url and url.rsplit("/", 1)[1] != json.loads(registry.routes[f"{registry.base}/manifests/{pinned}"][2])["config"]["digest"]
                   for url, _ in registry.requests), "only the config was fetched"


def test_an_image_without_an_entry_point_is_refused(tmp_path):
    registry = FakeRegistry()
    _, pinned = publish(registry, entrypoint=())

    with pytest.raises(ImageError, match="no entry point"):
        store(tmp_path, registry).prepare(registry.entry(pinned))


def test_a_second_start_uses_the_unpacked_image_without_the_registry(tmp_path):
    registry = FakeRegistry()
    _, pinned = publish(registry)
    images = store(tmp_path, registry)
    images.prepare(registry.entry(pinned))
    fetched = len(registry.requests)

    again = images.prepare(registry.entry(pinned))

    assert len(registry.requests) == fetched and again.declared.name == "demo"


def test_an_entry_that_changes_its_name_is_checked_against_the_cached_image_too(tmp_path):
    registry = FakeRegistry()
    _, pinned = publish(registry)
    images = store(tmp_path, registry)
    images.prepare(registry.entry(pinned))

    with pytest.raises(ImageError, match="declares the provider 'demo'"):
        images.prepare(registry.entry(pinned, name="impostor"))


def test_images_with_a_layer_in_common_store_it_once(tmp_path):
    registry = FakeRegistry()
    shared = Tar().file("usr/lib/libshared", b"s" * 500)
    one = FakeImage([shared, Tar().file("a", b"1")]).build()
    two = FakeImage([shared, Tar().file("b", b"2")], labels=labels_for(name="two")).build()
    pinned_one, pinned_two = registry.add_image(one), registry.add_image(two)
    images = store(tmp_path, registry)

    first = images.prepare(registry.entry(pinned_one))
    second = images.prepare(registry.entry(pinned_two, name="two"))

    assert os.path.samestat(os.stat(first.rootfs / "usr/lib/libshared"), os.stat(second.rootfs / "usr/lib/libshared"))
    layer_dirs = [p for p in (tmp_path / "state/layers").iterdir() if not p.name.startswith(".")]
    assert len(layer_dirs) == 3, "two images, three distinct layers"
    shared_fetches = [url for url, _ in registry.requests if url.endswith(next(iter(shared_digest(shared))))]
    assert len(shared_fetches) == 1


def shared_digest(tar: Tar):
    from support import digest

    yield digest(tar.gzipped())


def _permissions_label(**kwargs) -> str:
    base = {"egress": ["lrclib.net:443"], "egress_from_settings": [], "filesystem_from_settings": [], "limits": {}}
    return json.dumps({**base, **kwargs})


@pytest.mark.parametrize("declared, what", [
    (_permissions_label(egress=["lrclib.net:443", "evil.example:443"]), "egress"),
    (_permissions_label(egress=[]), "egress"),
    (_permissions_label(egress_from_settings=["url"]), "egress_from_settings"),
    (_permissions_label(filesystem_from_settings=[{"setting": "root", "mode": "rw"}]), "filesystem_from_settings"),
    (_permissions_label(limits={"memory_mb": 4096}), "limits"),
])
def test_an_image_whose_permissions_are_not_the_approved_ones_is_refused(tmp_path, declared, what):
    registry = FakeRegistry()
    _, pinned = publish(registry, labels=labels_for(permissions=declared))

    with pytest.raises(ImageError, match="not the ones approved") as refusal:
        store(tmp_path, registry).prepare(registry.entry(pinned))

    assert what in str(refusal.value)
    assert not any("/blobs/" in url and "layer" in url for url, _ in registry.requests)


def test_the_same_permissions_in_another_order_are_the_approved_ones(tmp_path):
    registry = FakeRegistry()
    declared = _permissions_label(egress=["b.example:443", "a.example:443"], egress_from_settings=["url", "host"])
    _, pinned = publish(registry, labels=labels_for(permissions=declared))
    approved = canonical(Permissions(egress=("a.example:443", "b.example:443"), egress_from_settings=("host", "url")))

    prepared = store(tmp_path, registry).prepare(registry.entry(pinned, permissions=approved))

    assert set(prepared.declared.permissions.egress) == {"a.example:443", "b.example:443"}


def test_what_was_approved_is_checked_against_an_image_that_is_already_unpacked_too(tmp_path):
    registry = FakeRegistry()
    _, pinned = publish(registry, labels=labels_for(permissions=_permissions_label(egress=["lrclib.net:443", "new.example:443"])))
    images = store(tmp_path, registry)
    approved_then = canonical(Permissions(egress=("lrclib.net:443", "new.example:443")))
    images.prepare(registry.entry(pinned, permissions=approved_then))

    with pytest.raises(ImageError, match="not the ones approved"):
        images.prepare(registry.entry(pinned))  # the admin approved less this time


def test_the_credential_reaches_the_registry_client(tmp_path):
    registry = FakeRegistry()
    registry.require_basic = "musizerr:s3cret"
    _, pinned = publish(registry)

    prepared = store(tmp_path, registry).prepare(registry.entry(pinned), "s3cret")

    assert prepared.rootfs.is_dir()


def test_the_credential_is_written_nowhere_in_the_state_directory(tmp_path):
    registry = FakeRegistry()
    registry.require_basic = "musizerr:ghp_canary_token"
    _, pinned = publish(registry)

    store(tmp_path, registry).prepare(registry.entry(pinned), "ghp_canary_token")

    needle = b"ghp_canary_token"
    import base64

    encoded = base64.b64encode(b"musizerr:ghp_canary_token")
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert needle not in data and encoded not in data, path

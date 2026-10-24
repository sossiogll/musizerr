"""Pulling by digest: what is verified, what is refused, and how the registry is spoken to."""
from __future__ import annotations

import io
from dataclasses import replace

import pytest

from musizeer_gateway.oci import ImageError, RegistryClient, RegistryError, host_architecture
from support import FakeImage, FakeRegistry, Tar, digest, labels_for


def image(**kwargs) -> FakeImage:
    return FakeImage([Tar().file("etc/os", b"base"), Tar().file("app/x.py", b"print()")], **kwargs).build()


def client_for(registry: FakeRegistry, pinned: str, architecture: str = "amd64", credential: str | None = None, **kwargs) -> RegistryClient:
    return RegistryClient(registry.entry(pinned, **kwargs), http=registry, architecture=architecture, credential=credential)


def test_an_image_is_resolved_from_its_pinned_manifest():
    registry, built = FakeRegistry(), image()
    pinned = registry.add_image(built)

    resolved = client_for(registry, pinned).resolve()

    assert resolved.manifest_digest == built.manifest_digest
    assert len(resolved.layers) == 2 and len(resolved.config.diff_ids) == 2
    assert resolved.config.labels["org.musizeer.provider.name"] == "demo"
    assert resolved.config.entrypoint == ("python", "-m", "musizeer_sdk.runner")
    assert resolved.config.env["PATH"] == "/usr/bin"


def test_docker_media_types_are_understood_too():
    registry = FakeRegistry()
    pinned = registry.add_image(image(docker_types=True))

    assert len(client_for(registry, pinned).resolve().layers) == 2


def test_a_multi_architecture_index_resolves_to_the_platform_of_the_host():
    registry, built = FakeRegistry(), image()
    unknown = {"mediaType": "x", "digest": "sha256:" + "0" * 64, "size": 1, "platform": {"os": "unknown", "architecture": "unknown"}}
    other = {"mediaType": "x", "digest": "sha256:" + "1" * 64, "size": 1, "platform": {"os": "linux", "architecture": "s390x"}}
    pinned = registry.add_image(built, as_index_for="arm64", extra_index_entries=[unknown, other])

    assert client_for(registry, pinned, "arm64").resolve().manifest_digest == built.manifest_digest
    with pytest.raises(ImageError, match="no linux/amd64 variant"):
        client_for(registry, pinned, "amd64").resolve()


def test_the_manifest_must_hash_to_the_digest_it_was_pinned_to():
    registry, built = FakeRegistry(), image()
    pinned = registry.add_image(built)
    registry.routes[f"{registry.base}/manifests/{pinned}"] = (200, {}, built.manifest + b" ")

    with pytest.raises(ImageError, match="not to the .* it was pinned to"):
        client_for(registry, pinned).resolve()


def test_the_config_must_hash_to_the_digest_the_manifest_names():
    registry, built = FakeRegistry(), image()
    pinned = registry.add_image(built)
    config_digest = next(d for d, blob in built.blobs.items() if blob.startswith(b'{"architecture"'))
    registry.routes[f"{registry.base}/blobs/{config_digest}"] = (200, {}, b'{"config": {}}')

    with pytest.raises(ImageError, match="the config hashes to"):
        client_for(registry, pinned).resolve()


def test_a_layer_must_hash_to_its_digest_and_have_its_size():
    registry, built = FakeRegistry(), image()
    pinned = registry.add_image(built)
    layer = client_for(registry, pinned).resolve().layers[0]

    out = io.BytesIO()
    client_for(registry, pinned).download_blob(layer, out)
    assert digest(out.getvalue()) == layer.digest

    registry.routes[f"{registry.base}/blobs/{layer.digest}"] = (200, {}, b"x" * layer.size)
    with pytest.raises(ImageError, match="hashes to"):
        client_for(registry, pinned).download_blob(layer, io.BytesIO())

    registry.routes[f"{registry.base}/blobs/{layer.digest}"] = (200, {}, b"short")
    with pytest.raises(ImageError, match="has 5 bytes"):
        client_for(registry, pinned).download_blob(layer, io.BytesIO())

    registry.routes[f"{registry.base}/blobs/{layer.digest}"] = (200, {}, b"y" * (layer.size + 10))
    with pytest.raises(ImageError, match="larger than"):
        client_for(registry, pinned).download_blob(layer, io.BytesIO())


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m.update(layers=[]), "no layers"),
    (lambda m: m["layers"][0].update(mediaType="application/vnd.oci.image.layer.v1.tar+zstd"), "unsupported layer type"),
    (lambda m: m["layers"].pop(), "disagree on the number of layers"),
    (lambda m: m["config"].update(digest="latest"), "no sha256 digest"),
])
def test_a_manifest_that_is_not_usable_is_refused(mutate, message):
    import json

    registry, built = FakeRegistry(), image()
    document = json.loads(built.manifest)
    mutate(document)
    manifest = json.dumps(document).encode()
    registry.routes[f"{registry.base}/manifests/{digest(manifest)}"] = (200, {}, manifest)
    for blob_digest, blob in built.blobs.items():
        registry.routes[f"{registry.base}/blobs/{blob_digest}"] = (200, {}, blob)

    with pytest.raises(ImageError, match=message):
        client_for(registry, digest(manifest)).resolve()


def test_an_anonymous_bearer_token_is_fetched_when_the_registry_asks_for_one():
    registry, built = FakeRegistry(), image()
    registry.require_token = "T0KEN"
    pinned = registry.add_image(built)

    resolved = client_for(registry, pinned).resolve()

    assert len(resolved.layers) == 2
    assert registry.token_calls == 1, "one token serves the whole pull"
    token_url = next(url for url, _ in registry.requests if url.startswith(registry.token_realm))
    assert "scope=repository%3Amusizeer%2Fdemo%3Apull" in token_url and "service=registry.test" in token_url
    assert all(headers.get("Authorization") == "Bearer T0KEN" for url, headers in registry.requests[2:] if url.startswith(registry.base))


def test_a_registry_that_wants_credentials_is_refused_when_the_source_has_none():
    registry = FakeRegistry()
    registry.get = lambda url, headers: __import__("support")._response(401, {"www-authenticate": 'Basic realm="x"'}, b"")  # type: ignore[method-assign]

    with pytest.raises(ImageError, match="this source has none"):
        client_for(registry, "sha256:" + "a" * 64).resolve()


def test_a_token_realm_that_is_an_internal_address_is_refused():
    registry = FakeRegistry()
    registry.token_realm = "https://127.0.0.1/token"
    registry.require_token = "x"

    with pytest.raises(ImageError, match="internal address"):
        client_for(registry, "sha256:" + "a" * 64).resolve()


def test_the_authorization_header_does_not_follow_a_redirect_to_another_host():
    registry, built = FakeRegistry(), image()
    registry.require_token = "T0KEN"
    pinned = registry.add_image(built)
    layer = client_for(registry, pinned).resolve().layers[0]
    registry.redirect_blobs_to = "https://cdn.example/blobs/"

    client = client_for(registry, pinned)
    client.resolve()
    client.download_blob(layer, io.BytesIO())

    cdn = [headers for url, headers in registry.requests if url.startswith("https://cdn.example/")]
    assert cdn and all("Authorization" not in headers for headers in cdn)


def test_a_redirect_that_leaves_https_is_refused():
    registry, built = FakeRegistry(), image()
    pinned = registry.add_image(built)
    layer = client_for(registry, pinned).resolve().layers[0]
    registry.redirect_blobs_to = "http://cdn.example/blobs/"

    with pytest.raises(ImageError, match="from https to http"):
        client_for(registry, pinned).download_blob(layer, io.BytesIO())


def test_plain_http_is_spoken_only_to_a_registry_the_entry_marks_insecure():
    registry, built = FakeRegistry(scheme="http"), image()
    pinned = registry.add_image(built)

    assert len(client_for(registry, pinned, insecure=True).resolve().layers) == 2
    # an entry that is not marked insecure asks for https://, which this registry does not serve
    strict = replace(registry.entry(pinned), insecure=False)
    with pytest.raises(ImageError, match="not found"):
        RegistryClient(strict, http=registry, architecture="amd64").resolve()
    assert all(url.startswith("https://") for url, _ in registry.requests[-1:])


def test_a_missing_object_and_a_refusal_are_told_apart_from_an_outage():
    registry = FakeRegistry()
    with pytest.raises(ImageError, match="not found"):
        client_for(registry, "sha256:" + "a" * 64).resolve()

    registry.get = lambda url, headers: __import__("support")._response(503, {}, b"")  # type: ignore[method-assign]
    with pytest.raises(RegistryError, match="503"):
        client_for(registry, "sha256:" + "a" * 64).resolve()


def test_the_architecture_of_the_host_is_named_the_oci_way(monkeypatch):
    import platform

    for machine, expected in (("x86_64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")):
        monkeypatch.setattr(platform, "machine", lambda m=machine: m)
        assert host_architecture() == expected


# ---- a private registry: the credential of the source ---------------------------------------


def private_registry(secret: str = "ghp_s3cret", user: str = "musizeer") -> tuple[FakeRegistry, str]:
    registry, built = FakeRegistry(), image()
    registry.require_basic = f"{user}:{secret}"
    return registry, registry.add_image(built)


def test_a_private_image_is_pulled_with_the_credential_sent_to_the_token_service_only():
    registry, pinned = private_registry()

    resolved = client_for(registry, pinned, credential="ghp_s3cret").resolve()

    assert len(resolved.layers) == 2
    token_calls = [h for url, h in registry.requests if url.startswith(registry.token_realm)]
    assert len(token_calls) == 1 and token_calls[0]["Authorization"].startswith("Basic ")
    for url, headers in registry.requests:
        if url.startswith(registry.base):
            assert headers.get("Authorization", "Bearer T0KEN") == "Bearer T0KEN", "the registry itself only sees the token"


def test_a_user_and_a_token_given_as_user_colon_token_are_sent_as_they_are():
    registry, pinned = private_registry(secret="pw:with:colons", user="alice")

    assert client_for(registry, pinned, credential="alice:pw:with:colons").resolve().layers


def test_the_wrong_credential_is_refused_and_never_named():
    registry, pinned = private_registry()

    with pytest.raises(ImageError, match="token service refused the credential") as refusal:
        client_for(registry, pinned, credential="ghp_wrong").resolve()

    assert "ghp_wrong" not in str(refusal.value)


def test_a_registry_that_challenges_with_basic_gets_the_credential_directly():
    registry, built = FakeRegistry(), image()
    registry.require_basic, registry.basic_challenge = "musizeer:tok", True
    pinned = registry.add_image(built)

    assert client_for(registry, pinned, credential="tok").resolve().layers
    assert registry.token_calls == 0


def test_without_a_credential_a_private_image_is_refused_not_guessed():
    registry, pinned = private_registry()

    with pytest.raises(ImageError, match="token service answered 401"):
        client_for(registry, pinned).resolve()


def test_a_registry_that_refuses_even_the_token_says_it_refuses_the_credential():
    registry, built = FakeRegistry(), image()
    registry.require_token = "something-else"
    pinned = registry.add_image(built)

    with pytest.raises(ImageError, match="refuses the credential"):
        client_for(registry, pinned, credential="tok").resolve()


def test_the_credential_never_goes_to_a_token_realm_that_is_not_the_registrys_own():
    registry, pinned = private_registry()
    registry.token_realm = "https://auth.elsewhere.example/token"

    with pytest.raises(ImageError, match="not the registry's own"):
        client_for(registry, pinned, credential="ghp_s3cret").resolve()

    assert not any(url.startswith(registry.token_realm) for url, _ in registry.requests)


@pytest.mark.parametrize("registry, realm, allowed", [
    ("ghcr.io", "ghcr.io", True),
    ("ghcr.io", "evil.io", False),
    ("ghcr.io", "auth.ghcr.io", True),
    ("ghcr.io", "xghcr.io", False),
    ("registry-1.docker.io", "auth.docker.io", True),
    ("registry-1.docker.io", "docker.io", True),
    ("registry-1.docker.io", "auth.evil.io", False),
    ("registry-1.docker.io", "evildocker.io", False),
    ("registry-1.docker.io", "docker.io.evil.com", False),
    ("quay.io", "quay.io", True),
    ("registry.local:5000", "registry.local", True),
    ("10.0.0.5:5000", "10.0.0.6", False),
    ("10.0.0.5:5000", "10.0.0.5", True),
])
def test_which_token_realms_may_receive_the_credential(registry, realm, allowed):
    from musizeer_gateway.oci import _realm_belongs_to

    assert _realm_belongs_to(registry, realm) is allowed


def test_a_credential_is_never_sent_over_plain_http():
    registry, built = FakeRegistry(scheme="http"), image()
    pinned = registry.add_image(built)

    with pytest.raises(ImageError, match="plain HTTP"):
        client_for(registry, pinned, credential="tok", insecure=True)


def test_the_credential_does_not_follow_a_redirect_to_another_host():
    registry, pinned = private_registry()
    layer = client_for(registry, pinned, credential="ghp_s3cret").resolve().layers[0]
    registry.redirect_blobs_to = "https://cdn.example/blobs/"

    client = client_for(registry, pinned, credential="ghp_s3cret")
    client.resolve()
    client.download_blob(layer, io.BytesIO())

    cdn = [headers for url, headers in registry.requests if url.startswith("https://cdn.example/")]
    assert cdn and all("Authorization" not in headers for headers in cdn)
    assert not any("ghp_s3cret" in str(headers) for _, headers in registry.requests)


def test_a_public_image_never_sees_the_credential_at_all():
    registry, built = FakeRegistry(), image()
    pinned = registry.add_image(built)

    client_for(registry, pinned, credential="ghp_s3cret").resolve()

    assert not any("Authorization" in headers for _, headers in registry.requests)


def test_an_expired_token_is_asked_for_again_once():
    registry, pinned = private_registry()
    client = client_for(registry, pinned, credential="ghp_s3cret")
    client.resolve()
    client._authorization = "Bearer expired"  # noqa: SLF001

    client.fetch_manifest(pinned)

    assert registry.token_calls == 2

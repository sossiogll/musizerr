"""The desired set the host pushes: what is a usable entry and what is refused."""
from __future__ import annotations

import pytest

from musizeer_gateway.desired import DesiredError, parse_entry, parse_set
from musizeer_sdk.permissions import FilesystemAccess, Permissions, ResourceLimits, canonical

D = "sha256:" + "a" * 64
PERMISSIONS = {"egress": ["lrclib.net:443"], "egress_from_settings": [], "filesystem_from_settings": [], "limits": {}}


def entry(**kwargs):
    return {
        "name": "lrclib", "image": "ghcr.io/sossiogll/musizeer-provider-lrclib", "digest": D,
        "roles": ["lyrics"], "permissions": PERMISSIONS, **kwargs,
    }


def test_an_entry_names_a_provider_a_registry_a_repository_a_digest_and_what_was_approved():
    parsed, credential = parse_entry(entry())

    assert (parsed.name, parsed.registry, parsed.repository, parsed.digest) == (
        "lrclib", "ghcr.io", "sossiogll/musizeer-provider-lrclib", D,
    )
    assert parsed.reference == f"ghcr.io/sossiogll/musizeer-provider-lrclib@{D}"
    assert parsed.roles == ("lyrics",) and parsed.insecure is False and credential is None
    assert parsed.permissions == canonical(Permissions(egress=("lrclib.net:443",)))


def test_a_registry_with_a_port_and_an_image_pinned_inline_are_understood():
    parsed, _ = parse_entry(entry(image=f"registry.local:5000/team/x@{D}", digest=None, insecure=True))

    assert (parsed.registry, parsed.repository, parsed.digest, parsed.insecure) == ("registry.local:5000", "team/x", D, True)


def test_the_approved_permissions_are_compared_whatever_their_order():
    one, _ = parse_entry(entry(permissions={"egress": ["b.test:443", "a.test:443"], "egress_from_settings": ["url", "host"]}))
    other, _ = parse_entry(entry(permissions={"egress": ["a.test:443", "b.test:443"], "egress_from_settings": ["host", "url"]}))
    files, _ = parse_entry(entry(permissions={
        "filesystem_from_settings": [{"setting": "root", "mode": "rw"}], "limits": {"memory_mb": 64},
    }))

    assert one == other
    assert files.permissions == Permissions(
        filesystem_from_settings=(FilesystemAccess("root", "rw"),), limits=ResourceLimits(memory_mb=64),
    )


def test_the_credential_is_not_part_of_the_entry():
    with_token, token = parse_entry(entry(credential="ghp_secret"))
    without, _ = parse_entry(entry())

    assert token == "ghp_secret" and with_token == without
    assert "ghp_secret" not in repr(with_token)


@pytest.mark.parametrize("raw, message", [
    ("nope", "JSON object"),
    (entry(name="Bad Name"), "not a provider name"),
    (entry(name="../x"), "not a provider name"),
    ({"name": "x"}, "no image"),
    (entry(digest=None), "digest is required"),
    (entry(digest="latest"), "digest is required"),
    (entry(digest="sha256:" + "A" * 64), "digest is required"),
    (entry(image="lrclib"), "registry/repository"),
    (entry(image="ghcr.io/Upper/Case"), "registry/repository"),
    (entry(image="ghcr.io/x/y:latest"), "registry/repository"),
    (entry(image=f"ghcr.io/x/y@sha256:{'b' * 64}"), "different digests"),
    (entry(insecure="yes"), "true or false"),
    (entry(roles="lyrics"), "non-empty list of roles"),
    (entry(roles=[]), "non-empty list of roles"),
    (entry(roles=["time-travel"]), "non-empty list of roles"),
    (entry(permissions=None), "permissions"),
    (entry(permissions=[]), "permissions"),
    (entry(permissions={"egress": ["not a target"]}), "host:port"),
    (entry(credential=""), "printable"),
    (entry(credential="line\nbreak"), "printable"),
    (entry(credential=5), "printable"),
    (entry(credential="token", insecure=True), "plain HTTP"),
])
def test_an_entry_that_cannot_be_used_is_refused_with_a_reason(raw, message):
    with pytest.raises(DesiredError, match=message):
        parse_entry(raw)


def test_roles_and_permissions_are_mandatory():
    for missing in ("roles", "permissions"):
        raw = entry()
        del raw[missing]
        with pytest.raises(DesiredError):
            parse_entry(raw)


def test_a_set_gives_the_usable_entries_their_credentials_and_why_the_others_are_not():
    entries, credentials, refused = parse_set([
        entry(credential="tok"), entry(name="other", digest="main"), entry(), "junk", entry(name="third", credential="t3"),
    ])

    assert [e.name for e in entries] == ["lrclib", "third"]
    assert credentials == {"lrclib": "tok", "third": "t3"}
    assert set(refused) == {"other", "#3", "lrclib"}
    assert "listed twice" in refused["lrclib"] and "digest" in refused["other"]


def test_a_set_that_is_not_a_list_or_is_huge_is_an_error():
    for bad in (None, {}, "x"):
        with pytest.raises(DesiredError, match="list"):
            parse_set(bad)
    with pytest.raises(DesiredError, match="more than"):
        parse_set([entry(name=f"p{i}") for i in range(300)])


def test_an_empty_set_is_valid_and_means_no_providers():
    assert parse_set([]) == ([], {}, {})

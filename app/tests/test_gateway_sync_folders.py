"""The host pushes the root folders and the working folder with the installed set (ADR-0008 §9) and records the gateway's
verdict on each root; the identifier of the set covers them, so a change of a root or of the working folder is pushed."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from db import root_store
from services import roots
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync, desired_state, generation
from tests.folders_support import add_root, store_folder
from tests.sources_support import FakeControlSocket
from tests.test_gateway_sync import install


@pytest.fixture
def gateway():
    fake = FakeControlSocket()
    yield fake
    fake.close()


@pytest.fixture
def sync(gateway, engine):
    return GatewaySync(GatewayClient(gateway.path), lambda: Session(engine))


def verdict_of(engine, root_id):
    with Session(engine) as session:
        root = root_store.get(session, root_id)
        return root.state, root.reason, root.checked_at is not None


def test_the_identifier_of_the_set_covers_the_roots_and_the_working_folder():
    entries = [{"name": "a", "digest": "sha256:" + "a" * 64}]
    base = generation(entries, [{"id": "r1", "path": "/library"}], "/downloads/.w")

    assert base == generation(entries, [{"id": "r1", "path": "/library"}], "/downloads/.w")
    assert base != generation(entries, [{"id": "r1", "path": "/music"}], "/downloads/.w"), "a root that moved"
    assert base != generation(entries, [{"id": "r1", "path": "/library"}, {"id": "r2", "path": "/x"}], "/downloads/.w")
    assert base != generation(entries, [{"id": "r1", "path": "/library"}], "/downloads/.other")
    assert base != generation(entries, [{"id": "r1", "path": "/library"}], None), "the working folder cleared"
    assert generation(entries, [{"id": "b", "path": "/b"}, {"id": "a", "path": "/a"}]) == generation(entries, [{"id": "a", "path": "/a"}, {"id": "b", "path": "/b"}])


def test_the_desired_state_is_the_providers_the_roots_and_the_resolved_working_folder(engine):
    install(engine, "lrclib")
    library, downloads = add_root(engine, "/library"), add_root(engine, "/downloads")
    store_folder(engine, "workspace_dir", downloads, ".musizeer-work")

    with Session(engine) as session:
        desired = desired_state(session)

    assert [e["name"] for e in desired.entries] == ["lrclib"]
    assert sorted(desired.roots, key=lambda r: r["path"]) == [{"id": downloads.id, "path": "/downloads"}, {"id": library.id, "path": "/library"}]
    assert desired.workspace == "/downloads/.musizeer-work"


def test_a_new_install_declares_nothing(engine):
    with Session(engine) as session:
        desired = desired_state(session)

    assert desired.roots == [] and desired.workspace is None


def test_a_working_folder_in_a_root_the_gateway_refused_is_not_pushed(engine):
    bad = add_root(engine, "/etc", state="refused", reason="not a mounted volume")
    store_folder(engine, "workspace_dir", bad, "work")

    with Session(engine) as session:
        assert roots.desired_folders(session)[1] is None


def test_a_push_sends_the_roots_and_the_working_folder_and_records_the_verdicts(engine, gateway, sync):
    good, bad = add_root(engine, "/downloads", state="unchecked"), add_root(engine, "/etc", state="unchecked")
    store_folder(engine, "workspace_dir", good, ".musizeer-work")
    gateway.refused_paths["/etc"] = "not a mounted volume"

    result = sync.push()

    assert result.pushed
    (push,) = gateway.pushes()
    assert sorted(push["roots"], key=lambda r: r["path"]) == [{"id": good.id, "path": "/downloads"}, {"id": bad.id, "path": "/etc"}]
    assert push["workspace"] == "/downloads/.musizeer-work"
    assert [r["state"] for r in sorted(result.roots, key=lambda r: r["path"])] == ["accepted", "refused"]
    assert verdict_of(engine, good.id) == ("accepted", "", True)
    assert verdict_of(engine, bad.id) == ("refused", "not a mounted volume", True)
    assert result.workspace["path"] == "/downloads/.musizeer-work"


def test_the_generation_pushed_is_the_one_the_gateway_reports_back(engine, gateway, sync):
    root = add_root(engine, "/downloads", state="unchecked")
    store_folder(engine, "workspace_dir", root, "w")
    sync.push()
    with Session(engine) as session:
        listing = sync.client.listing()
        assert listing.desired == desired_state(session).generation
    assert sync.ensure(listing) is False, "nothing to push: the gateway has the set"


def test_a_root_added_afterwards_makes_the_gateway_stale_and_ensure_pushes_it(engine, gateway, sync):
    sync.push()
    add_root(engine, "/library", state="unchecked")

    assert sync.ensure(sync.client.listing()) is True
    assert [r["path"] for r in gateway.pushes()[-1]["roots"]] == ["/library"]


def test_a_restarted_gateway_gets_the_roots_and_the_working_folder_again(engine, gateway, sync):
    root = add_root(engine, "/downloads", state="unchecked")
    store_folder(engine, "workspace_dir", root, "w")
    sync.push()
    gateway.restart()

    assert sync.ensure(sync.client.listing()) is True
    assert gateway.pushes()[-1]["workspace"] == "/downloads/w"


def test_the_verdicts_of_a_listing_are_recorded_and_a_root_that_becomes_usable_brings_the_working_folder(engine, gateway, sync):
    """The mount appeared after the first push: the root was refused and so the working folder was not pushed; the next
    listing shows the root accepted, the working folder is worth pushing, and the host pushes it."""
    root = add_root(engine, "/downloads", state="unchecked")
    store_folder(engine, "workspace_dir", root, "w")
    gateway.refused_paths["/downloads"] = "it is part of the container's own file system"
    sync.push()
    assert verdict_of(engine, root.id)[0] == "refused"
    assert sync.ensure(sync.client.listing()) is True and gateway.pushes()[-1]["workspace"] is None

    del gateway.refused_paths["/downloads"]
    gateway.roots = [{"id": root.id, "path": "/downloads"}]  # what the gateway holds, judged again by its next listing

    assert sync.ensure(sync.client.listing()) is True
    assert verdict_of(engine, root.id)[0] == "accepted"
    assert gateway.pushes()[-1]["workspace"] == "/downloads/w"


def test_a_gateway_that_does_not_answer_leaves_the_roots_unchecked(engine, gateway, sync):
    root = add_root(engine, "/library", state="unchecked")
    gateway.close()

    result = sync.push()

    assert not result.pushed and verdict_of(engine, root.id) == ("unchecked", "", False)

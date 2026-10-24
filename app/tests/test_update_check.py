"""
The version this instance runs and the newer releases of Musizeer (ADR-0007 §2 and §5.1): the rules
(core/versions.py), the check against GitHub (services/update_check.check, with recorded answers) and
the view of Administration (services/update_check.view, GET /admin/updates), which judges each newer
release against what runs now.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import main
from core.routines import enabled_setting_key, get_routine
from core.user_auth import create_access_token
from core.versions import (
    KIND_MAJOR,
    KIND_MINOR,
    KIND_PATCH,
    REASON_CONTRACT_MIGRATIONS,
    REASON_CONTRACT_UNSUPPORTED,
    REASON_MAJOR,
    REASON_MANUAL_STEPS,
    REASON_NO_METADATA,
    REASON_UPGRADE_PATH,
    InstalledContract,
    Version,
    assess_update,
    parse_version,
    update_kind,
)
from db.engine import get_session
from db.models import InstalledProvider, ProviderSource, RoutineRun, User
from routes import deps
from services import update_check
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import new_engine

NOW = datetime.now(timezone.utc) - timedelta(minutes=5)
API = "https://api.github.com/repos/sossiogll/musizeer/releases"


# ---- the rules -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1.4.0", Version(1, 4, 0)),
    ("v1.4.0", Version(1, 4, 0)),
    ("1.3.2+7.abc1234", Version(1, 3, 2)),  # a build of master ranks as the release it is based on
    ("dev", None),
    ("1.4.0-rc.1", None),  # a prerelease is never an update
    ("", None),
    (None, None),
])
def test_a_version_is_read_from_a_tag_or_a_build(text, expected):
    assert parse_version(text) == expected


def test_the_kind_of_an_update():
    assert update_kind(Version(1, 2, 3), Version(1, 2, 4)) == KIND_PATCH
    assert update_kind(Version(1, 2, 3), Version(1, 5, 0)) == KIND_MINOR
    assert update_kind(Version(1, 2, 3), Version(2, 0, 0)) == KIND_MAJOR


def test_an_update_that_only_needs_its_images_has_no_reasons():
    meta = {"provider_contracts": [5], "contract_migrations": False, "manual_steps": False}
    assert assess_update(Version(1, 2, 0), Version(1, 3, 0), meta, [InstalledContract("lyrics", 5)]) == []


def test_the_reasons_an_update_needs_care():
    meta = {"provider_contracts": [6], "min_upgrade_from": "1.9.0", "contract_migrations": True, "manual_steps": True}
    installed = [InstalledContract("lyrics", 5), InstalledContract("catalog", 6), InstalledContract("audio", 5)]
    reasons = assess_update(Version(1, 2, 0), Version(2, 0, 0), meta, installed)
    assert reasons == [
        {"code": REASON_MAJOR},
        {"code": REASON_UPGRADE_PATH, "through": "1.9.0"},
        {"code": REASON_CONTRACT_UNSUPPORTED, "providers": ["audio", "lyrics"]},
        {"code": REASON_CONTRACT_MIGRATIONS},
        {"code": REASON_MANUAL_STEPS},
    ]


def test_a_release_without_metadata_cannot_be_checked():
    assert assess_update(Version(1, 2, 0), Version(1, 3, 0), None, []) == [{"code": REASON_NO_METADATA}]


# ---- the check against GitHub -----------------------------------------------------------------------

def _release(tag, *, metadata=None, draft=False, prerelease=False):
    assets = [{"name": "release.json", "browser_download_url": f"https://github.com/dl/{tag}/release.json"}] if metadata else []
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "html_url": f"https://github.com/r/{tag}",
            "published_at": "2026-10-17T10:00:00Z", "assets": assets}


def _transport(releases, metadata=None, *, status=200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url).startswith(API):
            return httpx.Response(status, json=releases)
        tag = request.url.path.split("/")[2]
        return httpx.Response(200, json=(metadata or {})[tag])

    transport = httpx.MockTransport(handler)
    transport.seen = seen
    return transport


def test_the_check_keeps_the_newer_published_releases_with_their_metadata():
    meta = {"provider_contracts": [5], "contract_migrations": False, "manual_steps": False, "images": {"ignored": True}}
    releases = [
        _release("v1.2.0"), _release("v1.3.0", metadata=True), _release("v1.4.0-rc.1", prerelease=True),
        _release("v1.5.0", draft=True), _release("sdk-v5.5.0"), _release("v1.2.1"),
    ]
    transport = _transport(releases, {"v1.3.0": meta})

    result = update_check.check("1.2.0", transport=transport)

    assert result["status"] == "ok" and result["current"] == "1.2.0"
    assert [r["version"] for r in result["releases"]] == ["1.3.0", "1.2.1"]
    assert result["releases"][0]["meta"] == {"provider_contracts": [5], "contract_migrations": False, "manual_steps": False}
    assert result["releases"][1]["meta"] is None
    # Nothing about the instance travels: the version is not in any request.
    assert all("1.2.0" not in str(request.url) and "1.2.0" not in request.headers.get("user-agent", "")
               for request in transport.seen)
    assert len(json.dumps(result)) < 2000  # what a routine run keeps


def test_a_development_build_keeps_the_latest_release_only():
    result = update_check.check("dev", transport=_transport([_release("v1.2.0"), _release("v1.3.0")]))
    assert [r["version"] for r in result["releases"]] == ["1.3.0"]


def _answering(status, *, headers=None, body=None):
    return httpx.MockTransport(lambda request: httpx.Response(status, headers=headers or {}, json=body or {}))


@pytest.mark.parametrize("status, headers, reason", [
    (404, {}, "not_found"),                                        # the repository is private (for now) or renamed
    (401, {}, "unauthorized"),
    (403, {}, "unauthorized"),
    (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),         # GitHub's primary rate limit
    (403, {"retry-after": "60"}, "rate_limited"),                  # its secondary one
    (429, {}, "rate_limited"),
    (500, {}, "upstream_error"),
    (503, {}, "upstream_error"),
])
def test_a_feed_that_cannot_be_read_is_unavailable_not_an_error(status, headers, reason):
    result = update_check.check("1.2.0", transport=_answering(status, headers=headers, body={"message": "no"}))

    assert result["status"] == "ok" and result["state"] == "unavailable" and result["reason"] == reason
    assert result["releases"] == [] and result["current"] == "1.2.0"
    assert reason in update_check.UNAVAILABLE_REASONS


@pytest.mark.parametrize("error", [
    httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
    httpx.ConnectTimeout("timed out"),
    httpx.ReadError("connection reset"),
])
def test_a_network_without_access_to_github_is_unavailable(error):
    def refuse(request):
        raise error

    result = update_check.check("1.2.0", transport=httpx.MockTransport(refuse))

    assert (result["status"], result["state"], result["reason"]) == ("ok", "unavailable", "unreachable")


def test_a_readable_feed_says_it_was_checked():
    assert update_check.check("1.2.0", transport=_transport([_release("v1.2.0")]))["state"] == "checked"


@pytest.mark.parametrize("transport", [
    _answering(200, body={"message": "not a list"}),                                        # not a list of releases
    httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>proxy</html>")),  # not JSON
    _answering(422, body={"message": "invalid"}),                                           # a request GitHub refuses
])
def test_a_malformed_answer_is_still_an_error(transport):
    with pytest.raises(update_check.UpdateCheckError):
        update_check.check("1.2.0", transport=transport)


# ---- the view of Administration ----------------------------------------------------------------------

def _seed_run(session, result, *, status="ok", minutes_ago=10):
    at = NOW - timedelta(minutes=minutes_ago)
    session.add(RoutineRun(routine_key=update_check.ROUTINE, queued_at=at, started_at=at, finished_at=at,
                           status=status, detail=json.dumps(result) if status == "ok" else "unreachable"))
    session.commit()


def _install(session, name, contract):
    source = ProviderSource(name=f"src-{name}", url=f"https://catalog.test/{name}.json", added_at=NOW, enabled=False)
    session.add(source)
    session.flush()
    session.add(InstalledProvider(source_id=source.id, name=name, image=f"registry.test/{name}", digest="sha256:" + "a" * 64,
                                  provider_api_version=contract, label=name, roles='["lyrics"]', permissions="{}",
                                  approved_at=NOW, installed_at=NOW))
    session.commit()


RESULT = {"status": "ok", "current": "1.2.0", "releases": [
    {"version": "2.0.0", "url": "https://github.com/r/v2.0.0", "published_at": "2026-10-17T10:00:00Z",
     "meta": {"provider_contracts": [6], "contract_migrations": True}},
    {"version": "1.3.0", "url": "https://github.com/r/v1.3.0", "published_at": "2026-10-10T10:00:00Z",
     "meta": {"provider_contracts": [5], "contract_migrations": False, "manual_steps": False}},
]}


def test_each_newer_release_is_judged_against_what_runs_now():
    with Session(new_engine()) as session:
        _install(session, "lyrics", 5)
        _seed_run(session, RESULT)
        view = update_check.view(session, check_enabled=True, current="1.2.0")

    assert view["latest"] == "2.0.0" and view["development"] is False and view["checked_at"].endswith("+00:00")
    major, minor = view["updates"]
    assert minor == {"version": "1.3.0", "url": "https://github.com/r/v1.3.0", "published_at": "2026-10-10T10:00:00Z",
                     "kind": KIND_MINOR, "plain": True, "reasons": []}
    assert major["kind"] == KIND_MAJOR and major["plain"] is False
    assert [r["code"] for r in major["reasons"]] == [REASON_MAJOR, REASON_CONTRACT_UNSUPPORTED, REASON_CONTRACT_MIGRATIONS]


def test_a_release_no_longer_newer_than_what_runs_is_not_an_update():
    with Session(new_engine()) as session:
        _seed_run(session, RESULT)
        view = update_check.view(session, check_enabled=True, current="1.3.0")  # updated by hand since the check
    assert [u["version"] for u in view["updates"]] == ["2.0.0"]


def test_a_failed_check_keeps_the_last_answer_and_says_it_failed():
    with Session(new_engine()) as session:
        _seed_run(session, RESULT, minutes_ago=60)
        _seed_run(session, None, status="error", minutes_ago=1)
        view = update_check.view(session, check_enabled=True, current="1.2.0")
    assert view["last_check_failed"] is True
    assert [u["version"] for u in view["updates"]] == ["2.0.0", "1.3.0"]


def test_a_development_build_has_no_updates_but_knows_the_latest_release():
    with Session(new_engine()) as session:
        _seed_run(session, {"status": "ok", "current": "dev", "releases": [RESULT["releases"][0]]})
        view = update_check.view(session, check_enabled=True, current="dev")
    assert view["development"] is True and view["updates"] == [] and view["latest"] == "2.0.0"


UNAVAILABLE = {"status": "ok", "state": "unavailable", "reason": "not_found", "releases": [], "current": "1.2.0"}


def test_an_unavailable_feed_is_not_a_failed_check():
    with Session(new_engine()) as session:
        _seed_run(session, UNAVAILABLE)
        view = update_check.view(session, check_enabled=True, current="1.2.0")

    assert view["release_info_unavailable"] == "not_found"
    assert view["last_check_failed"] is False
    assert view["updates"] == [] and view["latest"] is None and view["checked_at"] is None


def test_an_unavailable_feed_keeps_the_last_answer_that_read_it():
    with Session(new_engine()) as session:
        _seed_run(session, RESULT, minutes_ago=60)
        _seed_run(session, UNAVAILABLE, minutes_ago=1)
        view = update_check.view(session, check_enabled=True, current="1.2.0")

    assert view["release_info_unavailable"] == "not_found" and view["last_check_failed"] is False
    assert [u["version"] for u in view["updates"]] == ["2.0.0", "1.3.0"]
    assert view["checked_at"] is not None


def test_a_feed_that_is_readable_again_is_not_reported_unavailable():
    with Session(new_engine()) as session:
        _seed_run(session, UNAVAILABLE, minutes_ago=60)
        _seed_run(session, RESULT, minutes_ago=1)
        view = update_check.view(session, check_enabled=True, current="1.2.0")

    assert view["release_info_unavailable"] is None


def test_never_checked():
    with Session(new_engine()) as session:
        view = update_check.view(session, check_enabled=False, current="1.2.0")
    assert view["checked_at"] is None and view["updates"] == [] and view["latest"] is None
    assert view["last_check_failed"] is False and view["check_enabled"] is False
    assert view["release_info_unavailable"] is None


# ---- the routes ----------------------------------------------------------------------------------------

@pytest.fixture
def client():
    engine = new_engine()
    with Session(engine) as session:
        admin = User(username="admin", email="admin@test", password_hash="x", role="admin", is_active=True)
        user = User(username="user", email="user@test", password_hash="x", role="user", is_active=True)
        session.add_all([admin, user])
        session.commit()
        tokens = {"admin": create_access_token(admin.id, admin.role), "user": create_access_token(user.id, user.role)}
        _seed_run(session, RESULT)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: FakeRegistry(),
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    test_client = TestClient(main.app, raise_server_exceptions=False)
    test_client.tokens = tokens
    test_client.config = config
    yield test_client
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)


def test_only_an_admin_sees_the_updates(client):
    assert client.get("/admin/updates", headers={"Authorization": f"Bearer {client.tokens['user']}"}).status_code == 403
    answer = client.get("/admin/updates", headers={"Authorization": f"Bearer {client.tokens['admin']}"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["current"] == "dev" and body["development"] is True and body["latest"] == "2.0.0"


def test_the_check_is_on_by_default_and_its_switch_is_the_routine(client):
    routine = get_routine(update_check.ROUTINE)
    assert routine is not None and routine.default_enabled is True
    assert routine.task_name == "workers.maintenance.check_for_updates"
    headers = {"Authorization": f"Bearer {client.tokens['admin']}"}
    assert client.get("/admin/updates", headers=headers).json()["check_enabled"] is True
    client.config.update({enabled_setting_key(update_check.ROUTINE): False})
    assert client.get("/admin/updates", headers=headers).json()["check_enabled"] is False


def test_health_tells_the_version_to_admins_only(client):
    assert "version" not in client.get("/health").json()
    assert "version" not in client.get("/health", headers={"Authorization": f"Bearer {client.tokens['user']}"}).json()
    body = client.get("/health", headers={"Authorization": f"Bearer {client.tokens['admin']}"}).json()
    assert body["version"] == "dev" and body["commit"] == ""

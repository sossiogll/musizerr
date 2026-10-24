"""
Is there a newer Musizeer to update to, and what would updating imply (ADR-0007 §5.1).

The routine ``check_for_updates`` (on by default, once a day) reads the releases of the repository on
GitHub and keeps, as the result of its run, the facts: the releases newer than this version, each with
the ``release.json`` it carries. Nothing about the instance is sent. The admin's view
(``GET /admin/updates``) reads that result and judges each release against what runs NOW (the providers
installed may have changed since the check): a release that only needs its images pulled, or the reasons
it needs care. Musizeer is always updated by hand; nothing here changes the instance.

A release feed that cannot be read is not a failure of the instance: GitHub answering 404/401/403 (the
repository is private or renamed), a rate limit, an outage or a network without access to GitHub give a run
that ends ``ok`` with ``state: "unavailable"`` and a ``reason`` (``UNAVAILABLE_REASONS``), shown in
Administration as "release information unavailable". Only an answer that is not what a release list is
(malformed JSON, not a list) is an error.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from sqlmodel import Session, select

from core.utc import utc_iso
from core.versions import InstalledContract, assess_update, parse_version, update_kind
from db.models import InstalledProvider
from db.routine_run_store import STATUS_ERROR, STATUS_OK, finished_runs, latest_run
from services import app_version
from services.schema_status import schema_status
from services.routines import run_result

ROUTINE = "check_for_updates"
# The repository whose releases are this instance's updates; a fork names its own.
REPOSITORY = os.environ.get("MUSIZERR_UPDATE_REPOSITORY") or "sossiogll/musizeer"
RELEASE_METADATA = "release.json"
# The routine keeps its result in a run's detail (2000 characters): the newest few releases are enough
# to say "there is an update", and each one links its notes.
MAX_RELEASES = 5
_META_FIELDS = ("provider_contracts", "min_upgrade_from", "contract_migrations", "manual_steps")
_TIMEOUT = 15.0
# How many finished runs back the view looks for the last one that read the feed.
_RUNS_LOOKED_AT = 10

# The ``state`` of a check's result: the feed was read, or it could not be (and why).
STATE_CHECKED = "checked"
STATE_UNAVAILABLE = "unavailable"
REASON_NOT_FOUND = "not_found"            # 404: the repository is private (for now) or renamed
REASON_UNAUTHORIZED = "unauthorized"      # 401/403 that is not a rate limit
REASON_RATE_LIMITED = "rate_limited"      # 429, or 403 with the rate-limit headers
REASON_UNREACHABLE = "unreachable"        # the network: no route to GitHub, DNS, a timeout
REASON_UPSTREAM_ERROR = "upstream_error"  # GitHub answered 5xx
UNAVAILABLE_REASONS = (
    REASON_NOT_FOUND, REASON_UNAUTHORIZED, REASON_RATE_LIMITED, REASON_UNREACHABLE, REASON_UPSTREAM_ERROR,
)


class UpdateCheckError(RuntimeError):
    """GitHub could not be asked, or answered something else than a list of releases."""


def unavailable_reason(response: httpx.Response) -> str | None:
    """Why the answer of the releases API says "cannot read the feed right now", or None for an answer to read
    (a list, or something that is not one and is an error)."""
    status = response.status_code
    if status == 429:
        return REASON_RATE_LIMITED
    if status == 403 and (response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers):
        return REASON_RATE_LIMITED
    if status in (401, 403):
        return REASON_UNAUTHORIZED
    if status == 404:
        return REASON_NOT_FOUND
    if status >= 500:
        return REASON_UPSTREAM_ERROR
    return None


def _unavailable(current: str, reason: str) -> dict[str, Any]:
    return {
        "status": "ok", "state": STATE_UNAVAILABLE, "reason": reason,
        "repository": REPOSITORY, "current": current, "releases": [],
    }


def check(current: str = app_version.VERSION, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """The result of the routine: the releases newer than ``current`` (newest first), each with its
    metadata. A development build (no version) keeps the latest release only, for information.

    A feed that cannot be read (see the module docstring) is ``state: "unavailable"`` with its ``reason``
    and no releases; an answer that is not a list of releases raises :class:`UpdateCheckError`."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Musizeer"}
    with httpx.Client(transport=transport, timeout=_TIMEOUT, headers=headers, follow_redirects=True) as client:
        try:
            response = client.get(f"https://api.github.com/repos/{REPOSITORY}/releases", params={"per_page": 30})
        except httpx.TransportError:
            return _unavailable(current, REASON_UNREACHABLE)
        except httpx.HTTPError as exc:
            raise UpdateCheckError(f"the releases of {REPOSITORY} could not be read: {exc}") from exc
        reason = unavailable_reason(response)
        if reason is not None:
            return _unavailable(current, reason)
        try:
            response.raise_for_status()
            listed = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpdateCheckError(f"the releases of {REPOSITORY} could not be read: {exc}") from exc
        if not isinstance(listed, list):
            raise UpdateCheckError(f"the releases of {REPOSITORY} are not a list")

        releases = sorted(
            (release for release in (_release(item) for item in listed) if release is not None),
            key=lambda release: release["_version"], reverse=True,
        )
        running = parse_version(current)
        newer = [r for r in releases if running is None or r["_version"] > running]
        newer = newer[:1] if running is None else newer[:MAX_RELEASES]
        for release in newer:
            release["meta"] = _metadata(client, release.pop("_metadata_url"))
    return {
        "status": "ok",
        "state": STATE_CHECKED,
        "repository": REPOSITORY,
        "current": current,
        "releases": [{key: value for key, value in r.items() if not key.startswith("_")} for r in newer],
    }


def _release(item: Any) -> dict[str, Any] | None:
    """A published release with a version tag; None for a draft, a prerelease or another tag."""
    if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
        return None
    version = parse_version(item.get("tag_name"))
    if version is None:
        return None
    assets = item.get("assets") if isinstance(item.get("assets"), list) else []
    metadata_url = next((a.get("browser_download_url") for a in assets
                         if isinstance(a, dict) and a.get("name") == RELEASE_METADATA), None)
    return {
        "version": str(version), "_version": version, "_metadata_url": metadata_url,
        "url": item.get("html_url"), "published_at": item.get("published_at"),
    }


def _metadata(client: httpx.Client, url: str | None) -> dict[str, Any] | None:
    """The fields of a release's ``release.json`` the assessment reads; None when it has none or it
    cannot be read (the release is then judged without it: its contracts cannot be checked)."""
    if not url:
        return None
    try:
        response = client.get(url)
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    return {key: document[key] for key in _META_FIELDS if key in document}


def view(session: Session, *, check_enabled: bool, current: str = app_version.VERSION) -> dict[str, Any]:
    """What Administration shows: the version that runs, whether the check is on, when it last
    answered, and each newer release with what updating to it implies now. Reads only."""
    # The last answer GitHub gave stays shown when a later check failed (last_check_failed says so) or could
    # not read the feed (release_info_unavailable says why): the newest run that DID read it is the one shown.
    last = latest_run(session, ROUTINE)
    ok_run, result, unavailable = None, None, None
    for run in finished_runs(session, ROUTINE, statuses=(STATUS_OK,), limit=_RUNS_LOOKED_AT):
        candidate = run_result(run.detail)
        if isinstance(candidate, dict) and candidate.get("state") == STATE_UNAVAILABLE:
            if unavailable is None:
                reason = candidate.get("reason")
                unavailable = reason if reason in UNAVAILABLE_REASONS else REASON_UNREACHABLE
            continue
        ok_run, result = run, candidate
        break
    running = parse_version(current)
    installed = [InstalledContract(row.name, row.provider_api_version)
                 for row in session.exec(select(InstalledProvider)).all()]

    listed = result.get("releases", []) if isinstance(result, dict) else []
    updates = []
    for release in listed:
        target = parse_version(release.get("version")) if isinstance(release, dict) else None
        # Only what is newer than what runs NOW: the check may predate an update made since.
        if target is None or running is None or not target > running:
            continue
        reasons = assess_update(running, target, release.get("meta"), installed)
        updates.append({key: release.get(key) for key in ("version", "url", "published_at")}
                       | {"kind": update_kind(running, target), "plain": not reasons, "reasons": reasons})

    return {
        "current": current,
        "commit": app_version.COMMIT,
        # The schema the database is at and what migrated it (ADR-0007 §2), or None when it cannot be read.
        "schema": schema_status(session.get_bind()),
        "development": running is None,
        "check_enabled": check_enabled,
        "checked_at": utc_iso(ok_run.finished_at) if ok_run is not None else None,
        "last_check_failed": bool(last is not None and last.status == STATUS_ERROR),
        # Why the releases could not be read at the last check that answered (None: they could, or it never ran).
        "release_info_unavailable": unavailable,
        "updates": updates,
        # The newest release the check saw (for a development build too, which has nothing to compare).
        "latest": next((r.get("version") for r in listed if isinstance(r, dict)), None),
    }

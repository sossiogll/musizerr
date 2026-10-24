"""
The host's side of scripts/test-provider-infra-integration.sh: in a container of the application image, with the
gateway's socket volume and the library volume mounted where the application has them. It uses the application's own
code (``ProviderRegistry``, ``HostProviderContext``, the settings table, the grants) against the REAL gateway and the
sample provider image (gateway/tests/fixtures/infra-provider), and checks, printing a marker line for each:

  WILDCARD_OK   a subdomain of the wildcard is reachable, the domain itself, another port and another domain are not;
  ROOTS_OK      the root folders the host declares are judged by the gateway's own mountinfo (a mounted volume is accepted,
                /etc and a missing folder are refused), the picker lists the volume, and the working folder gives the
                provider its /workspace;
  FS_BEFORE_OK  until a folder is granted the sandbox has none of the host's folders;
  FS_RW_OK      a granted folder is bound at the same path, writable (and the file is on the host); a read-only one
                is mounted read-only and refuses a write;
  FS_STABLE_OK  the same grant again does not restart the provider;
  FS_CHANGE_OK  a changed setting restarts the sandbox with the new bind and without the old;
  FS_REFUSED_OK a path outside the accepted roots, a link out of the root, an unnormalized path, a file and a missing
                folder are refused by the gateway even when a grant is sent that the host would never resolve, with the
                reason in the gateway's listing, and nothing is mounted;
  FS_MASK_OK    granting the folder that holds the workspace directory hides what is in it;
  TCP_OK        ftplib lists and downloads through the proxy: control connection and passive data connection on a port
                the server chose; a setting whose manifest allows only the control port gets the data port refused.

Exits non-zero on the first thing that is wrong.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MUSIZEER_DATA_DIR", "/tmp/data")

from db.engine import engine  # noqa: E402
from services.database_init import init_db  # noqa: E402
from sqlmodel import Session  # noqa: E402

from core.settings_resolver import namespace_provider_key  # noqa: E402
from db import root_store  # noqa: E402
from db.settings_store import SettingsRepository  # noqa: E402
from services.gateway_sync import GatewaySync  # noqa: E402
from musizeer_sdk.models import Track  # noqa: E402
from services.config_manager import get_config  # noqa: E402
from services.gateway_client import GatewayClient  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402
from musizeer_sdk.auth import AuthSession  # noqa: E402

SESSION = AuthSession.anonymous()
LIB = Path("/downloads/lib")


def say(marker: str, **fields) -> None:
    print(marker, json.dumps(fields) if fields else "", flush=True)


def fail(message: str) -> None:
    print("FAILED:", message, flush=True)
    sys.exit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def set_setting(key: str, value: str) -> None:
    with Session(engine) as session:
        SettingsRepository(session).set_system(namespace_provider_key("infra", key), value)
        session.commit()
    get_config()._cache_loaded_at = 0.0  # noqa: SLF001 — read the settings now, not after the cache window


def set_folder(key: str, root_id: str, path: str) -> None:
    """A folder setting of the provider: a root folder and a path inside it (ADR-0008)."""
    set_setting(key, {"root_id": root_id, "path": path})


def clear_folder(key: str) -> None:
    set_setting(key, None)


def grant_paths(paths: dict[str, str]) -> dict:
    """Send the gateway a grant the host would never make (a folder it would not resolve): the gateway judges it by itself."""
    reply = GatewayClient().request({"op": "egress", "provider": "infra", "targets": [], "ttl": 60, "paths": paths})
    return reply["paths"]


def settle(provider) -> None:
    """Wait until the provider answers again after the gateway restarted it to change its mounts."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            ask(provider, "fs")
            return
        except Exception:  # noqa: BLE001 — it is restarting
            time.sleep(1)
    fail("the provider did not come back after its mounts changed")


def declare_roots() -> tuple[str, str]:
    """The administrator's part: the root folders and the working folder, pushed to the gateway like the host does."""
    with Session(engine) as session:
        downloads = root_store.create(session, "Downloads", "/downloads")
        nowhere = root_store.create(session, "Etc", "/etc")
        missing = root_store.create(session, "Missing", "/downloads/not-there")
        SettingsRepository(session).set_system("workspace_dir", {"root_id": downloads.id, "path": ".musizeer-work"})
        session.commit()
        ids = (downloads.id, nowhere.id, missing.id)
    get_config()._cache_loaded_at = 0.0  # noqa: SLF001
    with Session(engine) as session:
        result = GatewaySync(GatewayClient()).push(session)
    expect(result.pushed, f"the gateway did not take the folders: {result.error}")
    return ids[0], json.dumps({"roots": result.roots, "workspace": result.workspace})


def ask(provider, command: str) -> str:
    return provider.get_lyrics(Track(title=command, artist="x", album="y", duration_ms=1000), SESSION).plain


def fs(provider) -> dict:
    return json.loads(ask(provider, "fs"))


def gateway_paths() -> dict:
    listing = GatewayClient().request({"op": "list"})
    (infra,) = [p for p in listing["providers"] if p["name"] == "infra"]
    return infra["folders"]


def main() -> None:
    init_db()
    registry = ProviderRegistry()
    provider = registry.get_lyrics_provider("infra")
    if provider is None:
        fail(f"infra is not in the registry: {registry.lyrics_names()} {registry.rejected()}")

    # ---- wildcard egress ----------------------------------------------------------------
    results = {target: ask(provider, f"connect:{target}") for target in (
        "www.example.com:443", "example.com:443", "www.example.com:80", "evil.example.org:443", "a.b.example.com:443",
    )}
    expect(results["www.example.com:443"] == "OK", f"a subdomain of the wildcard is not reachable: {results}")
    for refused in ("example.com:443", "www.example.com:80", "evil.example.org:443"):
        expect("ProxyRefused" in results[refused] and "not allowed" in results[refused], f"{refused} was not refused: {results[refused]!r}")
    say("WILDCARD_OK", results=results)

    # ---- root folders: the gateway judges them by its own mounts -------------------------
    # (the provider has been running since the install, before any folder was declared: it has no workspace yet)
    downloads_id, summary = declare_roots()
    listing = GatewayClient().request({"op": "list"})
    by_path = {root["path"]: root for root in listing["roots"]}
    expect(by_path["/downloads"]["state"] == "accepted" and by_path["/downloads"]["writable"], f"/downloads is not accepted: {by_path}")
    expect(by_path["/downloads"]["mount_point"] == "/downloads" and by_path["/downloads"]["device"], f"no mount reported for /downloads: {by_path}")
    expect(by_path["/etc"]["state"] == "refused" and "not a mounted volume" in by_path["/etc"]["reason"], f"/etc was not refused as part of the image: {by_path}")
    expect(by_path["/downloads/not-there"]["state"] == "refused" and "cannot be read" in by_path["/downloads/not-there"]["reason"], f"a missing folder: {by_path}")
    expect(listing["workspace"]["state"] == "accepted" and listing["workspace_root"] == "/downloads/.musizeer-work", f"the working folder: {listing['workspace']}")
    inspected = GatewayClient().inspect("/downloads", write_test=True)
    expect(inspected["mounted"] and inspected["writable"] and inspected["write_test"]["ok"], f"inspect: {inspected}")
    expect(GatewayClient().inspect("/etc")["mounted"] is False, "/etc is a mounted volume?")
    volumes = [entry["path"] for entry in GatewayClient().browse(None)["directories"]]
    expect("/downloads" in volumes and "/etc" not in volumes and "/proc" not in volumes and not any(v.startswith("/run/musizeer") for v in volumes), f"volumes: {volumes}")
    (Path("/downloads") / "lib").mkdir(exist_ok=True)
    picked = GatewayClient().browse("/downloads")
    expect("lib" in [entry["name"] for entry in picked["directories"]] and picked["parent"] is None, f"browse: {picked}")
    try:
        GatewayClient().browse("/etc")
    except Exception as exc:  # noqa: BLE001
        expect("not a mounted volume" in str(exc), f"browse /etc was refused for another reason: {exc}")
    else:
        fail("the gateway browsed /etc")
    # the provider restarts with the working folder: its /workspace is its part of it
    settle(provider)
    expect(Path("/downloads/.musizeer-work/infra").is_dir(), "the gateway did not make the workspace of the provider in the working folder")
    say("ROOTS_OK", summary=json.loads(summary)["workspace"], volumes=volumes)

    # ---- folders: none before a grant ---------------------------------------------------
    before = fs(provider)
    expect(before["dirs"] == {} and all(listing is None for listing in before["probes"].values()) and before["downloads"] is None, f"folders before any grant: {before}")
    say("FS_BEFORE_OK", instance=before["instance"])

    # ---- folders: a read-write and a read-only one --------------------------------------
    for name in ("rw", "rw2", "ro"):
        (LIB / name).mkdir(parents=True, exist_ok=True)
    (LIB / "ro" / "seed.txt").write_text("seeded on the host")
    (LIB / "escape").unlink(missing_ok=True)
    (LIB / "escape").symlink_to("/etc")
    set_folder("rw_dir", downloads_id, "lib/rw")
    set_folder("ro_dir", downloads_id, "lib/ro")
    started = time.monotonic()
    granted = fs(provider)
    took = time.monotonic() - started
    rw, ro = granted["dirs"]["rw_dir"], granted["dirs"]["ro_dir"]
    expect(granted["instance"] != before["instance"], "the provider was not restarted to get its folders")
    expect(rw["is_dir"] and rw["write"] == "ok" and "rw" in rw["mount"].split(","), f"the read-write folder: {rw}")
    expect((LIB / "rw" / "infra-was-here").read_text() == "written from the sandbox", "the file written in the sandbox is not on the host")
    expect(ro["is_dir"] and ro["listing"] == ["seed.txt"] and "ro" in ro["mount"].split(","), f"the read-only folder: {ro}")
    expect(ro["write"].startswith("OSError") and "Read-only" in ro["write"], f"the read-only folder accepted a write: {ro['write']}")
    expect(not (LIB / "ro" / "infra-was-here").exists(), "a file appeared in the read-only folder on the host")
    probes = granted["probes"]
    expect(probes[str(LIB / "rw")] == ["infra-was-here"] and probes[str(LIB / "ro")] == ["seed.txt"] and not probes[str(LIB / "rw2")], f"probes: {probes}")
    expect(gateway_paths()["refused"] == {}, f"refusals: {gateway_paths()}")
    say("FS_RW_OK", restart_and_call_seconds=round(took, 1), rw_mount=rw["mount"], ro_mount=ro["mount"], ro_write=ro["write"])

    again = fs(provider)
    expect(again["instance"] == granted["instance"], "the same grant restarted the provider")
    set_folder("rw_dir", downloads_id, "lib/rw")
    expect(fs(provider)["instance"] == granted["instance"], "the same value written again restarted the provider")
    say("FS_STABLE_OK", instance=again["instance"])

    set_folder("rw_dir", downloads_id, "lib/rw2")
    changed = fs(provider)
    expect(changed["instance"] != granted["instance"], "a changed setting did not restart the sandbox")
    expect(changed["probes"][str(LIB / "rw2")] == ["infra-was-here"] and not changed["probes"][str(LIB / "rw")], f"the old bind is still there: {changed['probes']}")
    expect(changed["dirs"]["rw_dir"]["write"] == "ok" and (LIB / "rw2" / "infra-was-here").exists(), "the new folder is not writable")
    say("FS_CHANGE_OK", old=granted["instance"], new=changed["instance"])

    # ---- folders: what is refused -------------------------------------------------------
    refusals = {}
    # The host resolves a folder from a root and a clean relative path and would never send these: they are sent straight
    # to the gateway, which judges every grant by itself against the roots it accepted.
    for label, value, reason in (
        ("outside the roots", "/etc", "not inside a root folder"),
        ("a link out of the root", str(LIB / "escape"), "leads out"),
        ("not normalized", f"{LIB}/rw/../../../etc", "normalized"),
        ("not a directory", str(LIB / "ro" / "seed.txt"), "not a directory"),
        ("missing", str(LIB / "missing"), "cannot be read"),
    ):
        grant_paths({"rw_dir": value, "ro_dir": str(LIB / "ro")})
        report = fs(provider)
        refused = gateway_paths()["refused"].get("rw_dir", "")
        expect(reason in refused, f"{label}: the gateway says {refused!r}, not {reason!r}")
        expect(not report["probes"][str(LIB / "rw2")], f"{label}: the previous folder is still bound")
        expect(report["dirs"].get("ro_dir", {}).get("is_dir"), f"{label}: the other folder was lost")
        refusals[label] = refused
    say("FS_REFUSED_OK", refusals=refusals)

    # ---- folders: the workspace directory is hidden inside a granted folder -------------
    workspace_on_host = sorted(p.name for p in Path("/downloads/.musizeer-work").iterdir())
    expect("infra" in workspace_on_host, f"the provider's workspace folder is not on the host: {workspace_on_host}")
    set_folder("rw_dir", downloads_id, "")  # the root itself: it holds the working folder
    masked = fs(provider)
    expect(masked["downloads"] is not None and "lib" in masked["downloads"], f"the library root is not mounted: {masked['downloads']}")
    expect(masked["work"] == [], f"the workspace directory is visible inside the granted folder: {masked['work']}")
    expect(masked["dirs"]["rw_dir"]["write"] == "ok", "the library root is not writable")
    (Path("/downloads") / "infra-was-here").unlink(missing_ok=True)
    say("FS_MASK_OK", on_host=workspace_on_host, in_sandbox=masked["work"])

    # ---- raw TCP: ftplib through the proxy ------------------------------------------------
    clear_folder("rw_dir")
    set_setting("ftp_host", "ftp-fake")
    set_setting("ftp_ctl", "ftp-fake-ctl")  # the same server by another name: the proxy decides by host, a setting's ports are for the host it names
    outcome = ask(provider, "ftp:ftp_host")
    try:
        document = json.loads(outcome)
    except ValueError:
        fail(f"ftp through the proxy failed: {outcome}")
    port = int(document["content"].rsplit(" ", 1)[1])
    expect(document["names"] == ["hello.txt"] and 30000 <= port <= 30100, f"ftp: {document}")
    limited = ask(provider, "ftp:ftp_ctl")
    expect("ProxyRefused" in limited and "not allowed" in limited, f"the data port was reachable though only the control port is declared: {limited!r}")
    set_setting("ftp_host", "")
    nothing = ask(provider, "ftp:ftp_host")
    expect(nothing.startswith(("KeyError", "ValueError")) or "ProxyRefused" in nothing, f"ftp still works with the setting emptied: {nothing!r}")
    say("TCP_OK", names=document["names"], data_port=port, control_only=limited[:90])


if __name__ == "__main__":
    main()

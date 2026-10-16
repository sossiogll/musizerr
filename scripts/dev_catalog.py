"""
Write or update an entry of a development catalog (docs/catalog.md, "Development").

Standard library only, so that it runs in a bare ``python:3.12-slim`` container (scripts/dev-catalog.sh does it):
the host does not run Python. It is given what the build of an image produced: the image name, the digest the
registry returned and the labels of the image (scripts/build-provider-image.sh, ``PROVIDER_LABELS_OUT``). The
entry is made of those: roles, API version and permissions from the ``org.musizerr.provider.*`` labels (the very
ones the gateway checks), the display name from ``org.musizerr.provider.label``, the description and the
homepage from the standard ``org.opencontainers.image.description`` and ``.url`` labels when the Dockerfile sets
them. An entry of the same name is replaced where it stands (its digest moves on); a new one is appended.

    python dev_catalog.py --catalog data/catalogs/registry.json --image oci-registry:5000/musizerr-provider-x \\
        --digest sha256:... --labels labels.txt [--catalog-name "Musizerr development catalog"]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

CATALOG_VERSION = 2
DEFAULT_CATALOG_NAME = "Musizerr development catalog"
PREFIX = "org.musizerr.provider."


def read_labels(text: str) -> dict[str, str]:
    """``key=value`` lines (the first ``=`` separates; a value may contain others)."""
    labels: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            labels[key] = value
    return labels


def entry_from_labels(labels: dict[str, str], image: str, digest: str) -> dict:
    """The catalog entry for ``image`` at ``digest``, described by ``labels``."""
    missing = [key for key in ("name", "roles", "api-version", "permissions") if PREFIX + key not in labels]
    if missing:
        raise SystemExit("the labels of the image lack: " + ", ".join(PREFIX + key for key in missing))
    name = labels[PREFIX + "name"]
    try:
        api_version = int(labels[PREFIX + "api-version"])
        permissions = json.loads(labels[PREFIX + "permissions"])
    except ValueError as exc:
        raise SystemExit(f"the labels of the image are not readable: {exc}") from exc
    entry = {
        "name": name,
        "image": image,
        "digest": digest,
        "provider_api_version": api_version,
        "roles": [role for role in labels[PREFIX + "roles"].split(",") if role],
        "label": labels.get(PREFIX + "label") or name,
        "description": labels.get("org.opencontainers.image.description", ""),
    }
    homepage = labels.get("org.opencontainers.image.url", "")
    if homepage.startswith(("http://", "https://")):
        entry["homepage"] = homepage
    entry["permissions"] = permissions
    return entry


def merge(catalog: dict | None, entry: dict, catalog_name: str = DEFAULT_CATALOG_NAME) -> dict:
    """``catalog`` (or a new one) with ``entry`` in it: replacing the entry of the same name where it stands,
    else appended. A document that is not a version 2 catalog is refused, never overwritten."""
    if catalog is None:
        catalog = {"name": catalog_name, "version": CATALOG_VERSION, "providers": []}
    if not isinstance(catalog, dict) or catalog.get("version") != CATALOG_VERSION or not isinstance(catalog.get("providers"), list):
        raise SystemExit(f"the existing file is not a version {CATALOG_VERSION} catalog; not touching it")
    providers = list(catalog["providers"])
    for index, current in enumerate(providers):
        if isinstance(current, dict) and current.get("name") == entry["name"]:
            providers[index] = entry
            break
    else:
        providers.append(entry)
    return {**catalog, "providers": providers}


def write_atomically(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(document, out, indent=2, ensure_ascii=False)
            out.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or update a development catalog entry from an image's labels.")
    parser.add_argument("--catalog", required=True, type=Path, help="the catalog file (created if missing)")
    parser.add_argument("--image", required=True, help="registry/repository, as the gateway reaches it (no tag)")
    parser.add_argument("--digest", required=True)
    parser.add_argument("--labels", required=True, type=Path, help="key=value lines: the image's labels")
    parser.add_argument("--catalog-name", default=DEFAULT_CATALOG_NAME, help="the name of the catalog, when this creates it")
    args = parser.parse_args(argv)

    entry = entry_from_labels(read_labels(args.labels.read_text(encoding="utf-8")), args.image, args.digest)
    existing = json.loads(args.catalog.read_text(encoding="utf-8")) if args.catalog.exists() else None
    replaced = existing is not None and any(isinstance(p, dict) and p.get("name") == entry["name"] for p in existing.get("providers", []))
    write_atomically(args.catalog, merge(existing, entry, args.catalog_name))
    print(f"{'updated' if replaced else 'added'} {entry['name']} -> {args.image}@{args.digest} in {args.catalog}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

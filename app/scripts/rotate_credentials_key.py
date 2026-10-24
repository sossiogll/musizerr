"""
Seals the stored credentials again with the current MUSIZEER_CREDENTIALS_KEY (docs/credentials.md): the last
step of rotating the key, after the new key became MUSIZEER_CREDENTIALS_KEY and the one it replaces was kept in
MUSIZEER_CREDENTIALS_KEY_PREVIOUS. Without ``--apply`` it is a dry run: the database does not change, the
report says how many values it would seal again.

Uso, dalla cartella ``app/``::

    python -m scripts.rotate_credentials_key            # dry run
    python -m scripts.rotate_credentials_key --apply    # seals again with the current key

Everything it prints is a count: never a credential, a key or a sealed text. A value that no
configured key opens (``unreadable``), or that is stored in clear where it never should be
(``in_clear``), is left as it is and makes the exit status 1; the same for a row somebody changed
while the command ran (``skipped``: run it again). Once every table says ``unreadable 0`` and
``in_clear 0``, nothing is sealed with an old key: remove it from MUSIZEER_CREDENTIALS_KEY_PREVIOUS.

Exit status: 0 done (or a clean dry run), 1 done but something was left as it is, 2 not done (the key
is missing or malformed).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import credential_cipher  # noqa: E402
from db.engine import atomic_session  # noqa: E402
from services.credentials_rotation import rotate  # noqa: E402


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="seal the values again (default: dry run)")
    args = parser.parse_args(argv)
    out = out or sys.stdout

    try:
        credential_cipher.require_credentials_key()
    except credential_cipher.CredentialsKeyError as refused:
        print(f"Not done: {refused}", file=out)
        return 2

    with atomic_session(commit=args.apply) as session:
        reports = rotate(session, apply=args.apply)

    print("Dry run: nothing was written." if not args.apply else "Applied.", file=out)
    verb = "sealed again" if args.apply else "to seal again"
    for report in reports:
        print(
            f"{report.table}: {report.current} with the current key, {report.rewritten} {verb}, "
            f"{report.unreadable} unreadable, {report.in_clear} in clear, {report.skipped} skipped",
            file=out,
        )
    if any(report.needs_attention for report in reports):
        print("Some values were left as they are (unreadable, in clear or changed meanwhile): see docs/credentials.md.", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

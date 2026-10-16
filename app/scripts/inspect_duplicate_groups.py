"""
Riepilogo in sola lettura dei gruppi di duplicati di libreria: conteggi per
stato, membri cancellabili (messi in libreria da musizerr) e tre gruppi di
esempio con keeper e qualità di ogni membro.

    docker exec musizerr-app-1 python scripts/inspect_duplicate_groups.py
"""
from __future__ import annotations
import sys
from pathlib import Path

from sqlmodel import Session, func, select

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import engine
from db.models import DuplicateGroup, DuplicateMember

with Session(engine) as session:
    total = session.exec(select(func.count()).select_from(DuplicateGroup)).one()
    by_status = {}
    for status in ("proposed", "needs_review", "resolved", "dismissed"):
        c = session.exec(select(func.count()).select_from(DuplicateGroup).where(DuplicateGroup.status == status)).one()
        by_status[status] = c
    print("total groups:", total, by_status)

    deletable_members = session.exec(
        select(func.count()).select_from(DuplicateMember).where(DuplicateMember.origin == "musizerr", DuplicateMember.download_id.is_not(None))
    ).one()
    total_members = session.exec(select(func.count()).select_from(DuplicateMember)).one()
    print("deletable members:", deletable_members, "/", total_members)

    print("\n--- 3 example groups ---")
    groups = session.exec(select(DuplicateGroup).limit(60)).all()
    shown = 0
    for g in groups:
        members = session.exec(select(DuplicateMember).where(DuplicateMember.group_id == g.id)).all()
        print(f"\nGroup {g.id[:8]} recording_id={g.recording_id} status={g.status} reason={g.reason}")
        for m in members:
            print(f"  [{'KEEPER' if m.is_keeper else 'loser '}] {m.title!r} - {m.artist!r} | virtual_path={m.path!r} | "
                  f"suffix={m.suffix} bitrate={m.bitrate} sr={m.sample_rate} | origin={m.origin} download_id={m.download_id}")
        shown += 1
        if shown >= 3:
            break

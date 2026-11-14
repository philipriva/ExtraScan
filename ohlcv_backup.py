#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly BACKUP of the OHLCV store (no Yahoo/Massive re-download).

Why not Drive files.copy: copies made by the service account would be owned by the
SA, which has no Drive quota (copy fails). Instead two owner-created slot files in
warehouse/ohlcv_store_backup are overwritten alternately (oldest first) with ONE
uncompressed tar of the current shards + manifest, so only the last 2 backups are
kept (~630 MB each, Philip's quota). The slot file is renamed
ohlcv_store_backup_YYYY-MM-DD_slotX.tar (YYYY-MM-DD = backup date, Asia/Tokyo).

Integrity: every shard md5/size == source manifest before packing; uploaded tar md5
(Drive md5Checksum) == local tar md5; member count == 17.
Also: optional one-time `revoke-public` removes the 'anyone with link' permission on
the warehouse folder (Philip 2026-11-14) and prints remaining permissions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ohlcv_store as st  # noqa: E402

BACKUP_FOLDER = "1P7SbYvxIZ5p0SYaeHAbYl0Nt71l3-e4S"
SLOTS = {"A": "1VLkqqAJkTbXU2hfpjQ2StoWG40QPo0uz", "B": "1jqfaRKtGM9oh-pZSB12dWVGtc4854H3u"}
WAREHOUSE = "1cpbLQIzDOvpLAIbpAxkTdQVo9TBbcGyQ"
NAME_RE = re.compile(r"ohlcv_store_backup_(\d{4}-\d{2}-\d{2})_slot[AB]\.tar$")


def slot_date(name: str):
    m = NAME_RE.search(name or "")
    return date.fromisoformat(m.group(1)) if m else None


def pick_slot(names: dict, today: date, force: bool = False):
    """names: {slot: filename}. Returns (slot or None if skip, reason)."""
    dates = {s: slot_date(n) for s, n in names.items()}
    recent = [d for d in dates.values() if d and (today - d).days < 6]
    if recent and not force:
        return None, f"backup this week exists ({max(recent)})"
    oldest = sorted(dates, key=lambda s: (dates[s] is not None, dates[s] or date.min, s))[0]
    return oldest, "ok"


def cmd_backup(a) -> int:
    sa = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    drv = st.Drive(sa, a.folder_id)
    svc = drv.svc
    names = {s: svc.files().get(fileId=f, fields="name").execute()["name"] for s, f in SLOTS.items()}
    slot, why = pick_slot(names, today, a.force)
    if slot is None:
        print(f"BACKUP result=SKIP reason={why}", flush=True)
        return 0
    work = Path(a.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    drv.download("manifest.json", work / "manifest.json")
    man = json.loads((work / "manifest.json").read_text())
    files = ["manifest.json"]
    for name, meta in man["shards"].items():
        p = drv.download(name, work / name)
        if st.md5_file(p) != meta["md5"] or p.stat().st_size != meta["bytes"]:
            print(f"BACKUP result=BLOCKED reason=source_mismatch file={name}", flush=True)
            return 3
        files.append(name)
    tar = work / "backup.tar"
    with tarfile.open(tar, "w") as tf:
        for n in files:
            tf.add(work / n, arcname=n)
    with tarfile.open(tar) as tf:
        members = tf.getnames()
    if len(members) != len(man["shards"]) + 1:
        print(f"BACKUP result=BLOCKED reason=member_count {len(members)}", flush=True)
        return 3
    local_md5 = st.md5_file(tar)
    from googleapiclient.http import MediaFileUpload

    new_name = f"ohlcv_store_backup_{today.isoformat()}_slot{slot}.tar"
    r = svc.files().update(fileId=SLOTS[slot], body={"name": new_name},
                           media_body=MediaFileUpload(str(tar), mimetype="application/x-tar", resumable=True,
                                                      chunksize=32 * 1024 * 1024),
                           fields="id,name,size,md5Checksum").execute(num_retries=5)
    drv._prune(SLOTS[slot], keep=1)
    ok = r.get("md5Checksum") == local_md5 and int(r.get("size", 0)) == tar.stat().st_size
    print(f"BACKUP slot={slot} name={r['name']} store_session={man['session']} files={len(members)} "
          f"bytes={r.get('size')} md5_match={'yes' if ok else 'no'} other={names[[s for s in SLOTS if s != slot][0]]} "
          f"result={'WROTE' if ok else 'BLOCKED'}", flush=True)
    return 0 if ok else 3


def cmd_revoke_public(a) -> int:
    drv = st.Drive(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], WAREHOUSE)
    svc = drv.svc
    perms = svc.permissions().list(fileId=WAREHOUSE, fields="permissions(id,type,role,emailAddress)").execute()["permissions"]
    for p in perms:
        if p["type"] == "anyone":
            svc.permissions().delete(fileId=WAREHOUSE, permissionId=p["id"]).execute()
            print(f"removed permission type=anyone role={p['role']}", flush=True)
    after = svc.permissions().list(fileId=WAREHOUSE, fields="permissions(type,role,emailAddress)").execute()["permissions"]
    print("PERMS " + "; ".join(f"{p['type']}:{p['role']}:{p.get('emailAddress', '')}" for p in after), flush=True)
    # Authenticated SA read still works?
    m = st.Drive(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], a.folder_id, readonly=True)
    p = m.download("manifest.json", Path("manifest_check.json"))
    print(f"SA_READ ok session={json.loads(p.read_text()).get('session')}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backup", "revoke-public"])
    ap.add_argument("--folder-id", default=os.environ.get("OHLCV_STORE_FOLDER_ID", st.DEFAULT_FOLDER_ID))
    ap.add_argument("--work-dir", default="backup_work")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    return cmd_backup(a) if a.cmd == "backup" else cmd_revoke_public(a)


if __name__ == "__main__":
    sys.exit(main())

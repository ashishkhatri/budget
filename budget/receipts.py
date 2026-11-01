"""Receipt photos, kept small: grayscale JPEG, at most 1400 px on the long side, quality 55.
That's about 60-150 KB per receipt and still easy to read.

Files live in data/receipts/. A scan is saved as "pending-<token>.jpg" first; when you add the
transaction (or attach it to one that's already there) it's renamed to "<transaction id>-<token>.jpg"
and recorded in transactions.receipt. Pending files from abandoned scans, and photos whose
transaction was deleted, are cleaned up when the app starts.
"""
from __future__ import annotations

import io
import os
import re
import secrets as pysecrets
import time
from pathlib import Path
from typing import Optional

MAX_SIDE = 1400
QUALITY = 55
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_NAME = re.compile(r"^(\d+|pending)-[A-Za-z0-9_-]{8,40}\.jpg$")


def folder(db_path: str) -> Path:
    d = Path(db_path).parent / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def compress(data: bytes) -> bytes:
    try:
        from PIL import Image, ImageOps
    except ImportError:                       # Pillow comes with pdfplumber; keep the photo as-is if not
        return data
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=QUALITY, optimize=True, progressive=True)
        return out.getvalue()
    except Exception:                         # not an image Pillow can read: keep it as sent
        return data


def save_pending(db_path: str, data: bytes) -> str:
    token = pysecrets.token_urlsafe(12)
    (folder(db_path) / f"pending-{token}.jpg").write_bytes(compress(data))
    return token


def attach(con, db_path: str, tid: int, token: Optional[str]) -> Optional[str]:
    """Give a pending scan to a transaction. Returns the file name, or None."""
    if not token or not _TOKEN.match(token):
        return None
    src = folder(db_path) / f"pending-{token}.jpg"
    if not src.exists():
        return None
    name = f"{tid}-{token}.jpg"
    old = con.execute("SELECT receipt FROM transactions WHERE id = ?", (tid,)).fetchone()
    src.replace(folder(db_path) / name)
    con.execute("UPDATE transactions SET receipt = ? WHERE id = ?", (name, tid))
    if old and old[0] and old[0] != name:
        try:
            (folder(db_path) / old[0]).unlink()
        except OSError:
            pass
    return name


def path_for(db_path: str, name: str) -> Optional[Path]:
    if not _NAME.match(name or "") or name.startswith("pending-"):
        return None
    p = folder(db_path) / name
    return p if p.exists() else None


def clean_up(con, db_path: str, pending_age_hours: float = 24) -> int:
    """Remove abandoned scans and photos whose transaction is gone."""
    keep = {r[0] for r in con.execute("SELECT receipt FROM transactions WHERE receipt IS NOT NULL")}
    removed = 0
    now = time.time()
    for p in folder(db_path).glob("*.jpg"):
        stale_pending = p.name.startswith("pending-") and now - p.stat().st_mtime > pending_age_hours * 3600
        orphan = not p.name.startswith("pending-") and p.name not in keep
        if stale_pending or orphan:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed

"""Find transactions that were probably imported twice.

Importing already skips rows that exactly match one in the same account (same date, amount and
description). These slip through that check:

- the same statement imported into two accounts (e.g. "Chase Credit Card" and "... 9743");
- the same charge read slightly differently by two files: a CSV and a PDF of the same month, or
  the posting date in one and the transaction date in the other;
- a pending charge that later posted with a cleaned-up description.

A pair is a likely duplicate when it has the same amount, the same merchant (or nearly the same
description), dates at most a few days apart, and came from different imports. Rows from one file
never count as duplicates of each other: if your statement lists two $5 coffees, you bought two.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Dict, List, Tuple

from .db import squash

MAX_DAYS = 4
SETTING = "duplicates_dismissed"


def _desc_similarity(a: str, b: str) -> float:
    a, b = squash(a), squash(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.startswith(b) or b.startswith(a):
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def dismissed(con) -> set:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
    try:
        return set(json.loads(row["value"])) if row else set()
    except (ValueError, TypeError):
        return set()


def dismiss(con, ids: List[int]) -> None:
    """Mark every pair within ``ids`` as 'not a duplicate'."""
    keys = dismissed(con)
    ids = sorted(set(int(i) for i in ids))
    keys.update(f"{a}-{b}" for i, a in enumerate(ids) for b in ids[i + 1:])
    con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SETTING, json.dumps(sorted(keys))))


def _same_statement(a: dict, b: dict) -> bool:
    """Could rows in two accounts be one statement imported twice? Only if it's the same file, or
    one account is a less specific name for the other ("Chase Credit Card" vs "... 9743").
    Two cards in the same household often have identical charges (two subway fares), so
    matching rows on two different cards aren't duplicates."""
    if a["file_sha256"] and a["file_sha256"] == b["file_sha256"]:
        return True
    x, y = sorted(((a["account"] or "").lower(), (b["account"] or "").lower()), key=len)
    return bool(x) and y.startswith(x) and x != y


def _different_store(a: str, b: str) -> bool:
    """'CHIPOTLE 2231 HOUSTON' and 'CHIPOTLE 5000 PEARLAND' are two restaurants, not one charge."""
    na, nb = set(re.findall(r"\d{3,}", a)), set(re.findall(r"\d{3,}", b))
    return bool(na and nb and not na & nb)


def _pairs(rows: List[dict], hidden: set) -> List[Tuple[int, int, str, float]]:
    """(id_a, id_b, reason, score) for every likely duplicate pair."""
    by_amount: Dict[int, List[dict]] = defaultdict(list)
    for r in rows:
        by_amount[r["amount_cents"]].append(r)
    out = []
    for group in by_amount.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r["date"])
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                gap = (b["d"] - a["d"]).days
                if gap > MAX_DAYS:
                    break
                if a["import_id"] is not None and a["import_id"] == b["import_id"]:
                    continue                     # same file: genuinely separate purchases
                lo, hi = sorted((a["id"], b["id"]))
                if f"{lo}-{hi}" in hidden:
                    continue
                sim = _desc_similarity(a["description"], b["description"])
                if sim < 0.75 or _different_store(a["description"], b["description"]):
                    continue
                exact = sim == 1.0 and gap == 0
                cross = a["account_id"] != b["account_id"]
                if cross and not (exact and _same_statement(a, b)):
                    continue

                score = (1.0 if exact else 0.6 + 0.3 * sim - 0.05 * gap) - (0.05 if cross else 0)
                if exact:
                    reason = "Same date, amount and description"
                elif gap == 0:
                    reason = "Same date and amount, similar description"
                else:
                    reason = f"Same amount, {gap} day{'s' if gap > 1 else ''} apart"
                if cross:
                    reason += ", in two accounts"
                out.append((a["id"], b["id"], reason, round(score, 2)))
    return out


def find(con) -> dict:
    rows = [dict(r) for r in con.execute("""
        SELECT t.id, t.date, t.description, t.amount_cents, t.account_id, t.import_id, t.merchant_id,
               t.category_id, t.category_source, t.note, COALESCE(m.name, t.merchant) AS merchant,
               a.name AS account, i.filename, i.created_at AS imported_at, i.file_sha256
        FROM transactions t
        LEFT JOIN merchants m ON m.id = t.merchant_id
        LEFT JOIN accounts a ON a.id = t.account_id
        LEFT JOIN imports i ON i.id = t.import_id""")]
    for r in rows:
        r["d"] = dt.date.fromisoformat(r["date"])
    pairs = _pairs(rows, dismissed(con))
    by_id = {r["id"]: r for r in rows}

    # Pair each transaction with at most one other, best matches first. (Chaining every candidate
    # together would lump a week of identical subway fares into one group.) A third copy shows up
    # again once you've resolved the first pair.
    pairs.sort(key=lambda p: (-p[3], p[0], p[1]))
    used: set = set()
    groups: Dict[int, dict] = {}
    for a, b, reason, score in pairs:
        if a in used or b in used:
            continue
        used.update((a, b))
        groups[a] = {"ids": {a, b}, "reasons": [reason], "score": score}

    out = []
    for g in groups.values():
        txs = sorted((by_id[i] for i in g["ids"]), key=lambda r: (r["date"], r["id"]))
        # Keep the one you've touched (your category or a note), else the one imported first.
        keep = min(txs, key=lambda r: (r["category_source"] not in ("manual", "merchant") and not r["note"], r["id"]))
        out.append({
            "ids": [t["id"] for t in txs], "keep_id": keep["id"],
            "reason": g["reasons"][0], "score": g["score"],
            "amount_cents": txs[0]["amount_cents"], "merchant": keep["merchant"], "merchant_id": keep["merchant_id"],
            "transactions": [{k: t[k] for k in ("id", "date", "description", "account", "account_id", "filename",
                                                "imported_at", "category_id", "category_source", "note")} for t in txs],
        })
    out.sort(key=lambda g: (-g["score"], g["transactions"][0]["date"]))
    return {"groups": out, "total": sum(len(g["ids"]) - 1 for g in out)}

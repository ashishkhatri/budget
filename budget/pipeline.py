"""The single path that gets transactions into the database.

File imports call ``preview`` (to show the user what will happen) and then
``ingest``. A future bank-API sync calls ``ingest`` directly with Candidates
that carry an ``external_id``. Either way, categorization and duplicate
detection behave the same.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .categorize import UNCATEGORIZED, categorize, detail_key, merchant_key, prepare_rules
from .db import squash
from .parsing.common import Candidate, norm


# ---------- lookups ----------

def category_ids(con: sqlite3.Connection) -> Dict[str, int]:
    return {r["name"].lower(): r["id"] for r in con.execute("SELECT id, name FROM categories")}


def load_rules(con: sqlite3.Connection):
    rows = con.execute("SELECT r.pattern, c.name FROM rules r JOIN categories c ON c.id = r.category_id").fetchall()
    return prepare_rules((r[0], r[1]) for r in rows)


def load_patterns(con) -> List[Tuple[str, int]]:
    """Merchant patterns (squashed), longest first."""
    rows = [(squash(p), mid) for p, mid in con.execute("SELECT pattern, merchant_id FROM merchant_patterns")]
    return sorted([r for r in rows if r[0]], key=lambda r: -len(r[0]))


def pattern_merchant(patterns: List[Tuple[str, int]], description: str) -> Optional[int]:
    d = squash(description)
    for p, mid in patterns:
        if p in d:
            return mid
    return None


class Categorizer:
    """Categorizes with merchants, rules and keywords, and maps names to category ids."""

    def __init__(self, con: sqlite3.Connection):
        self.ids = category_ids(con)
        self.names = {v: k for k, v in self.ids.items()}
        self.rules = load_rules(con)
        self.uncat = self.ids[UNCATEGORIZED.lower()]
        # alias key -> (category name, 'manual'|'ai') for merchants that have a category
        self.merchants = {}
        for key, cid, src in con.execute("""SELECT a.key, m.category_id, m.category_source
                                            FROM merchant_aliases a JOIN merchants m ON m.id = a.merchant_id
                                            WHERE m.category_id IS NOT NULL"""):
            if cid in self.names:
                self.merchants[key.upper()] = (self.names[cid], src or "manual")
        self.patterns = load_patterns(con)
        self.by_id = {mid: (self.names[cid], src or "manual") for mid, cid, src in con.execute(
            "SELECT id, category_id, category_source FROM merchants WHERE category_id IS NOT NULL") if cid in self.names}

    def __call__(self, description, amount_cents, bank_category="", bank_type="") -> Tuple[int, str]:
        pm = pattern_merchant(self.patterns, description) if self.patterns else None
        m = self.by_id.get(pm) if pm is not None else self.merchants.get(merchant_key(description).upper())
        name, source = categorize(description, amount_cents, bank_category, bank_type, self.rules, m)
        cid = self.ids.get(name.lower())
        if cid is None:  # built-in category was deleted by the user
            return self.uncat, "none"
        return cid, source


# ---------- merchants ----------

def resolve_merchant(con, description: str, cache: Optional[dict] = None) -> Tuple[int, str]:
    """(merchant id, alias key) for a bank description, creating the merchant if it's new."""
    key = merchant_key(description)
    patterns = cache.setdefault("__patterns__", load_patterns(con)) if cache is not None else load_patterns(con)
    pm = pattern_merchant(patterns, description) if patterns else None
    if pm is not None:
        return pm, key
    if cache is not None and key.upper() in cache:
        return cache[key.upper()], key
    row = con.execute("SELECT merchant_id FROM merchant_aliases WHERE key = ?", (key,)).fetchone()
    if row:
        mid = row[0]
    else:
        existing = con.execute("SELECT id FROM merchants WHERE name = ?", (key,)).fetchone()
        mid = existing[0] if existing else con.execute("INSERT INTO merchants(name) VALUES (?)", (key,)).lastrowid
        con.execute("INSERT INTO merchant_aliases(key, merchant_id) VALUES (?,?)", (key, mid))
    if cache is not None:
        cache[key.upper()] = mid
    return mid, key


def set_merchant_category(con, merchant_id: int, category_id: Optional[int]) -> int:
    """Set (or clear, with None) a merchant's category. Setting it applies to ALL its transactions,
    including ones changed by hand before. Returns how many transactions changed."""
    if category_id is None:
        con.execute("UPDATE merchants SET category_id = NULL, category_source = NULL WHERE id = ?", (merchant_id,))
        return recategorize(con, merchant_id=merchant_id)
    con.execute("UPDATE merchants SET category_id = ?, category_source = 'manual' WHERE id = ?", (category_id, merchant_id))
    return con.execute("UPDATE transactions SET category_id = ?, category_source = 'merchant' "
                       "WHERE merchant_id = ? AND (category_id != ? OR category_source != 'merchant')",
                       (category_id, merchant_id, category_id)).rowcount


def remember_ai_category(con, merchant_id: int, category_id: int) -> None:
    """Save an AI answer on the merchant, unless the merchant already has a category."""
    con.execute("UPDATE merchants SET category_id = ?, category_source = 'ai' "
                "WHERE id = ? AND category_id IS NULL", (category_id, merchant_id))


def merge_merchants(con, source_id: int, target_id: int) -> None:
    """Fold one merchant into another: its alternate names and transactions move over."""
    if source_id == target_id:
        return
    src = con.execute("SELECT category_id, category_source FROM merchants WHERE id = ?", (source_id,)).fetchone()
    con.execute("UPDATE merchant_aliases SET merchant_id = ? WHERE merchant_id = ?", (target_id, source_id))
    con.execute("UPDATE merchant_patterns SET merchant_id = ? WHERE merchant_id = ?", (target_id, source_id))
    con.execute("UPDATE transactions SET merchant_id = ? WHERE merchant_id = ?", (target_id, source_id))
    if src and src[0] is not None:
        con.execute("UPDATE merchants SET category_id = ?, category_source = ? WHERE id = ? AND category_id IS NULL",
                    (src[0], src[1], target_id))
    con.execute("DELETE FROM merchants WHERE id = ?", (source_id,))
    tgt = con.execute("SELECT category_id, category_source FROM merchants WHERE id = ?", (target_id,)).fetchone()
    if tgt and tgt[0] is not None and tgt[1] == "manual":
        set_merchant_category(con, target_id, tgt[0])
    else:
        recategorize(con, merchant_id=target_id)


def split_groups(con, merchant_id: int) -> List[dict]:
    """A merchant's transactions grouped by business, to offer a split.

    Transactions are grouped by their longer name ('CLEAR LAKE WATER', 'CLEAR LAKE HUNAN RESTAURA').
    Names where one just extends the other ('TACODELI WASHINGTON A' and '... A HOUSTON TX', a
    city added) are the same business and stay together. Each group's ``key`` is its shortest
    name: the suggested merchant name, and the text future descriptions are matched on.
    """
    by_key: Dict[str, dict] = {}
    for tid, desc, amt in con.execute(
            "SELECT id, description, amount_cents FROM transactions WHERE merchant_id = ? ORDER BY date DESC",
            (merchant_id,)):
        k = detail_key(desc)
        g = by_key.setdefault(k, {"keys": [k], "ids": [], "examples": [], "total_cents": 0})
        g["ids"].append(tid)
        g["total_cents"] += amt
        if desc not in g["examples"]:
            g["examples"].append(desc)
    groups: List[dict] = []
    for k in sorted(by_key, key=lambda k: len(squash(k))):           # shortest first: it becomes the key
        sk = squash(k)
        home = next((g for g in groups if sk.startswith(squash(g["key"]))), None)
        if home is None:
            home = {"key": k, "ids": [], "examples": [], "total_cents": 0, "variants": 0}
            groups.append(home)
        src = by_key[k]
        home["ids"] += src["ids"]
        home["total_cents"] += src["total_cents"]
        home["variants"] += 1
        home["examples"] += [e for e in src["examples"] if e not in home["examples"]]
    for g in groups:
        g["count"] = len(g["ids"])
        g["examples"] = g["examples"][:4]
    return sorted(groups, key=lambda g: -g["count"])


def split_merchant(con, merchant_id: int, groups: List[dict]) -> List[int]:
    """Move groups of a merchant's transactions to other merchants.

    Each group is {name, match, ids}: the transactions go to the merchant called ``name`` (created
    if needed; an existing one is reused), and future descriptions containing ``match`` go there
    too. A group named like the current merchant stays. Returns the merchants that received
    transactions.
    """
    cur = con.execute("SELECT name, notes, category_id FROM merchants WHERE id = ?", (merchant_id,)).fetchone()
    touched = []
    for g in groups:
        name = " ".join(str(g.get("name") or "").split())[:80].upper()
        if not name or name.lower() == cur[0].lower():
            continue
        ids = [int(i) for i in g.get("ids") or []]
        row = con.execute("SELECT id FROM merchants WHERE name = ?", (name,)).fetchone()
        target = row[0] if row else con.execute("INSERT INTO merchants(name) VALUES (?)", (name,)).lastrowid
        con.executemany("UPDATE transactions SET merchant_id = ? WHERE id = ? AND merchant_id = ?",
                        [(target, i, merchant_id) for i in ids])
        match = " ".join(str(g.get("match") or "").split())
        if squash(match):
            con.execute("INSERT INTO merchant_patterns(pattern, merchant_id) VALUES (?, ?) "
                        "ON CONFLICT(pattern) DO UPDATE SET merchant_id = excluded.merchant_id", (match.upper(), target))
        touched.append(target)
    for t in dict.fromkeys(touched):
        # moved transactions follow their new merchant's category (or are categorized afresh)
        con.execute("UPDATE transactions SET category_source = 'none' WHERE merchant_id = ? AND category_source = 'merchant'", (t,))
        tgt = con.execute("SELECT category_id, category_source FROM merchants WHERE id = ?", (t,)).fetchone()
        if tgt[0] is not None and tgt[1] == "manual":
            set_merchant_category(con, t, tgt[0])
        else:
            recategorize(con, merchant_id=t)
    left = con.execute("SELECT COUNT(*) FROM transactions WHERE merchant_id = ?", (merchant_id,)).fetchone()[0]
    if not left and not cur[1]:
        con.execute("DELETE FROM merchants WHERE id = ?", (merchant_id,))
    return list(dict.fromkeys(touched))


def find_account(con: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM accounts WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()


def get_or_create_account(con: sqlite3.Connection, name: str, institution: Optional[str] = None,
                          account_type: str = "other", source: str = "file", external_id: Optional[str] = None) -> int:
    name = (name or "").strip() or "Imported"
    row = find_account(con, name)
    if row:
        return row["id"]
    cur = con.execute("INSERT INTO accounts(name, institution, type, source, external_id) VALUES (?,?,?,?,?)",
                      (name, institution, account_type or "other", source, external_id))
    return cur.lastrowid


# ---------- duplicates ----------

def _key(date: str, amount_cents: int, description: str):
    return (date, int(amount_cents), norm(description))


def _existing_counts(con, account_id: Optional[int], dates: Sequence[str]) -> Counter:
    if account_id is None or not dates:
        return Counter()
    rows = con.execute(
        "SELECT date, amount_cents, description FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ?",
        (account_id, min(dates), max(dates)))
    return Counter(_key(r[0], r[1], r[2]) for r in rows)


def mark_duplicates(con, account_name: str, rows: Sequence[dict]) -> List[bool]:
    """For each row, True if it's already in the account.

    Counts matter: if the file has two identical $5 coffees on the same day
    and the database has one, only one of them is a duplicate.
    """
    acct = find_account(con, account_name) if account_name else None
    have = _existing_counts(con, acct["id"] if acct else None, [r["date"] for r in rows])
    out = []
    for r in rows:
        k = _key(r["date"], r["amount_cents"], r["description"])
        if have[k] > 0:
            have[k] -= 1
            out.append(True)
        else:
            out.append(False)
    return out


# ---------- preview & ingest ----------

def preview(con, candidates: Sequence[Candidate], account_name: str) -> List[dict]:
    cat = Categorizer(con)
    rows = []
    for c in candidates:
        cid, src = cat(c.description, c.amount_cents, c.bank_category, c.bank_type)
        rows.append({"date": c.date, "description": c.description, "amount_cents": c.amount_cents,
                     "bank_category": c.bank_category, "bank_type": c.bank_type,
                     "category_id": cid, "category_source": src})
    for r, dup in zip(rows, mark_duplicates(con, account_name, rows)):
        r["dup"] = dup
    return rows


def ingest(con, account_id: int, rows: Iterable[dict], *, source: str = "file", filename: Optional[str] = None,
           fmt: Optional[str] = None, file_sha256: Optional[str] = None) -> dict:
    """Insert rows into an account, skipping duplicates. Commits nothing; caller owns the transaction.

    Each row: date, description, amount_cents, and optionally bank_category, bank_type,
    external_id, pending, and category_id + manual=True to force a category.
    """
    rows = list(rows)
    cat = Categorizer(con)
    mcache: dict = {}
    have = _existing_counts(con, account_id, [r["date"] for r in rows if not r.get("external_id")])
    cur = con.execute("INSERT INTO imports(source, filename, account_id, format, file_sha256) VALUES (?,?,?,?,?)",
                      (source, filename, account_id, fmt, file_sha256))
    import_id = cur.lastrowid
    inserted = skipped = 0
    for r in rows:
        if not r.get("external_id"):
            k = _key(r["date"], r["amount_cents"], r["description"])
            if have[k] > 0:
                have[k] -= 1
                skipped += 1
                continue
        mid, mkey = resolve_merchant(con, r["description"], mcache)
        if r.get("manual") and r.get("category_id"):
            cid, src = int(r["category_id"]), "manual"
        else:
            cid, src = cat(r["description"], r["amount_cents"], r.get("bank_category", ""), r.get("bank_type", ""))
            if src == "none" and r.get("category_source") in ("ai", "learned") and r.get("category_id"):
                cid, src = int(r["category_id"]), r["category_source"]   # AI suggestion from the preview
                if src == "ai":
                    remember_ai_category(con, mid, cid)
                    cat = Categorizer(con)                               # so later rows of this merchant follow
        res = con.execute(
            """INSERT OR IGNORE INTO transactions
               (account_id, date, description, amount_cents, category_id, category_source, merchant, merchant_id,
                bank_category, bank_type, external_id, pending, import_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, r["date"], r["description"], int(r["amount_cents"]), cid, src,
             mkey, mid, r.get("bank_category") or None, r.get("bank_type") or None,
             r.get("external_id"), 1 if r.get("pending") else 0, import_id))
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    con.execute("UPDATE imports SET row_count = ?, skipped_count = ? WHERE id = ?", (inserted, skipped, import_id))
    return {"import_id": import_id, "inserted": inserted, "skipped": skipped}


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_file_import(con, sha256: str) -> Optional[dict]:
    """The earlier import of this exact file, if its transactions are still in the database."""
    row = con.execute("""
        SELECT i.id, i.filename, i.created_at, i.row_count, a.name account
        FROM imports i LEFT JOIN accounts a ON a.id = i.account_id
        WHERE i.file_sha256 = ? AND EXISTS (SELECT 1 FROM transactions t WHERE t.import_id = i.id)
        ORDER BY i.id DESC LIMIT 1""", (sha256,)).fetchone()
    return dict(row) if row else None


def refresh_merchants(con) -> int:
    """Keep merchants in step with the name-cleanup rules (run at startup).

    - Transactions without a merchant get one.
    - When the cleanup now gives a better name (e.g. 'MARSH &' -> 'MARSH & MCLENNAN'):
        * a merchant you never renamed takes the new name, or is split up if the old clipped name
          had lumped different companies together (each keeps the category you set);
        * a merchant you renamed or merged keeps its name and just gains the new bank name as an alias.
    """
    changed = 0
    cache: dict = {}
    by_merchant: Dict[int, list] = {}
    for tid, desc, mkey, mid in con.execute("SELECT id, description, merchant, merchant_id FROM transactions").fetchall():
        key = merchant_key(desc)
        if mid is None:
            mid, key = resolve_merchant(con, desc, cache)
            con.execute("UPDATE transactions SET merchant_id = ?, merchant = ? WHERE id = ?", (mid, key, tid))
            changed += 1
        elif key != mkey:
            by_merchant.setdefault(mid, []).append((tid, desc, mkey, key))
    for mid, txs in by_merchant.items():
        m = con.execute("SELECT name, category_id, category_source, notes FROM merchants WHERE id = ?", (mid,)).fetchone()
        old_keys = {t[2].upper() for t in txs if t[2]}
        new_keys = {t[3] for t in txs}
        auto_named = m is not None and m[0].upper() in old_keys
        if auto_named and len(new_keys) == 1:
            new = next(iter(new_keys))
            owner = con.execute("SELECT merchant_id FROM merchant_aliases WHERE key = ?", (new,)).fetchone()
            clash = con.execute("SELECT id FROM merchants WHERE name = ? AND id != ?", (new, mid)).fetchone()
            if not clash and (owner is None or owner[0] == mid):
                con.execute("UPDATE merchants SET name = ? WHERE id = ?", (new, mid))
                if owner is None:
                    con.execute("INSERT INTO merchant_aliases(key, merchant_id) VALUES (?,?)", (new, mid))
                con.executemany("UPDATE transactions SET merchant = ? WHERE id = ?", [(new, t[0]) for t in txs])
                changed += len(txs)
                continue
        if auto_named:
            # split: each transaction goes to the merchant for its new name, keeping your category
            for tid, desc, _old, _new in txs:
                nmid, key = resolve_merchant(con, desc, cache)
                con.execute("UPDATE transactions SET merchant_id = ?, merchant = ? WHERE id = ?", (nmid, key, tid))
                if m[1] is not None:
                    con.execute("UPDATE merchants SET category_id = ?, category_source = ? WHERE id = ? AND category_id IS NULL",
                                (m[1], m[2], nmid))
                changed += 1
            left = con.execute("SELECT COUNT(*) FROM transactions WHERE merchant_id = ?", (mid,)).fetchone()[0]
            if not left and not m[3]:
                con.execute("DELETE FROM merchants WHERE id = ?", (mid,))
            continue
        for tid, desc, _old, key in txs:     # renamed/merged by you: keep it, learn the new bank name
            if not con.execute("SELECT 1 FROM merchant_aliases WHERE key = ?", (key,)).fetchone():
                con.execute("INSERT INTO merchant_aliases(key, merchant_id) VALUES (?,?)", (key, mid))
            con.execute("UPDATE transactions SET merchant = ? WHERE id = ?", (key, tid))
            changed += 1
    return changed


def recategorize(con, include_manual: bool = False, merchant_id: Optional[int] = None) -> int:
    """Re-run categorization. Hand-picked categories are kept unless include_manual."""
    cat = Categorizer(con)
    q = "SELECT id, description, amount_cents, bank_category, bank_type, category_id, category_source FROM transactions"
    where, args = [], []
    if not include_manual:
        where.append("category_source != 'manual'")
    if merchant_id is not None:
        where.append("merchant_id = ?"); args.append(merchant_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    changed = 0
    for r in con.execute(q, args).fetchall():
        cid, src = cat(r["description"], r["amount_cents"], r["bank_category"] or "", r["bank_type"] or "")
        if src == "none" and r["category_source"] in ("ai", "learned"):
            continue  # nothing better than the AI's pick; keep it
        if cid != r["category_id"]:
            changed += 1
        con.execute("UPDATE transactions SET category_id = ?, category_source = ? WHERE id = ?", (cid, src, r["id"]))
    return changed

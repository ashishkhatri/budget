"""Bank sync through Plaid (https://plaid.com).

What happens, and what's stored:
- You sign in to your bank inside Plaid's own window (for Chase, on chase.com itself). Your bank
  password never reaches this app.
- Plaid gives the app an *access token* for the "transactions" product only: read-only. This app
  never asks for account/routing numbers or payment features, so nothing here can move money.
- Your Plaid secret and the access tokens are stored encrypted with your Windows login
  (see secrets.py). Revoke access any time in the bank's settings or at my.plaid.com.

Sync uses /transactions/sync with a saved cursor, so each run only fetches what changed.
Transactions already imported from CSV/PDF files are matched up (same account, same amount,
dates within a few days) instead of being added twice.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from . import pipeline
from . import secrets

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
SETTINGS = ("plaid_client_id", "plaid_secret", "plaid_env")
LINK_SCRIPT = "https://cdn.plaid.com/link/v2/stable/link-initialize.js"
MATCH_DAYS = 3
_lock = threading.Lock()          # one sync at a time


class PlaidError(Exception):
    pass


# ---------- settings ----------

def get_settings(con) -> Dict[str, str]:
    rows = dict(con.execute("SELECT key, value FROM settings WHERE key LIKE 'plaid_%'").fetchall())
    return {k: rows.get(k) or "" for k in SETTINGS} | {"plaid_env": rows.get("plaid_env") or "sandbox"}


def public_settings(con) -> dict:
    s = get_settings(con)
    items = [dict(r) for r in con.execute(
        "SELECT id, institution, item_id, last_sync, last_error, created_at, access_token FROM plaid_items ORDER BY id")]
    accounts = {}
    for aid, name, item in con.execute("SELECT id, name, plaid_item FROM accounts WHERE plaid_item IS NOT NULL"):
        accounts.setdefault(item, []).append({"id": aid, "name": name})
    for it in items:
        it["encrypted"] = secrets.is_sealed(it.pop("access_token"))
        it["accounts"] = accounts.get(it["id"], [])
    return {"client_id": s["plaid_client_id"], "has_secret": bool(s["plaid_secret"]), "env": s["plaid_env"],
            "encrypted": secrets.is_sealed(s["plaid_secret"]) if s["plaid_secret"] else secrets.protected(),
            "items": items, "link_script": LINK_SCRIPT}


def save_settings(con, client_id: Optional[str], secret: Optional[str], env: Optional[str]) -> None:
    def put(k, v):
        con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, v))
    if client_id is not None:
        put("plaid_client_id", client_id.strip())
    if secret:                                   # blank = keep the saved one
        put("plaid_secret", secrets.seal(secret.strip()))
    if env in HOSTS:
        put("plaid_env", env)


# ---------- HTTP ----------

def _post(con, path: str, body: dict, timeout: float = 60) -> dict:
    s = get_settings(con)
    if not s["plaid_client_id"] or not s["plaid_secret"]:
        raise PlaidError("Add your Plaid client ID and secret first (Settings → Bank connections).")
    base = os.environ.get("PLAID_BASE_URL") or HOSTS[s["plaid_env"]]
    payload = {"client_id": s["plaid_client_id"], "secret": secrets.unseal(s["plaid_secret"]), **body}
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Plaid-Version": "2020-09-14"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise PlaidError(f"Plaid returned an error ({e.code}).") from e
        code = err.get("error_code") or ""
        msg = err.get("display_message") or err.get("error_message") or code
        ex = PlaidError(f"Plaid: {msg}" + (f" ({code})" if code and code not in msg else ""))
        ex.code = code
        raise ex from e
    except (urllib.error.URLError, OSError) as e:
        raise PlaidError(f"Can't reach Plaid ({getattr(e, 'reason', e)}). Check your internet connection.") from e


# ---------- connecting ----------

def link_token(con) -> str:
    r = _post(con, "/link/token/create", {
        "client_name": "Budget", "language": "en", "country_codes": ["US"],
        "user": {"client_user_id": "budget-local-user"},
        "products": ["transactions"], "transactions": {"days_requested": 730},
    })
    return r["link_token"]


def exchange(con, public_token: str, institution: Optional[dict] = None) -> dict:
    r = _post(con, "/item/public_token/exchange", {"public_token": public_token})
    inst = (institution or {}).get("name") or "Bank"
    cur = con.execute("INSERT INTO plaid_items(item_id, access_token, institution, institution_id) VALUES (?,?,?,?)",
                      (r["item_id"], secrets.seal(r["access_token"]), inst, (institution or {}).get("institution_id")))
    item = con.execute("SELECT * FROM plaid_items WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"id": item["id"], "institution": inst, "accounts": link_accounts(con, item)}


def _kind(acc: dict) -> str:
    if acc.get("type") == "credit":
        return "credit"
    return acc.get("subtype") if acc.get("subtype") in ("checking", "savings") else "other"


def link_accounts(con, item) -> List[dict]:
    """Match the bank's accounts to yours by their last 4 digits ("Chase Credit Card 9743"), so
    imported history and synced transactions share one account; create the rest."""
    r = _post(con, "/accounts/get", {"access_token": secrets.unseal(item["access_token"])})
    out = []
    for acc in r.get("accounts", []):
        pid, mask, kind = acc["account_id"], acc.get("mask") or "", _kind(acc)
        row = con.execute("SELECT id, name FROM accounts WHERE source = 'plaid' AND external_id = ?", (pid,)).fetchone()
        if not row and mask:
            cands = con.execute("""SELECT id, name, type FROM accounts WHERE (external_id IS NULL OR source != 'plaid')
                                   AND (name LIKE ? OR name LIKE ?) ORDER BY id""", (f"% {mask}", f"%{mask}")).fetchall()
            cands = [c for c in cands if c["type"] in (kind, "other")] or cands
            row = cands[0] if cands else None
        if row:
            con.execute("UPDATE accounts SET source = 'plaid', external_id = ?, plaid_item = ? WHERE id = ?",
                        (pid, item["id"], row["id"]))
            out.append({"id": row["id"], "name": row["name"], "matched": True})
            continue
        kind_name = {"credit": "Credit Card", "checking": "Checking", "savings": "Savings"}.get(kind) or \
            (acc.get("official_name") or acc.get("name") or "Account")
        name = " ".join(x for x in (item["institution"], kind_name, mask) if x)
        base, n = name, 2
        while con.execute("SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone():
            name, n = f"{base} ({n})", n + 1
        aid = pipeline.get_or_create_account(con, name, item["institution"], kind, source="plaid", external_id=pid)
        con.execute("UPDATE accounts SET plaid_item = ? WHERE id = ?", (item["id"], aid))
        out.append({"id": aid, "name": name, "matched": False})
    return out


def disconnect(con, item_db_id: int) -> None:
    """Stop syncing: Plaid forgets the connection. Accounts and transactions stay; the link ids are
    cleared so that reconnecting later matches up with them again instead of duplicating."""
    item = con.execute("SELECT * FROM plaid_items WHERE id = ?", (item_db_id,)).fetchone()
    if not item:
        return
    try:
        _post(con, "/item/remove", {"access_token": secrets.unseal(item["access_token"])})
    except PlaidError:
        pass                                   # already gone at Plaid; forget it here anyway
    ids = [r[0] for r in con.execute("SELECT id FROM accounts WHERE plaid_item = ?", (item_db_id,))]
    for aid in ids:
        con.execute("UPDATE transactions SET external_id = NULL WHERE account_id = ?", (aid,))
        con.execute("UPDATE accounts SET source = 'file', external_id = NULL, plaid_item = NULL WHERE id = ?", (aid,))
    con.execute("DELETE FROM plaid_items WHERE id = ?", (item_db_id,))


# ---------- syncing ----------

def _fetch_changes(con, item) -> dict:
    token = secrets.unseal(item["access_token"])
    for _attempt in range(3):
        cursor, added, modified, removed = item["cursor"], [], [], []
        status = None
        try:
            while True:
                body = {"access_token": token, "count": 500, "options": {"include_original_description": True}}
                if cursor:
                    body["cursor"] = cursor
                r = _post(con, "/transactions/sync", body)
                added += r.get("added", [])
                modified += r.get("modified", [])
                removed += r.get("removed", [])
                cursor = r.get("next_cursor") or cursor
                status = r.get("transactions_update_status")
                if not r.get("has_more"):
                    return {"added": added, "modified": modified, "removed": removed, "cursor": cursor,
                            "status": status}
        except PlaidError as e:
            if getattr(e, "code", "") != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise
    raise PlaidError("Plaid kept changing the data while it was being read. Try again in a minute.")


def _row(t: dict) -> dict:
    p = t.get("personal_finance_category") or {}
    pfc = p.get("detailed") or p.get("primary") or ""          # e.g. FOOD_AND_DRINK_GROCERIES
    return {"date": t.get("authorized_date") or t["date"],
            "posted": t["date"],
            "description": " ".join((t.get("original_description") or t.get("name") or "").split()),
            "amount_cents": -int(round(float(t["amount"]) * 100)),   # Plaid: positive = money out
            "bank_category": pfc.replace("_", " ").title(), "external_id": t["transaction_id"],
            "account": t["account_id"]}


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _catch_all_accounts(con) -> List[int]:
    """Imported accounts with no card/account number in the name ("old transactions-unknown acct"):
    they may hold history of any account, so synced transactions are matched against them too."""
    return [r[0] for r in con.execute("SELECT id, name FROM accounts WHERE source != 'plaid'")
            if not re.search(r"\d{4}", r[1] or "")]


def _history_start(con, account_id: int) -> Optional[str]:
    """The first transaction imported from a file into this account. Plaid's older history is
    skipped: before that date your history came from files (often in a catch-all account), and
    adding it again would double it."""
    r = con.execute("""SELECT MIN(t.date) FROM transactions t JOIN imports i ON i.id = t.import_id
                       WHERE t.account_id = ? AND i.source = 'file'""", (account_id,)).fetchone()
    return r[0] if r else None


def _match_existing(con, account_id: int, rows: List[dict]):
    """Link synced transactions to ones already imported from files.
    Returns (new rows, how many were skipped as older than your imported history)."""
    start = _history_start(con, account_id)
    total = len(rows)
    if start:
        rows = [r for r in rows if r["date"] >= start]
    older = total - len(rows)
    if not rows:
        return [], older
    lo = min(min(r["date"], r["posted"]) for r in rows)
    hi = max(max(r["date"], r["posted"]) for r in rows)
    lo = (_d(lo) - dt.timedelta(days=MATCH_DAYS)).isoformat()
    hi = (_d(hi) + dt.timedelta(days=MATCH_DAYS)).isoformat()
    pool: Dict[int, List[tuple]] = {}
    accts = [account_id] + [a for a in _catch_all_accounts(con) if a != account_id]
    for tid, date, amt in con.execute(f"""SELECT id, date, amount_cents FROM transactions
                                          WHERE account_id IN ({','.join('?' * len(accts))})
                                          AND external_id IS NULL AND date BETWEEN ? AND ?
                                          ORDER BY account_id = ? DESC""", (*accts, lo, hi, account_id)):
        pool.setdefault(amt, []).append((tid, _d(date)))
    new = []
    for r in rows:
        if con.execute("SELECT 1 FROM transactions WHERE account_id = ? AND external_id = ?",
                       (account_id, r["external_id"])).fetchone():
            continue                                             # already have it
        best = None
        for i, (tid, d) in enumerate(pool.get(r["amount_cents"], [])):
            gap = min(abs((d - _d(r["date"])).days), abs((d - _d(r["posted"])).days))
            if gap <= MATCH_DAYS and (best is None or gap < best[0]):
                best = (gap, i, tid)
        if best:
            pool[r["amount_cents"]].pop(best[1])
            con.execute("UPDATE transactions SET external_id = ? WHERE id = ?", (r["external_id"], best[2]))
        else:
            new.append(r)
    return new, older


def sync_item(con, item) -> dict:
    ch = _fetch_changes(con, item)
    acct = {r[0]: r[1] for r in con.execute(
        "SELECT external_id, id FROM accounts WHERE source = 'plaid' AND plaid_item = ?", (item["id"],))}
    if any(t["account_id"] not in acct for t in ch["added"]):
        link_accounts(con, item)                                 # a new account appeared at the bank
        acct = {r[0]: r[1] for r in con.execute(
            "SELECT external_id, id FROM accounts WHERE source = 'plaid' AND plaid_item = ?", (item["id"],))}
    gone = [r.get("transaction_id") for r in ch["removed"] if r.get("transaction_id")]
    ids = list(acct.values())
    for tid in gone if ids else []:
        con.execute(f"DELETE FROM transactions WHERE external_id = ? AND account_id IN ({','.join('?' * len(ids))})",
                    (tid, *ids))
    by_acct: Dict[int, List[dict]] = {}
    for t in ch["added"]:
        if t.get("pending") or t["account_id"] not in acct:
            continue                                             # pending ones arrive again once they post
        by_acct.setdefault(acct[t["account_id"]], []).append(_row(t))
    added = matched = older = 0
    for aid, rows in by_acct.items():
        new, old = _match_existing(con, aid, rows)
        older += old
        matched += len(rows) - len(new) - old
        if new:
            res = pipeline.ingest(con, aid, new, source="plaid", filename=f"Plaid sync ({item['institution']})")
            added += res["inserted"]
    changed = 0
    for t in ch["modified"]:
        if t.get("pending"):
            continue
        r = _row(t)
        changed += con.execute("UPDATE transactions SET date = ?, amount_cents = ?, description = ? WHERE external_id = ?",
                               (r["date"], r["amount_cents"], r["description"], r["external_id"])).rowcount
    con.execute("UPDATE plaid_items SET cursor = ?, last_sync = datetime('now'), last_error = NULL WHERE id = ?",
                (ch["cursor"], item["id"]))
    return {"institution": item["institution"], "added": added, "matched": matched, "older": older, "updated": changed,
            "removed": len(gone), "status": ch["status"]}


def sync_all(con) -> dict:
    if not _lock.acquire(blocking=False):
        raise PlaidError("A sync is already running.")
    try:
        results, errors = [], []
        for item in con.execute("SELECT * FROM plaid_items ORDER BY id").fetchall():
            try:
                with con:
                    results.append(sync_item(con, item))
            except PlaidError as e:
                msg = str(e)
                if getattr(e, "code", "") == "ITEM_LOGIN_REQUIRED":
                    msg = f"{item['institution']} needs you to sign in again: disconnect and connect it again."
                with con:
                    con.execute("UPDATE plaid_items SET last_error = ? WHERE id = ?", (msg, item["id"]))
                errors.append(f"{item['institution']}: {msg}")
        return {"results": results, "errors": errors}
    finally:
        _lock.release()

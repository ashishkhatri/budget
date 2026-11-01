"""Flask web app: JSON API under /api plus the single-page UI in static/."""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_from_directory

from . import __version__
from . import db as dbm
from . import ai, duplicates, pipeline, plaid, receipts, recurring, reports
from .categorize import detail_key, merchant_key, pattern_matches
from .parsing import ParseError, parse_file

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BUDGET_DATA_DIR", ROOT / "data"))


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def create_app(db_path=None) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["DB_PATH"] = str(db_path or DATA_DIR / "budget.sqlite")
    app.config["BACKUP_DIR"] = str(Path(app.config["DB_PATH"]).parent / "backups")
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    dbm.init_db(app.config["DB_PATH"])
    _c = dbm.connect(app.config["DB_PATH"])
    try:
        with _c:
            pipeline.refresh_merchants(_c)
        receipts.clean_up(_c, app.config["DB_PATH"])
    finally:
        _c.close()

    def con():
        if "db" not in g:
            g.db = dbm.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.errorhandler(ApiError)
    def _api_err(e):
        return jsonify(error=str(e), **e.extra), e.status

    @app.errorhandler(ParseError)
    def _parse_err(e):
        return jsonify(error=str(e)), 400

    def body():
        return request.get_json(silent=True) or {}

    def category_or_400(cid):
        row = con().execute("SELECT * FROM categories WHERE id = ?", (cid,)).fetchone()
        if not row:
            raise ApiError("Unknown category.")
        return row

    # ---------------- pages ----------------
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon")

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():   # served from the root so it covers the whole app
        r = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
        r.headers["Cache-Control"] = "no-cache"
        return r

    # ---------------- meta ----------------
    @app.get("/api/meta")
    def meta():
        c = con()
        cats = [dict(r) for r in c.execute("""
            SELECT c.id, c.name, c.color, c.kind, COUNT(t.id) tx_count
            FROM categories c LEFT JOIN transactions t ON t.category_id = c.id
            GROUP BY c.id ORDER BY c.sort_order, c.name""")]
        accts = [dict(r) for r in c.execute("""
            SELECT a.id, a.name, a.institution, a.type, a.source, a.plaid_item, COUNT(t.id) tx_count,
                   MIN(t.date) first_date, MAX(t.date) last_date
            FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
            GROUP BY a.id ORDER BY a.name""")]
        total = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        backups = dbm.list_backups(app.config["BACKUP_DIR"])
        return jsonify(categories=cats, accounts=accts, months=reports.months(c), tx_total=total, version=__version__,
                       rule_count=c.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
                       db_path=app.config["DB_PATH"],
                       last_backup=backups[0].name if backups else None,
                       uncategorized=c.execute("""SELECT COUNT(*) FROM transactions t JOIN categories c
                           ON c.id = t.category_id WHERE c.name = 'Uncategorized'""").fetchone()[0],
                       ai_auto=ai.get_settings(c)["ai_auto"] == "1")

    # ---------------- dashboard / budgets ----------------
    @app.get("/api/summary")
    def summary():
        a = request.args
        start, end = a.get("start"), a.get("end")
        try:
            if start and end:
                from datetime import date
                date.fromisoformat(start); date.fromisoformat(end)
        except ValueError:
            raise ApiError("Dates must look like 2026-10-31.")
        return jsonify(reports.summary(con(), a.get("month"), a.get("account_id", type=int), start, end))

    @app.get("/api/budgets")
    def budgets():
        return jsonify(reports.budget_table(con(), request.args.get("month")))

    @app.put("/api/budgets/<int:cid>")
    def set_budget(cid):
        category_or_400(cid)
        cents = body().get("monthly_cents")
        c = con()
        with c:
            if cents is None or int(cents) <= 0:
                c.execute("DELETE FROM budgets WHERE category_id = ?", (cid,))
            else:
                c.execute("INSERT INTO budgets(category_id, monthly_cents) VALUES (?,?) "
                          "ON CONFLICT(category_id) DO UPDATE SET monthly_cents = excluded.monthly_cents", (cid, int(cents)))
        return jsonify(ok=True)

    # ---------------- transactions ----------------
    SORTS = {"date": "t.date", "description": "t.description", "merchant": "COALESCE(m.name, t.description) COLLATE NOCASE",
             "amount": "t.amount_cents", "account": "a.name"}

    @app.get("/api/transactions")
    def transactions():
        a = request.args
        where, args = ["1=1"], []

        def many(name, conv=int):
            """Comma-separated values: ?category_id=1,4,7"""
            out = []
            for v in (a.get(name) or "").split(","):
                v = v.strip()
                if v:
                    try:
                        out.append(conv(v))
                    except ValueError:
                        raise ApiError(f"Bad value for {name}: {v}")
            return out

        def month_ok(m):
            if len(m) != 7 or m[4] != "-":
                raise ValueError(m)
            return m

        months_ = many("month", month_ok)
        if months_:
            where.append(f"substr(t.date, 1, 7) IN ({','.join('?' * len(months_))})"); args += months_
        if a.get("from"):
            where.append("t.date >= ?"); args.append(a["from"])
        if a.get("to"):
            where.append("t.date <= ?"); args.append(a["to"])
        for name, col in (("category_id", "t.category_id"), ("account_id", "t.account_id"), ("merchant_id", "t.merchant_id")):
            vals = many(name)
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})"); args += vals
        if a.get("source") == "ai":
            where.append("t.category_source IN ('ai','learned')")
        q = dbm.squash(a.get("q"))
        if q:   # ignores punctuation and spaces: "heb" finds H-E-B, "7 eleven" finds 7-ELEVEN
            where.append("(instr(squash(t.description), ?) OR instr(squash(t.note), ?) OR instr(squash(m.name), ?))"); args += [q] * 3
        sort = SORTS.get(a.get("sort", "date"), "t.date")
        direction = "ASC" if a.get("dir") == "asc" else "DESC"
        limit = min(int(a.get("limit", 1500)), 10000)
        offset = max(int(a.get("offset", 0) or 0), 0)
        w = " AND ".join(where)
        c = con()
        stats = c.execute(f"""SELECT COUNT(*), COALESCE(SUM(t.amount_cents),0) FROM transactions t
                              LEFT JOIN merchants m ON m.id = t.merchant_id WHERE {w}""", args).fetchone()
        rows = c.execute(f"""
            SELECT t.id, t.date, t.description, t.amount_cents, t.category_id, t.category_source, t.merchant_id,
                   m.name merchant, t.note, t.pending, a.name account, t.account_id, t.receipt
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            LEFT JOIN merchants m ON m.id = t.merchant_id
            WHERE {w} ORDER BY {sort} {direction}, t.date DESC, t.id DESC LIMIT ? OFFSET ?""",
            [*args, limit, offset]).fetchall()
        return jsonify(items=[dict(r) for r in rows], total=stats[0], net_cents=stats[1])

    @app.post("/api/transactions")
    def add_transaction():
        """Add one transaction by hand (or from a scanned receipt)."""
        b = body()
        date = str(b.get("date") or "")
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            raise ApiError("Pick a date.")
        desc = " ".join(str(b.get("description") or "").split())[:200]
        if not desc:
            raise ApiError("Type who it was with (the merchant or a description).")
        try:
            amount = int(b.get("amount_cents"))
        except (TypeError, ValueError):
            raise ApiError("Type an amount.")
        if not amount:
            raise ApiError("The amount can't be zero.")
        c = con()
        with c:
            if b.get("account_id"):
                acct = c.execute("SELECT id FROM accounts WHERE id = ?", (int(b["account_id"]),)).fetchone()
                if not acct:
                    raise ApiError("Account not found.", 404)
                aid = acct[0]
            else:
                aid = pipeline.get_or_create_account(c, (b.get("account") or "Cash").strip()[:60], account_type="other",
                                                     source="manual")
            row = {"date": date, "description": desc, "amount_cents": amount}
            if b.get("category_id"):
                category_or_400(b["category_id"])
                row.update(category_id=int(b["category_id"]), manual=True)
            res = pipeline.ingest(c, aid, [row], source="manual", filename="Added by hand")
            if not res["inserted"]:
                raise ApiError("That account already has this exact transaction (same date, amount and description).", 409)
            tid = c.execute("SELECT MAX(id) FROM transactions WHERE import_id = ?", (res["import_id"],)).fetchone()[0]
            note = " ".join(str(b.get("note") or "").split())[:500]
            if note:
                c.execute("UPDATE transactions SET note = ? WHERE id = ?", (note, tid))
            receipts.attach(c, app.config["DB_PATH"], tid, b.get("receipt_token"))
        return jsonify(ok=True, id=tid)

    @app.post("/api/receipt")
    def read_receipt():
        """Read a receipt photo with the image model and find transactions it might already be."""
        f = request.files.get("image")
        if not f:
            raise ApiError("No photo received.")
        data = f.read()
        if len(data) > 15_000_000:
            raise ApiError("That photo is too large.")
        c = con()
        token = receipts.save_pending(app.config["DB_PATH"], data)     # kept small, whatever the AI says
        try:
            r = ai.parse_receipt(c, data)
        except ai.AIError as e:
            raise ApiError(str(e) + " The photo is still attached if you fill in the fields yourself.", 502,
                           receipt_token=token)
        r["receipt_token"] = token
        r["matches"] = []
        if r["total_cents"]:
            day = r["date"] or dt.date.today().isoformat()
            lo = (dt.date.fromisoformat(day) - dt.timedelta(days=4 if r["date"] else 45)).isoformat()
            hi = (dt.date.fromisoformat(day) + dt.timedelta(days=4)).isoformat()
            r["matches"] = [dict(x) for x in c.execute("""
                SELECT t.id, t.date, t.description, t.amount_cents, t.note, COALESCE(m.name, t.merchant) merchant, a.name account
                FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN merchants m ON m.id = t.merchant_id
                WHERE t.amount_cents = ? AND t.date BETWEEN ? AND ? ORDER BY ABS(julianday(t.date) - julianday(?)) LIMIT 5""",
                (-r["total_cents"], lo, hi, day))]
        return jsonify(r)

    @app.patch("/api/transactions/<int:tid>")
    def update_tx(tid):
        c = con()
        t = c.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
        if not t:
            raise ApiError("Transaction not found.", 404)
        b = body()
        out = {"ok": True}
        with c:
            if "category_id" in b:
                category_or_400(b["category_id"])
                c.execute("UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE id = ?",
                          (int(b["category_id"]), tid))
                m = c.execute("SELECT id, name, category_id FROM merchants WHERE id = ?", (t["merchant_id"],)).fetchone()
                if m:
                    similar = c.execute("SELECT COUNT(*) FROM transactions WHERE merchant_id = ? AND id != ? AND category_id != ?",
                                        (m["id"], tid, int(b["category_id"]))).fetchone()[0]
                    out.update(merchant_id=m["id"], merchant=m["name"], similar=similar,
                               merchant_has_category=m["category_id"] is not None)
            if "note" in b:
                c.execute("UPDATE transactions SET note = ? WHERE id = ?", ((b["note"] or "").strip() or None, tid))
            if b.get("receipt_token"):
                out["receipt"] = receipts.attach(c, app.config["DB_PATH"], tid, b["receipt_token"])
        return jsonify(out)

    @app.get("/api/receipts/<name>")
    def receipt_image(name):
        p = receipts.path_for(app.config["DB_PATH"], name)
        if not p:
            raise ApiError("Receipt not found.", 404)
        return send_from_directory(str(p.parent), p.name, mimetype="image/jpeg", max_age=86400)

    @app.post("/api/transactions/<int:tid>/receipt")
    def upload_receipt(tid):
        """Attach a photo to a transaction that's already there (no AI involved)."""
        f = request.files.get("image")
        if not f:
            raise ApiError("No photo received.")
        c = con()
        if not c.execute("SELECT 1 FROM transactions WHERE id = ?", (tid,)).fetchone():
            raise ApiError("Transaction not found.", 404)
        token = receipts.save_pending(app.config["DB_PATH"], f.read())
        with c:
            name = receipts.attach(c, app.config["DB_PATH"], tid, token)
        return jsonify(ok=True, receipt=name)

    @app.delete("/api/transactions/<int:tid>/receipt")
    def remove_receipt(tid):
        c = con()
        with c:
            c.execute("UPDATE transactions SET receipt = NULL WHERE id = ?", (tid,))
        receipts.clean_up(c, app.config["DB_PATH"])
        return jsonify(ok=True)

    @app.post("/api/transactions/categorize")
    def categorize_many():
        """Set one category on several transactions (counts as your own choice)."""
        b = body()
        ids = [int(i) for i in b.get("ids", [])]
        category_or_400(b.get("category_id"))
        c = con()
        with c:
            c.executemany("UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE id = ?",
                          [(int(b["category_id"]), i) for i in ids])
        return jsonify(changed=len(ids))

    @app.get("/api/duplicates")
    def duplicates_list():
        return jsonify(duplicates.find(con()))

    @app.post("/api/duplicates/dismiss")
    def duplicates_dismiss():
        """Mark a group as 'not duplicates' so it isn't suggested again."""
        ids = [int(i) for i in body().get("ids", [])]
        if len(ids) < 2:
            raise ApiError("Need at least two transactions.")
        c = con()
        with c:
            duplicates.dismiss(c, ids)
        return jsonify(ok=True)

    @app.get("/api/uncategorized")
    def uncategorized():
        """Uncategorized transactions grouped by merchant, biggest spend first."""
        c = con()
        rows = c.execute("""
            SELECT t.id, t.date, t.description, t.amount_cents, t.merchant_id, m.name merchant, a.name account
            FROM transactions t JOIN categories c ON c.id = t.category_id JOIN accounts a ON a.id = t.account_id
            LEFT JOIN merchants m ON m.id = t.merchant_id
            WHERE c.name = 'Uncategorized' ORDER BY t.date DESC""").fetchall()
        groups = {}
        for r in rows:
            g = groups.setdefault(r["merchant_id"] or r["description"], {
                "merchant": r["merchant"] or r["description"], "merchant_id": r["merchant_id"], "ids": [], "total_cents": 0,
                "first_date": r["date"], "last_date": r["date"], "examples": [], "accounts": set()})
            g["ids"].append(r["id"]); g["total_cents"] += r["amount_cents"]
            g["first_date"] = min(g["first_date"], r["date"]); g["last_date"] = max(g["last_date"], r["date"])
            if r["description"] not in g["examples"] and len(g["examples"]) < 3:
                g["examples"].append(r["description"])
            g["accounts"].add(r["account"])
        out = sorted(groups.values(), key=lambda g: (g["total_cents"], -len(g["ids"])))
        for g in out:
            g["accounts"] = sorted(g["accounts"])
        return jsonify(groups=out, total=len(rows))

    @app.post("/api/transactions/delete")
    def delete_tx():
        ids = [int(i) for i in body().get("ids", [])]
        c = con()
        with c:
            c.executemany("DELETE FROM transactions WHERE id = ?", [(i,) for i in ids])
        return jsonify(deleted=len(ids))

    # ---------------- import ----------------
    @app.post("/api/import/preview")
    def import_preview():
        f = request.files.get("file")
        if not f:
            raise ApiError("No file received.")
        data = f.read()
        sha = pipeline.file_hash(data)
        res = parse_file(data, f.filename or "")
        rows = pipeline.preview(con(), res.candidates, res.account)
        return jsonify(filename=f.filename, format=res.format, account=res.account, institution=res.institution,
                       account_type=res.account_type, notes=res.notes, rows=rows, file_sha256=sha,
                       already_imported=pipeline.find_file_import(con(), sha))

    @app.post("/api/import/recheck")
    def import_recheck():
        """Re-categorize (non-manual rows) and re-check duplicates after the user flips signs or renames the account."""
        b = body()
        c = con()
        cat = pipeline.Categorizer(c)
        rows = b.get("rows", [])
        for r in rows:
            if not r.get("manual"):
                cid, src = cat(r["description"], int(r["amount_cents"]), r.get("bank_category", ""), r.get("bank_type", ""))
                if src == "none" and r.get("category_source") in ("ai", "learned"):
                    continue  # keep the AI's suggestion
                r["category_id"], r["category_source"] = cid, src
        for r, d in zip(rows, pipeline.mark_duplicates(c, b.get("account", ""), rows)):
            r["dup"] = d
        return jsonify(rows=rows)

    @app.post("/api/import/commit")
    def import_commit():
        b = body()
        rows = b.get("rows", [])
        if not rows:
            raise ApiError("Nothing selected to import.")
        c = con()
        with c:
            acct_id = pipeline.get_or_create_account(c, b.get("account", ""), b.get("institution"),
                                                     b.get("account_type") or "other")
            res = pipeline.ingest(c, acct_id, rows, source="file", filename=b.get("filename"), fmt=b.get("format"),
                                 file_sha256=b.get("file_sha256"))
        name = c.execute("SELECT name FROM accounts WHERE id = ?", (acct_id,)).fetchone()[0]
        return jsonify(account=name, **res)

    @app.get("/api/imports")
    def imports():
        rows = con().execute("""
            SELECT i.id, i.source, i.filename, i.format, i.row_count, i.skipped_count, i.created_at, a.name account
            FROM imports i LEFT JOIN accounts a ON a.id = i.account_id ORDER BY i.id DESC LIMIT 20""").fetchall()
        return jsonify([dict(r) for r in rows])

    # ---------------- merchants ----------------
    MSORTS = {"spent": "spent_cents DESC", "count": "tx_count DESC", "name": "m.name COLLATE NOCASE ASC",
              "last": "last_date IS NULL, last_date DESC"}

    def merchant_or_404(mid):
        row = con().execute("SELECT * FROM merchants WHERE id = ?", (mid,)).fetchone()
        if not row:
            raise ApiError("Merchant not found.", 404)
        return row

    @app.get("/api/recurring")
    def recurring_list():
        return jsonify(recurring.detect(con()))

    @app.post("/api/recurring/manual")
    def recurring_manual():
        """Mark a merchant as recurring yourself ({merchant_id, frequency, remove?})."""
        b = body()
        merchant_or_404(b.get("merchant_id"))
        freq = b.get("frequency")
        if freq not in recurring.FREQ:
            raise ApiError("Pick how often it recurs.")
        c = con()
        with c:
            recurring.set_manual(c, int(b["merchant_id"]), freq, not b.get("remove"))
        return jsonify(ok=True)

    @app.put("/api/recurring/dismissed")
    def recurring_dismiss():
        b = request.get_json(force=True) or {}
        key = str(b.get("key") or "")
        if not key:
            raise ApiError("key is required")
        c = con()
        with c:
            recurring.set_dismissed(c, key, bool(b.get("dismissed", True)))
        return jsonify(ok=True)

    @app.get("/api/merchants")
    def merchants():
        a = request.args
        where, args = ["1=1"], []
        q = dbm.squash(a.get("q"))
        if q:
            where.append("""(instr(squash(m.name), ?) OR instr(squash(m.notes), ?) OR EXISTS (SELECT 1 FROM merchant_aliases x
                             WHERE x.merchant_id = m.id AND instr(squash(x.key), ?)))""")
            args += [q] * 3
        c = con()
        rows = [dict(r) for r in c.execute(f"""
            SELECT m.id, m.name, m.category_id, m.category_source, m.notes,
                   COUNT(t.id) tx_count,
                   COALESCE(-SUM(CASE WHEN tc.kind = 'expense' THEN t.amount_cents ELSE 0 END), 0) spent_cents,
                   MIN(t.date) first_date, MAX(t.date) last_date,
                   COUNT(DISTINCT t.category_id) n_categories
            FROM merchants m
            LEFT JOIN transactions t ON t.merchant_id = m.id
            LEFT JOIN categories tc ON tc.id = t.category_id
            WHERE {' AND '.join(where)} GROUP BY m.id
            ORDER BY {MSORTS.get(a.get('sort'), MSORTS['spent'])}, m.name""", args)]
        aliases = {}
        for key, mid in c.execute("SELECT key, merchant_id FROM merchant_aliases ORDER BY key"):
            aliases.setdefault(mid, []).append(key)
        top = {}
        for mid, cid, n in c.execute("""SELECT merchant_id, category_id, COUNT(*) FROM transactions
                                        WHERE merchant_id IS NOT NULL GROUP BY merchant_id, category_id"""):
            if mid not in top or n > top[mid][1]:
                top[mid] = (cid, n)
        variants, amounts = {}, {}
        for mid, desc, amt in c.execute(
                "SELECT merchant_id, description, amount_cents FROM transactions WHERE merchant_id IS NOT NULL"):
            variants.setdefault(mid, set()).add(detail_key(desc))
            amounts.setdefault(mid, []).append(amt)
        for r in rows:
            r["n_variants"] = len(variants.get(r["id"], ()))          # >1: offer to split it
            amts = amounts.get(r["id"], [])
            out_ = sorted(-a for a in amts if a < 0)                  # typical charge (refunds left out)
            pick = out_ or sorted(amts)
            # an even count takes the higher of the two middle charges (2 charges: the larger one)
            r["median_cents"] = pick[len(pick) // 2] if pick else None
            r["aliases"] = [k for k in aliases.get(r["id"], []) if k.upper() != r["name"].upper()]
            r["usual_category_id"] = top.get(r["id"], (None,))[0]      # most common, when none is set
        return jsonify(items=rows, total=len(rows))

    @app.patch("/api/merchants/<int:mid>")
    def edit_merchant(mid):
        """Rename, add notes, or set/clear the category. A category applies to ALL its transactions."""
        merchant_or_404(mid)
        b = body()
        c = con()
        out = {"ok": True}
        with c:
            if "name" in b:
                name = " ".join((b.get("name") or "").split()).upper()     # merchant names are always capitals
                if not name:
                    raise ApiError("The name can't be empty.")
                other = c.execute("SELECT id FROM merchants WHERE name = ? AND id != ?", (name, mid)).fetchone()
                if other:
                    raise ApiError(f"Another merchant is already called “{name}”. Use Merge to combine them.", 409)
                c.execute("UPDATE merchants SET name = ? WHERE id = ?", (name, mid))
            if "notes" in b:
                c.execute("UPDATE merchants SET notes = ? WHERE id = ?", ((b["notes"] or "").strip() or None, mid))
            if "category_id" in b:
                cid = b["category_id"]
                if cid is not None:
                    category_or_400(cid)
                out["changed"] = pipeline.set_merchant_category(c, mid, int(cid) if cid is not None else None)
        return jsonify(out)

    @app.post("/api/merchants/<int:mid>/merge")
    def merge_merchant(mid):
        """Fold this merchant into another one (its names and transactions move over)."""
        merchant_or_404(mid)
        target = body().get("into_id")
        merchant_or_404(target)
        if int(target) == mid:
            raise ApiError("Pick a different merchant to merge into.")
        c = con()
        with c:
            pipeline.merge_merchants(c, mid, int(target))
        return jsonify(ok=True)

    @app.get("/api/merchants/<int:mid>/split")
    def split_suggest(mid):
        """This merchant's transactions grouped by their longer name, to split it into several."""
        merchant_or_404(mid)
        return jsonify(groups=pipeline.split_groups(con(), mid))

    @app.post("/api/merchants/<int:mid>/split")
    def split_merchant(mid):
        merchant_or_404(mid)
        groups = body().get("groups") or []
        c = con()
        with c:
            made = pipeline.split_merchant(c, mid, groups)
        return jsonify(ok=True, merchants=made)

    @app.delete("/api/merchants/<int:mid>")
    def delete_merchant(mid):
        merchant_or_404(mid)
        c = con()
        if c.execute("SELECT 1 FROM transactions WHERE merchant_id = ? LIMIT 1", (mid,)).fetchone():
            raise ApiError("This merchant still has transactions. Merge it into another merchant instead.")
        with c:
            c.execute("DELETE FROM merchants WHERE id = ?", (mid,))
        return jsonify(ok=True)

    # ---------------- local AI ----------------
    # ---------------- bank sync (Plaid) ----------------
    def plaid_call(fn):
        try:
            return fn()
        except plaid.PlaidError as e:
            raise ApiError(str(e), 502)

    @app.get("/api/plaid")
    def plaid_status():
        return jsonify(plaid.public_settings(con()))

    @app.put("/api/plaid/settings")
    def plaid_save():
        b = body()
        c = con()
        with c:
            plaid.save_settings(c, b.get("client_id"), b.get("secret"), b.get("env"))
        return jsonify(plaid.public_settings(c))

    @app.post("/api/plaid/link-token")
    def plaid_link_token():
        return jsonify(link_token=plaid_call(lambda: plaid.link_token(con())))

    @app.post("/api/plaid/exchange")
    def plaid_exchange():
        """After you connect a bank in Plaid's window: save the connection and download transactions."""
        b = body()
        if not b.get("public_token"):
            raise ApiError("Missing public_token.")
        c = con()
        with c:
            item = plaid_call(lambda: plaid.exchange(c, b["public_token"], b.get("institution")))
        res = plaid_call(lambda: plaid.sync_all(c))
        return jsonify(item=item, sync=res)

    @app.post("/api/plaid/sync")
    def plaid_sync():
        return jsonify(plaid_call(lambda: plaid.sync_all(con())))

    @app.delete("/api/plaid/items/<int:iid>")
    def plaid_disconnect(iid):
        c = con()
        with c:
            plaid.disconnect(c, iid)
        return jsonify(ok=True)

    @app.get("/api/ai/settings")
    def ai_settings():
        return jsonify(ai.get_settings(con()))

    @app.put("/api/ai/settings")
    def ai_save_settings():
        b = body()
        if "ai_min_confidence" in b:
            try:
                v = float(b["ai_min_confidence"])
            except (TypeError, ValueError):
                raise ApiError("Confidence must be a number between 0 and 1.")
            if not 0 <= v <= 1:
                raise ApiError("Confidence must be between 0 and 1.")
        c = con()
        with c:
            ai.save_settings(c, b)
        return jsonify(ai.get_settings(c))

    @app.get("/api/ai/models")
    def ai_models():
        base = request.args.get("base_url") or ai.get_settings(con())["ai_base_url"]
        try:
            return jsonify(models=ai.list_models(base))
        except ai.AIError as e:
            raise ApiError(str(e), 502)

    @app.post("/api/ai/categorize")
    def ai_categorize():
        ids = body().get("ids")
        started = ai.start(app.config["DB_PATH"], [int(i) for i in ids] if ids else None)
        if not started:
            raise ApiError("AI categorization is already running.", 409)
        return jsonify(ai.status())

    @app.post("/api/ai/suggest")
    def ai_suggest():
        """Suggest categories for import-preview rows. Nothing is saved."""
        b = body()
        rows, token = b.get("rows", []), str(b.get("token") or "")
        stop = ai.cancel_event(token) if token else None
        try:
            return jsonify(results=ai.suggest(con(), rows, stop=stop))
        except ai.Cancelled:
            return jsonify(results=[None] * len(rows), cancelled=True)
        except ai.AIError as e:
            raise ApiError(str(e), 502)
        finally:
            if token:
                ai.release(token)

    @app.post("/api/ai/cancel")
    def ai_cancel():
        """Stop the import preview's AI requests (sent when you close the preview or leave Import)."""
        for token in body().get("tokens", []):
            ai.cancel(str(token))
        return jsonify(ok=True)

    @app.get("/api/ai/status")
    def ai_status():
        return jsonify(ai.status())

    # ---------------- rules ----------------
    @app.get("/api/rules")
    def rules():
        c = con()
        descs = [r[0] for r in c.execute("SELECT description FROM transactions")]
        out = []
        for r in c.execute("""SELECT r.id, r.pattern, r.category_id, c.name category, c.color
                              FROM rules r JOIN categories c ON c.id = r.category_id ORDER BY r.pattern"""):
            d = dict(r)
            d["matches"] = sum(1 for x in descs if pattern_matches(r["pattern"], x))
            out.append(d)
        return jsonify(out)

    @app.post("/api/rules")
    def add_rule():
        b = body()
        pattern = (b.get("pattern") or "").strip()
        if not pattern:
            raise ApiError("Enter some text to match.")
        category_or_400(b.get("category_id"))
        c = con()
        with c:
            c.execute("INSERT INTO rules(pattern, category_id) VALUES (?,?) "
                      "ON CONFLICT(pattern) DO UPDATE SET category_id = excluded.category_id",
                      (pattern, int(b["category_id"])))
            changed = pipeline.recategorize(c) if b.get("apply", True) else 0
        return jsonify(ok=True, changed=changed)

    @app.delete("/api/rules/<int:rid>")
    def delete_rule(rid):
        c = con()
        with c:
            c.execute("DELETE FROM rules WHERE id = ?", (rid,))
        return jsonify(ok=True)

    @app.post("/api/recategorize")
    def recategorize():
        c = con()
        with c:
            n = pipeline.recategorize(c, include_manual=bool(body().get("include_manual")))
        return jsonify(changed=n)

    # ---------------- categories ----------------
    @app.post("/api/categories")
    def add_category():
        b = body()
        name = (b.get("name") or "").strip()
        if not name:
            raise ApiError("Enter a category name.")
        kind = b.get("kind") if b.get("kind") in ("expense", "income", "transfer") else "expense"
        c = con()
        if c.execute("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            raise ApiError("That category already exists.")
        palette = ["#3d8b8b", "#b5838d", "#6d597a", "#e07a5f", "#81b29a", "#c9a227", "#5e548e"]
        n = c.execute("SELECT COUNT(*), MAX(sort_order) FROM categories").fetchone()
        with c:
            c.execute("INSERT INTO categories(name, color, kind, sort_order) VALUES (?,?,?,?)",
                      (name, b.get("color") or palette[n[0] % len(palette)], kind, (n[1] or 0) + 1))
        return jsonify(ok=True)

    @app.patch("/api/categories/<int:cid>")
    def edit_category(cid):
        row = category_or_400(cid)
        b = body()
        c = con()
        with c:
            if b.get("color"):
                c.execute("UPDATE categories SET color = ? WHERE id = ?", (b["color"], cid))
            if b.get("kind") in ("expense", "income", "transfer") and row["name"] not in dbm.PROTECTED_CATEGORIES:
                c.execute("UPDATE categories SET kind = ? WHERE id = ?", (b["kind"], cid))
            if b.get("name") and row["name"] not in dbm.PROTECTED_CATEGORIES:
                c.execute("UPDATE categories SET name = ? WHERE id = ?", (b["name"].strip(), cid))
        return jsonify(ok=True)

    @app.delete("/api/categories/<int:cid>")
    def delete_category(cid):
        row = category_or_400(cid)
        if row["name"] in dbm.PROTECTED_CATEGORIES:
            raise ApiError(f"“{row['name']}” is built in and can't be removed.")
        c = con()
        uncat = c.execute("SELECT id FROM categories WHERE name = 'Uncategorized'").fetchone()[0]
        with c:
            c.execute("UPDATE transactions SET category_id = ?, category_source = 'none' WHERE category_id = ?", (uncat, cid))
            c.execute("DELETE FROM categories WHERE id = ?", (cid,))
        return jsonify(ok=True)

    # ---------------- accounts ----------------
    @app.patch("/api/accounts/<int:aid>")
    def edit_account(aid):
        b = body()
        c = con()
        with c:
            if b.get("name"):
                other = pipeline.find_account(c, b["name"])
                if other and other["id"] != aid:
                    raise ApiError("Another account already has that name.")
                c.execute("UPDATE accounts SET name = ? WHERE id = ?", (b["name"].strip(), aid))
            if b.get("type") in ("checking", "savings", "credit", "other"):
                c.execute("UPDATE accounts SET type = ? WHERE id = ?", (b["type"], aid))
        return jsonify(ok=True)

    @app.delete("/api/accounts/<int:aid>")
    def delete_account(aid):
        dbm.backup(app.config["DB_PATH"], app.config["BACKUP_DIR"], "before-delete-account")
        c = con()
        with c:
            c.execute("DELETE FROM accounts WHERE id = ?", (aid,))
        return jsonify(ok=True)

    # ---------------- data ----------------
    @app.get("/api/backups")
    def backups():
        return jsonify([{"name": p.name, "size": p.stat().st_size} for p in dbm.list_backups(app.config["BACKUP_DIR"])][:30])

    @app.post("/api/backup")
    def make_backup():
        p = dbm.backup(app.config["DB_PATH"], app.config["BACKUP_DIR"], "manual")
        return jsonify(name=p.name, folder=str(p.parent))

    @app.get("/api/export.csv")
    def export_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Description", "Amount", "Category", "Account", "Note"])
        for r in con().execute("""SELECT t.date, t.description, t.amount_cents, c.name, a.name, t.note
                                  FROM transactions t JOIN categories c ON c.id = t.category_id
                                  JOIN accounts a ON a.id = t.account_id ORDER BY t.date, t.id"""):
            w.writerow([r[0], r[1], f"{r[2] / 100:.2f}", r[3], r[4], r[5] or ""])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=transactions.csv"})

    @app.post("/api/wipe")
    def wipe():
        if body().get("confirm") != "DELETE":
            raise ApiError("Confirmation missing.")
        p = dbm.backup(app.config["DB_PATH"], app.config["BACKUP_DIR"], "before-wipe")
        c = con()
        with c:
            for t in ("transactions", "imports", "rules", "budgets", "accounts", "merchant_patterns", "merchant_aliases", "merchants"):
                c.execute(f"DELETE FROM {t}")
            c.execute("UPDATE plaid_items SET cursor = NULL")      # bank connections stay; next sync starts over
        return jsonify(ok=True, backup=p.name)

    return app

"""Bank sync against a fake Plaid server."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from budget.app import create_app  # noqa: E402


def tx(tid, acct, date, amount, desc, pending=False, authorized=None, pfc=None):
    return {"transaction_id": tid, "account_id": acct, "date": date, "authorized_date": authorized or date,
            "amount": amount, "name": desc.title(), "original_description": desc, "pending": pending,
            "personal_finance_category": {"primary": (pfc or "GENERAL_MERCHANDISE").split(":")[0],
                                          "detailed": (pfc or "GENERAL_MERCHANDISE").replace(":", "_")}}


class FakePlaid(BaseHTTPRequestHandler):
    pages = []          # list of /transactions/sync responses, served in order by cursor
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakePlaid.calls.append((self.path, body))
        if body.get("client_id") != "cid" or body.get("secret") != "s3cret":
            return self._send(400, {"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret"})
        if self.path == "/link/token/create":
            assert body["products"] == ["transactions"]
            return self._send(200, {"link_token": "link-sandbox-123"})
        if self.path == "/item/public_token/exchange":
            return self._send(200, {"access_token": "access-sandbox-abc", "item_id": "item-1"})
        if self.path == "/accounts/get":
            return self._send(200, {"accounts": [
                {"account_id": "acc-card", "mask": "9743", "name": "Sapphire", "type": "credit", "subtype": "credit card"},
                {"account_id": "acc-chk", "mask": "3339", "name": "Total Checking", "type": "depository", "subtype": "checking"},
                {"account_id": "acc-new", "mask": "5555", "name": "Savings", "type": "depository", "subtype": "savings"}]})
        if self.path == "/transactions/sync":
            assert body["access_token"] == "access-sandbox-abc"
            i = int(body.get("cursor") or 0)
            if i >= len(FakePlaid.pages):
                return self._send(200, {"added": [], "modified": [], "removed": [], "next_cursor": str(i), "has_more": False})
            page = dict(FakePlaid.pages[i])
            page.setdefault("modified", []); page.setdefault("removed", [])
            page["next_cursor"] = str(i + 1)
            page["has_more"] = page.get("has_more", False)
            return self._send(200, page)
        if self.path == "/item/remove":
            return self._send(200, {})
        self._send(404, {"error_code": "NOT_FOUND"})

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestPlaid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlaid)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ["PLAID_BASE_URL"] = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.environ.pop("PLAID_BASE_URL", None)

    def setUp(self):
        FakePlaid.pages, FakePlaid.calls = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()
        # history already imported from a statement
        body = ("Date,Description,Amount\n10/01/2026,STARBUCKS STORE 1234 HOUSTON TX,-6.45\n"
                "10/02/2026,MTA*NYCT PAYGO,-3.00\n10/02/2026,MTA*NYCT PAYGO,-3.00\n")
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), "stmt.csv")},
                             content_type="multipart/form-data").json
        p["account"] = "Chase Credit Card 9743"
        self.client.post("/api/import/commit", json=p)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        r = self.client.put("/api/plaid/settings", json={"client_id": "cid", "secret": "s3cret", "env": "sandbox"}).json
        self.assertTrue(r["has_secret"])
        self.assertNotIn("s3cret", json.dumps(r))                                  # never sent back
        self.assertEqual(self.client.post("/api/plaid/link-token").json["link_token"], "link-sandbox-123")
        return self.client.post("/api/plaid/exchange", json={"public_token": "public-x",
                                                            "institution": {"name": "Chase", "institution_id": "ins_3"}}).json

    def txs(self):
        return self.client.get("/api/transactions").json["items"]

    def test_connect_matches_accounts_and_imported_history(self):
        FakePlaid.pages = [{"added": [
            # the same Starbucks charge as the statement: posted a day later -> matched, not added
            tx("t1", "acc-card", "2026-10-02", 6.45, "STARBUCKS STORE 1234 HOUSTON TX", authorized="2026-10-01",
               pfc="FOOD_AND_DRINK:COFFEE"),
            # two identical fares in the statement, three at the bank: one new
            tx("t2", "acc-card", "2026-10-02", 3.00, "MTA*NYCT PAYGO"),
            tx("t3", "acc-card", "2026-10-02", 3.00, "MTA*NYCT PAYGO"),
            tx("t4", "acc-card", "2026-10-03", 3.00, "MTA*NYCT PAYGO"),
            tx("t5", "acc-card", "2026-10-05", 54.20, "H-E-B #592 PEARLAND TX", pfc="FOOD_AND_DRINK:GROCERIES"),
            tx("t6", "acc-card", "2026-10-06", 12.00, "PENDING THING", pending=True),
            tx("t7", "acc-chk", "2026-10-07", -2500.00, "ACME CORP PAYROLL", pfc="INCOME:WAGES"),
        ], "has_more": True}, {"added": [tx("t8", "acc-chk", "2026-10-08", 1320.35, "PAYMENT TO CHASE CARD",
                                            pfc="LOAN_PAYMENTS:CREDIT_CARD_PAYMENT")]}]
        r = self.connect()
        names = {a["name"]: a["matched"] for a in r["item"]["accounts"]}
        self.assertEqual(names, {"Chase Credit Card 9743": True, "Chase Checking 3339": False, "Chase Savings 5555": False})
        s = r["sync"]["results"][0]
        self.assertEqual((s["added"], s["matched"]), (4, 3))
        items = self.txs()
        self.assertEqual(len(items), 3 + 4)
        heb = next(t for t in items if t["description"].startswith("H-E-B"))
        self.assertEqual(heb["amount_cents"], -5420)                              # Plaid's sign flipped
        pay = next(t for t in items if "PAYROLL" in t["description"])
        self.assertEqual(pay["amount_cents"], 250000)
        cats = {c["id"]: c["name"] for c in self.client.get("/api/meta").json["categories"]}
        self.assertEqual(cats[heb["category_id"]], "Groceries")
        self.assertFalse(any("PENDING" in t["description"] for t in items))
        # syncing again fetches nothing new and adds nothing
        again = self.client.post("/api/plaid/sync").json["results"][0]
        self.assertEqual(again["added"], 0)
        self.assertEqual(len(self.txs()), 7)

    def test_modified_removed_and_disconnect(self):
        FakePlaid.pages = [{"added": [tx("t5", "acc-card", "2026-10-05", 54.20, "H-E-B #592 PEARLAND TX")]},
                           {"modified": [tx("t5", "acc-card", "2026-10-05", 56.00, "H-E-B #592 PEARLAND TX")]},
                           {"removed": [{"transaction_id": "t5"}]}]
        self.connect()
        heb = lambda: [t for t in self.txs() if t["description"].startswith("H-E-B")]
        self.assertEqual(heb()[0]["amount_cents"], -5420)
        self.client.post("/api/plaid/sync")
        self.assertEqual(heb()[0]["amount_cents"], -5600)
        self.client.post("/api/plaid/sync")
        self.assertEqual(heb(), [])
        st = self.client.get("/api/plaid").json
        self.assertEqual(len(st["items"]), 1)
        self.assertEqual(st["items"][0]["institution"], "Chase")
        self.client.delete(f"/api/plaid/items/{st['items'][0]['id']}")
        self.assertEqual(self.client.get("/api/plaid").json["items"], [])
        self.assertTrue(any(p == "/item/remove" for p, _ in FakePlaid.calls))
        accts = {a["name"]: a for a in self.client.get("/api/meta").json["accounts"]}
        self.assertIsNone(accts["Chase Credit Card 9743"]["plaid_item"])
        self.assertEqual(len(self.txs()), 3)                                       # history kept

    def test_older_history_and_catch_all_account_are_not_doubled(self):
        # old history sits in a catch-all account without a card number
        body = "Date,Description,Amount\n05/03/2026,NETFLIX.COM,-15.49\n05/20/2026,SHELL OIL 5544,-41.00\n"
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), "old.csv")},
                             content_type="multipart/form-data").json
        p["account"] = "old transactions-unknown acct"
        self.client.post("/api/import/commit", json=p)
        FakePlaid.pages = [{"added": [
            tx("o1", "acc-card", "2026-05-03", 15.49, "NETFLIX.COM"),            # before the card's first file row
            tx("o2", "acc-new", "2026-05-20", 41.00, "SHELL OIL 5544"),          # new account, but in the catch-all
            tx("o3", "acc-new", "2026-05-21", 12.00, "TARGET 123"),              # genuinely new
        ]}]
        s = self.connect()["sync"]["results"][0]
        self.assertEqual((s["added"], s["matched"]), (1, 1))
        self.assertEqual(len(self.txs()), 3 + 2 + 1)

    def test_bad_keys_give_a_clear_error(self):
        self.client.put("/api/plaid/settings", json={"client_id": "cid", "secret": "wrong"})
        r = self.client.post("/api/plaid/link-token")
        self.assertEqual(r.status_code, 502)
        self.assertIn("invalid client_id or secret", r.json["error"])

    def test_secret_is_not_stored_readably(self):
        self.client.put("/api/plaid/settings", json={"client_id": "cid", "secret": "s3cret"})
        import sqlite3
        raw = sqlite3.connect(os.path.join(self.tmp.name, "t.sqlite")).execute(
            "SELECT value FROM settings WHERE key = 'plaid_secret'").fetchone()[0]
        self.assertNotIn("s3cret", raw)


if __name__ == "__main__":
    unittest.main()

"""Run with:  .venv\\Scripts\\python -m unittest discover tests   (or: python -m unittest discover tests)"""
import io
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"

from budget import db as dbm  # noqa: E402
from budget import pipeline  # noqa: E402
from budget.categorize import categorize, merchant_key  # noqa: E402
from budget.parsing import parse_csv, parse_file  # noqa: E402
from budget.parsing.common import parse_amount, parse_date  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_amounts(self):
        cases = {"$1,234.56": 123456, "(12.00)": -1200, "12.00-": -1200, "-5": -500, "3.10 CR": -310,
                 "+7.5": 750, "": None, "abc": None, "0.005": 1}
        for s, want in cases.items():
            self.assertEqual(parse_amount(s), want, s)

    def test_dates(self):
        self.assertEqual(parse_date("10/05/2026"), "2026-10-05")
        self.assertEqual(parse_date("1/5/26"), "2026-01-05")
        self.assertEqual(parse_date("2026-10-05"), "2026-10-05")
        self.assertIsNone(parse_date("13/40/2026"))
        self.assertIsNone(parse_date("Posting Date"))


class TestCategorize(unittest.TestCase):
    def cat(self, d, a=-100, **kw):
        return categorize(d, a, **kw)[0]

    def test_keywords(self):
        self.assertEqual(self.cat("UBER *EATS"), "Dining")
        self.assertEqual(self.cat("UBER *TRIP"), "Transportation")
        self.assertEqual(self.cat("COSTCO GAS #123"), "Gas & Fuel")
        self.assertEqual(self.cat("COSTCO WHSE #123"), "Groceries")
        self.assertEqual(self.cat("CHASE CREDIT CRD AUTOPAY"), "Transfers")
        self.assertEqual(self.cat("ACME PAYROLL", 100000), "Income")

    def test_word_boundaries(self):
        self.assertNotEqual(self.cat("CURRENT ACCOUNT ADJ"), "Rent & Mortgage")
        self.assertEqual(self.cat("GREYSTAR RENT"), "Rent & Mortgage")

    def test_rules_win(self):
        self.assertEqual(categorize("KROGER #1", -100, rules=[("KROGER", "Shopping")]), ("Shopping", "rule"))

    def test_withdrawal_prefix_is_not_cash(self):
        self.assertEqual(self.cat("WITHDRAWAL ACH CITI AUTOPAY TYPE: PAYMENT CO: CITI AUTOPAY"), "Transfers")
        self.assertEqual(self.cat("ATM WITHDRAWAL 001234 MAIN ST"), "Cash & ATM")

    def test_bank_category_fallback(self):
        self.assertEqual(categorize("SOME LOCAL PLACE", -100, bank_category="Food & Drink"), ("Dining", "bank"))

    def test_merchant_key(self):
        self.assertEqual(merchant_key("Card Purchase 09/14 STARBUCKS STORE 1234 CHICAGO"), "STARBUCKS STORE")
        self.assertEqual(merchant_key("AMAZON.COM*AB12C3D45"), "AMAZON.COM")
        self.assertEqual(merchant_key("COMED PAYMENT PPD ID: 1360938990"), "COMED PAYMENT")
        self.assertEqual(merchant_key("TST* JOES BBQ"), "JOES BBQ")
        # store numbers and city spellings must not split one merchant into two
        self.assertEqual(merchant_key("0252-GAYLORD TEXAN GRAPVINE TX"), "GAYLORD TEXAN")
        self.assertEqual(merchant_key("0252 GAYLORD TEXAN GRAPEVINE TX"), "GAYLORD TEXAN")
        self.assertEqual(merchant_key("7-ELEVEN 12345 HOUSTON TX"), "7-ELEVEN")
        self.assertEqual(merchant_key("UBER *EATS"), "UBER EATS")
        self.assertEqual(merchant_key("UBER *TRIP"), "UBER TRIP")
        # ACH descriptions: use the company after "CO:", not "DEPOSIT ACH"
        self.assertEqual(merchant_key("DEPOSIT ACH MARSH & MCLENNAN TYPE: DIRECT-PAY CO: MARSH & MCLENNAN ENTRY CLASS CODE: PPD"),
                         "MARSH & MCLENNAN")
        self.assertEqual(merchant_key("WITHDRAWAL ACH CITI AUTOPAY TYPE: PAYMENT CO: CITI AUTOPAY NAME: J DOE ENTRY CLASS"),
                         "CITI AUTOPAY")
        self.assertEqual(merchant_key("ACH DEBIT GEICO INSURANCE"), "GEICO INSURANCE")
        # 'MERCHANT - LOCATION' and trailing punctuation
        self.assertEqual(merchant_key("TACODELI - KATY KATY TX"), "TACODELI")
        self.assertEqual(merchant_key("76 - NEW CRESWELL CR CRESWELL OR"), "76")
        self.assertEqual(merchant_key("X CORP. PAID FEATURES"), "X CORP")
        self.assertEqual(merchant_key("THE HOME DEPOT #6543"), "THE HOME DEPOT")


class TestParsers(unittest.TestCase):
    def load(self, name):
        return parse_file((SAMPLES / name).read_bytes(), name)

    def test_chase_checking(self):
        r = self.load("Chase_Checking_sample.csv")
        self.assertEqual(r.format, "Chase checking/savings")
        self.assertEqual(len(r.candidates), 11)
        self.assertEqual(r.candidates[0].amount_cents, 285000)
        self.assertEqual(r.candidates[1].amount_cents, -124318)

    def test_chase_credit(self):
        r = self.load("Chase_CreditCard_sample.csv")
        self.assertEqual(r.account_type, "credit")
        self.assertEqual(r.candidates[0].bank_category, "Food & Drink")
        self.assertEqual(r.candidates[0].amount_cents, -645)

    def test_amex_flipped(self):
        r = self.load("amex_activity.csv")
        self.assertEqual(r.candidates[0].amount_cents, -6412)   # charge -> money out
        self.assertEqual(r.candidates[1].amount_cents, 30000)   # payment -> money in

    def test_pdf(self):
        r = self.load("Chase_Statement_sample.pdf")
        self.assertEqual(len(r.candidates), 8)
        by = {c.description: c for c in r.candidates}
        self.assertEqual(by["HOME DEPOT #6543 HOUSTON TX"].amount_cents, -120355)
        self.assertEqual(by["Payment Thank You - Web"].amount_cents, 50000)
        self.assertEqual(by["HEB #432 HOUSTON TX"].date, "2026-09-16")

    def test_wells_fargo_headerless(self):
        data = b'"10/01/2026","-12.34","*","","KROGER #1"\n"10/02/2026","1000.00","*","","PAYROLL"\n'
        r = parse_csv(data, "Checking1.csv")
        self.assertEqual(r.format, "Wells Fargo")
        self.assertEqual([c.amount_cents for c in r.candidates], [-1234, 100000])

    def test_chase_last4_in_account_name(self):
        r = parse_file((SAMPLES / "Chase_Statement_sample.pdf").read_bytes(), "20261009-statements-9743-.pdf")
        self.assertEqual(r.account, "Chase Credit Card 9743")
        r = parse_file((SAMPLES / "Chase_CreditCard_sample.csv").read_bytes(), "Chase9748_Activity_20261018.csv")
        self.assertEqual(r.account, "Chase Credit Card 9748")
        self.assertEqual(self.load("Chase_CreditCard_sample.csv").account, "Chase Credit Card")

    def test_credit_or_debit_direction_column(self):
        data = (b'"Account Name","Processed Date","Description","Check Number","Credit or Debit","Amount"\n'
                b'"MY SPENDING",2026-09-24,"DEPOSIT ACH ACME TYPE: DIRECT-PAY CO: ACME CORP ENTRY CLASS CODE: PPD",,"Credit",692.62\n'
                b'"MY SPENDING",2026-09-21,"WITHDRAWAL ACH CITI AUTOPAY TYPE: PAYMENT CO: CITI AUTOPAY",,"Debit",2489.79\n'
                b'"MY SPENDING",2026-09-20,"KROGER #12",,"DEBIT",12.50\n\n')
        r = parse_csv(data, "export.csv")
        self.assertEqual([c.amount_cents for c in r.candidates], [69262, -248979, -1250])
        self.assertEqual(r.account, "MY SPENDING")
        self.assertEqual(r.notes, [])                       # no "flipped signs" guess

    def test_type_column_with_other_values_is_not_a_direction(self):
        r = self.load("Chase_CreditCard_sample.csv")          # Type = Sale/Payment/Return
        self.assertEqual(r.candidates[0].amount_cents, -645)
        self.assertEqual(r.candidates[6].amount_cents, 124318)

    def test_debit_credit_columns(self):
        data = b"Date,Description,Debit,Credit\n10/01/2026,KROGER,12.34,\n10/02/2026,REFUND,,5.00\n"
        r = parse_csv(data, "x.csv")
        self.assertEqual([c.amount_cents for c in r.candidates], [-1234, 500])


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sqlite")
        dbm.init_db(self.path)
        self.con = dbm.connect(self.path)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def rows(self):
        return [dict(date="2026-10-01", description="STARBUCKS", amount_cents=-500)] * 2 + \
               [dict(date="2026-10-02", description="KROGER", amount_cents=-2000)]

    def test_dedupe_counts_multiples(self):
        with self.con:
            aid = pipeline.get_or_create_account(self.con, "Test")
            r1 = pipeline.ingest(self.con, aid, self.rows()[:1])     # one coffee already there
            r2 = pipeline.ingest(self.con, aid, self.rows())         # file has two coffees + groceries
        self.assertEqual((r1["inserted"], r2["inserted"], r2["skipped"]), (1, 2, 1))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 3)

    def test_external_ids_dedupe(self):
        rows = [dict(date="2026-10-01", description="X", amount_cents=-1, external_id="abc")]
        with self.con:
            aid = pipeline.get_or_create_account(self.con, "API")
            pipeline.ingest(self.con, aid, rows, source="test")
            r = pipeline.ingest(self.con, aid, rows, source="test")
        self.assertEqual((r["inserted"], r["skipped"]), (0, 1))

    def test_rule_and_recategorize_keep_manual(self):
        ids = pipeline.category_ids(self.con)
        with self.con:
            aid = pipeline.get_or_create_account(self.con, "Test")
            pipeline.ingest(self.con, aid, [dict(date="2026-10-01", description="MYSTERY SHOP", amount_cents=-100),
                                            dict(date="2026-10-02", description="MYSTERY SHOP", amount_cents=-200,
                                                 category_id=ids["travel"], manual=True)])
            self.con.execute("INSERT INTO rules(pattern, category_id) VALUES ('mystery shop', ?)", (ids["shopping"],))
            changed = pipeline.recategorize(self.con)
        cats = [r[0] for r in self.con.execute("SELECT category_id FROM transactions ORDER BY date")]
        self.assertEqual(changed, 1)
        self.assertEqual(cats, [ids["shopping"], ids["travel"]])

    def test_migrations_idempotent(self):
        dbm.init_db(self.path)
        self.assertEqual(self.con.execute("PRAGMA user_version").fetchone()[0], max(dbm.MIGRATIONS))


class TestFileFingerprint(unittest.TestCase):
    def setUp(self):
        from budget.app import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sqlite")
        self.client = create_app(self.path).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, name="Chase_Checking_sample.csv", upload_as=None):
        data = (SAMPLES / name).read_bytes()
        return self.client.post("/api/import/preview", data={"file": (io.BytesIO(data), upload_as or name)},
                                content_type="multipart/form-data").json

    def commit(self, p):
        return self.client.post("/api/import/commit", json={**p, "rows": p["rows"]}).json

    def test_same_file_is_recognized_even_if_renamed(self):
        p = self.preview()
        self.assertIsNone(p["already_imported"])
        self.commit(p)
        again = self.preview(upload_as="renamed copy.csv")
        self.assertEqual(again["already_imported"]["account"], "Chase Checking")
        self.assertIsNone(self.preview("amex_activity.csv")["already_imported"])

    def test_allowed_again_after_its_transactions_are_deleted(self):
        self.commit(self.preview())
        aid = self.client.get("/api/meta").json["accounts"][0]["id"]
        self.client.delete(f"/api/accounts/{aid}")
        self.assertIsNone(self.preview()["already_imported"])

    def test_upgrade_from_v1_database(self):
        import sqlite3
        old = os.path.join(self.tmp.name, "old.sqlite")
        con = sqlite3.connect(old)
        con.executescript(dbm.MIGRATIONS[1]); con.execute("PRAGMA user_version = 1"); con.commit(); con.close()
        dbm.init_db(old)
        con = dbm.connect(old)
        cols = [r[1] for r in con.execute("PRAGMA table_info(imports)")]
        con.close()
        self.assertIn("file_sha256", cols)


class TestMerchants(unittest.TestCase):
    def setUp(self):
        from budget.app import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()
        for f in ("Chase_Checking_sample.csv", "Chase_CreditCard_sample.csv"):
            self.imp(f)
        self.cats = {c["name"]: c["id"] for c in self.client.get("/api/meta").json["categories"]}

    def tearDown(self):
        self.tmp.cleanup()

    def imp(self, name, data=None, account=None):
        data = data if data is not None else (SAMPLES / name).read_bytes()
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(data), name)},
                             content_type="multipart/form-data").json
        if account:
            p["account"] = account
        return self.client.post("/api/import/commit", json=p).json

    def merchant(self, name):
        return next(m for m in self.client.get("/api/merchants").json["items"] if m["name"] == name)

    def txs(self, mid):
        return self.client.get(f"/api/transactions?merchant_id={mid}").json["items"]

    def test_every_transaction_is_linked_to_a_merchant(self):
        items = self.client.get("/api/transactions").json["items"]
        self.assertTrue(all(t["merchant_id"] for t in items))
        acme = self.merchant("ACME CORP")
        self.assertEqual(acme["tx_count"], 3)
        self.assertEqual(len(self.txs(acme["id"])), 3)

    def test_setting_category_applies_to_all_including_hand_edits_and_future_imports(self):
        m = self.merchant("ACME CORP")
        first = self.txs(m["id"])[0]["id"]
        self.client.patch(f"/api/transactions/{first}", json={"category_id": self.cats["Travel"]})   # hand edit
        r = self.client.patch(f"/api/merchants/{m['id']}", json={"category_id": self.cats["Education"]}).json
        self.assertEqual(r["changed"], 3)
        self.assertTrue(all(t["category_id"] == self.cats["Education"] for t in self.txs(m["id"])))
        # a later one-off change on a single transaction is still allowed
        self.client.patch(f"/api/transactions/{first}", json={"category_id": self.cats["Travel"]})
        # future imports follow the merchant
        self.imp("payroll.csv", b"Date,Description,Amount\n11/01/2026,ACME CORP PAYROLL PPD ID: 99,2850.00\n", "Chase Checking")
        new = [t for t in self.txs(m["id"]) if t["date"] == "2026-11-01"][0]
        self.assertEqual((new["category_id"], new["category_source"]), (self.cats["Education"], "merchant"))

    def test_merchants_survive_deleting_transactions(self):
        m = self.merchant("MYSTERY SHOP")
        self.client.patch(f"/api/merchants/{m['id']}", json={"category_id": self.cats["Shopping"], "name": "MYSTERY SHOP CO"})
        ids = [t["id"] for t in self.client.get("/api/transactions").json["items"]]
        self.client.post("/api/transactions/delete", json={"ids": ids})
        kept = self.merchant("MYSTERY SHOP CO")
        self.assertEqual((kept["tx_count"], kept["category_id"]), (0, self.cats["Shopping"]))
        self.imp("Chase_Checking_sample.csv")                                   # re-import
        tx = self.txs(kept["id"])
        self.assertEqual((len(tx), tx[0]["category_id"], tx[0]["merchant"]), (1, self.cats["Shopping"], "MYSTERY SHOP CO"))

    def test_merge_moves_transactions_and_alternate_names(self):
        amazon, ret = self.merchant("AMAZON.COM"), self.merchant("AMAZON.COM RETURN")
        self.client.patch(f"/api/merchants/{amazon['id']}", json={"name": "Amazon", "category_id": self.cats["Shopping"]})
        self.client.post(f"/api/merchants/{ret['id']}/merge", json={"into_id": amazon["id"]})
        a = self.merchant("AMAZON")
        self.assertEqual(a["tx_count"], 2)
        self.assertIn("AMAZON.COM RETURN", a["aliases"])
        self.assertTrue(all(t["category_id"] == self.cats["Shopping"] for t in self.txs(a["id"])))
        names = [m["name"] for m in self.client.get("/api/merchants").json["items"]]
        self.assertNotIn("AMAZON.COM RETURN", names)

    def test_transactions_search_and_sort_by_merchant_name(self):
        m = self.merchant("KROGER")
        self.client.patch(f"/api/merchants/{m['id']}", json={"name": "KROGER MARKETPLACE"})
        r = self.client.get("/api/transactions?q=marketplace").json
        self.assertEqual((r["total"], r["items"][0]["merchant"]), (1, "KROGER MARKETPLACE"))
        names = [t["merchant"] for t in self.client.get("/api/transactions?sort=merchant&dir=asc").json["items"]]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_search_ignores_punctuation_and_spaces(self):
        q = lambda s: self.client.get("/api/transactions?" + urllib.parse.urlencode({"q": s})).json["total"]
        exact = q("AMAZON.COM")
        self.assertGreater(exact, 0)
        self.assertEqual(q("amazoncom"), exact)
        self.assertEqual(q("amazon com"), exact)
        self.assertEqual(q("Acme-Corp"), q("ACME CORP"))
        self.assertEqual(q("..."), self.client.get("/api/transactions").json["total"])   # nothing left = no filter
        ms = lambda s: [m["name"] for m in self.client.get("/api/merchants?" + urllib.parse.urlencode({"q": s})).json["items"]]
        self.assertIn("AMAZON.COM", ms("amazoncom"))

    def test_multi_value_filters(self):
        acme, kroger = self.merchant("ACME CORP"), self.merchant("KROGER")
        r = self.client.get(f"/api/transactions?merchant_id={acme['id']},{kroger['id']}").json
        self.assertEqual(r["total"], 4)
        both = self.client.get("/api/transactions?month=2026-09,2026-10").json["total"]
        self.assertEqual(both, self.client.get("/api/transactions").json["total"])
        sep = self.client.get("/api/transactions?month=2026-09").json["total"]
        self.assertLess(sep, both)
        cats = f"{self.cats['Income']},{self.cats['Groceries']}"
        self.assertEqual(self.client.get(f"/api/transactions?category_id={cats}").json["total"], 5)
        self.assertEqual(self.client.get("/api/transactions?category_id=abc").status_code, 400)

    def test_rename_conflict_and_delete_rules(self):
        m = self.merchant("KROGER")
        r = self.client.patch(f"/api/merchants/{m['id']}", json={"name": "ACME CORP"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.delete(f"/api/merchants/{m['id']}").status_code, 400)   # still has transactions

    def test_clearing_category_goes_back_to_automatic(self):
        m = self.merchant("KROGER")
        self.client.patch(f"/api/merchants/{m['id']}", json={"category_id": self.cats["Dining"]})
        self.client.patch(f"/api/merchants/{m['id']}", json={"category_id": None})
        t = self.txs(m["id"])[0]
        self.assertEqual((t["category_id"], t["category_source"]), (self.cats["Groceries"], "builtin"))

    def test_needs_review_groups_and_bulk_file(self):
        groups = self.client.get("/api/uncategorized").json["groups"]
        self.assertEqual({g["merchant"] for g in groups}, {"ZELLE PAYMENT", "MYSTERY SHOP"})
        z = next(g for g in groups if g["merchant"] == "ZELLE PAYMENT")
        self.assertTrue(z["merchant_id"])
        self.client.post("/api/transactions/categorize", json={"ids": z["ids"], "category_id": self.cats["Gifts & Donations"]})
        self.assertEqual(self.client.get("/api/uncategorized").json["total"], 1)
        self.assertIsNone(self.merchant("ZELLE PAYMENT")["category_id"])        # "just these" leaves the merchant alone

    def test_migration_from_v2_creates_merchants_and_converts_rules(self):
        import sqlite3
        old = os.path.join(self.tmp.name, "old.sqlite")
        con = sqlite3.connect(old)
        con.executescript(dbm.MIGRATIONS[1]); con.executescript(dbm.MIGRATIONS[2])
        con.execute("PRAGMA user_version = 2")
        con.execute("INSERT INTO categories(id, name, kind) VALUES (1,'Uncategorized','expense'),(2,'Shopping','expense')")
        con.execute("INSERT INTO accounts(id, name) VALUES (1,'A')")
        con.execute("INSERT INTO transactions(account_id,date,description,amount_cents,category_id,merchant) "
                    "VALUES (1,'2026-10-01','KROGER #1',-100,1,'KROGER'),(1,'2026-10-02','KROGER #2',-100,1,'KROGER')")
        con.execute("INSERT INTO rules(pattern, category_id) VALUES ('KROGER', 2), ('WEIRD PATTERN', 2)")
        con.commit(); con.close()
        dbm.init_db(old)
        con = dbm.connect(old)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM merchants").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT category_id, category_source FROM merchants").fetchone()[:], (2, "manual"))
        self.assertEqual([r[0] for r in con.execute("SELECT pattern FROM rules")], ["WEIRD PATTERN"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM transactions WHERE merchant_id IS NULL").fetchone()[0], 0)
        con.close()


class TestDashboardPeriods(unittest.TestCase):
    def setUp(self):
        from budget.app import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()
        csv = ("Date,Description,Amount\n"
               "07/05/2026,KROGER #1,-100.00\n08/05/2026,KROGER #1,-200.00\n09/05/2026,KROGER #1,-300.00\n"
               "10/05/2026,KROGER #1,-400.00\n10/15/2026,ACME PAYROLL,1000.00\n06/05/2026,KROGER #1,-50.00\n").encode()
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(csv), "x.csv")},
                             content_type="multipart/form-data").json
        self.client.post("/api/import/commit", json=p)
        groc = next(c["id"] for c in self.client.get("/api/meta").json["categories"] if c["name"] == "Groceries")
        self.client.put(f"/api/budgets/{groc}", json={"monthly_cents": 25000})

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, **q):
        from urllib.parse import urlencode
        return self.client.get("/api/summary?" + urlencode(q)).json

    def test_default_is_latest_month(self):
        s = self.get()
        self.assertEqual((s["start"], s["end"], s["spend_cents"], s["n_months"]), ("2026-10-01", "2026-10-31", 40000, 1))

    def test_three_month_period_with_previous_and_scaled_budget(self):
        s = self.get(start="2026-08-01", end="2026-10-31")
        self.assertEqual((s["spend_cents"], s["income_cents"], s["n_months"]), (90000, 100000, 3))
        self.assertEqual((s["prev_start"], s["prev_end"], s["prev_spend_cents"]), ("2026-05-01", "2026-07-31", 15000))
        groc = next(r for r in s["by_category"] if r["name"] == "Groceries")
        self.assertEqual(groc["budget_cents"], 75000)            # $250/month x 3
        self.assertEqual([t["month"] for t in s["trend"] if t["in_period"]], ["2026-08", "2026-09", "2026-10"])

    def test_budgets_only_cover_months_with_data(self):
        s = self.get(start="2026-01-01", end="2026-12-31")            # data is Jun–Oct
        groc = next(r for r in s["by_category"] if r["name"] == "Groceries")
        self.assertEqual((s["budget_months"], groc["budget_cents"]), (5, 125000))

    def test_custom_range_not_whole_months(self):
        s = self.get(start="2026-09-01", end="2026-10-10")
        self.assertEqual(s["spend_cents"], 70000)
        self.assertIsNone(s["n_months"])
        self.assertAlmostEqual(s["months_factor"], 1.31, places=2)  # 40 days

    def test_transactions_date_range_filter(self):
        r = self.client.get("/api/transactions?from=2026-08-01&to=2026-09-30").json
        self.assertEqual(r["total"], 2)


if __name__ == "__main__":
    unittest.main()

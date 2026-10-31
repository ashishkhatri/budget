import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from budget.app import create_app  # noqa: E402


class TestDuplicates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def imp(self, name, account, rows):
        body = "Date,Description,Amount\n" + "".join(f"{d},{x},{a}\n" for d, x, a in rows)
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), name)},
                             content_type="multipart/form-data").json
        p["account"] = account
        p["rows"] = [{**r, "include": True} for r in p["rows"]]
        return self.client.post("/api/import/commit", json=p).json

    def dups(self):
        return self.client.get("/api/duplicates").json

    def test_same_charge_read_differently_by_two_files(self):
        self.imp("sept.csv", "Card 1234", [("09/03/2026", "STARBUCKS STORE 123 HOUSTON TX", "-6.45"),
                                           ("09/05/2026", "SHELL OIL 5544", "-40.00")])
        # the PDF version: posting date a day later, description cut differently
        self.imp("sept.pdf.csv", "Card 1234", [("09/04/2026", "STARBUCKS STORE 123 HOUSTON", "-6.45"),
                                               ("09/05/2026", "SHELL OIL 5544", "-41.00")])
        d = self.dups()
        self.assertEqual(d["total"], 1)
        g = d["groups"][0]
        self.assertEqual(g["amount_cents"], -645)
        self.assertIn("1 day apart", g["reason"])
        self.assertEqual(g["keep_id"], min(g["ids"]))          # the first import is kept

    def test_statement_imported_into_a_generic_and_a_specific_account(self):
        rows = [("09/03/2026", "NETFLIX.COM", "-15.49"), ("09/04/2026", "KROGER #5", "-80.10")]
        self.imp("stmt.csv", "Chase Credit Card", rows)
        self.imp("stmt2.csv", "Chase Credit Card 9743", rows)
        d = self.dups()
        self.assertEqual(d["total"], 2)
        self.assertTrue(all("two accounts" in g["reason"] for g in d["groups"]))

    def test_not_duplicates(self):
        # two cards, the same subway fare each: two people travelling together
        self.imp("a.csv", "Card 1111", [("09/30/2026", "MTA*NYCT PAYGO NEW YORK NY", "-3.00")])
        self.imp("b.csv", "Card 2222", [("09/30/2026", "MTA*NYCT PAYGO NEW YORK NY", "-3.00"),
                                        ("09/29/2026", "CVS 123", "-4.10")])
        # two coffees in one statement are two coffees
        self.imp("c.csv", "Card 1111", [("10/01/2026", "BLUE BOTTLE", "-5.00"), ("10/01/2026", "BLUE BOTTLE", "-5.00")])
        # same amount, different merchant
        self.imp("d.csv", "Card 1111", [("10/02/2026", "TARGET 123", "-20.00")])
        self.imp("e.csv", "Card 1111", [("10/03/2026", "WALGREENS 9", "-20.00")])
        # same merchant and amount a week apart (weekly class)
        self.imp("f.csv", "Card 1111", [("10/05/2026", "YOGA STUDIO", "-20.00")])
        self.imp("g.csv", "Card 1111", [("10/12/2026", "YOGA STUDIO", "-20.00")])
        # same chain and amount, two different stores
        self.imp("h.csv", "Card 1111", [("10/06/2026", "CHIPOTLE 2231 HOUSTON TX", "-13.80")])
        self.imp("i.csv", "Card 1111", [("10/08/2026", "CHIPOTLE 5000 PEARLAND TX", "-13.80")])
        self.assertEqual(self.dups()["total"], 0)

    def test_mark_as_separate_and_delete(self):
        self.imp("1.csv", "Card", [("09/03/2026", "UBER TRIP", "-12.00"), ("09/08/2026", "LYFT RIDE", "-9.00")])
        self.imp("2.csv", "Card", [("09/04/2026", "UBER TRIP HELP.UBER.COM", "-12.00"), ("09/09/2026", "LYFT  RIDE", "-9.00")])
        groups = self.dups()["groups"]
        self.assertEqual(len(groups), 2)
        uber = next(g for g in groups if g["amount_cents"] == -1200)
        lyft = next(g for g in groups if g["amount_cents"] == -900)
        self.client.post("/api/duplicates/dismiss", json={"ids": uber["ids"]})
        extra = [i for i in lyft["ids"] if i != lyft["keep_id"]]
        self.client.post("/api/transactions/delete", json={"ids": extra})
        self.assertEqual(self.dups()["total"], 0)
        self.assertEqual(self.client.get("/api/transactions").json["total"], 3)


if __name__ == "__main__":
    unittest.main()

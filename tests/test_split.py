import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from budget.app import create_app  # noqa: E402

ROWS = [("09/01/2026", "CLEAR LAKE HUNAN RESTAURA", "-42.00"),
        ("09/03/2026", "CLEAR LAKE REG MED CAFE", "-3.28"),
        ("09/04/2026", "CLEAR LAKE REG MED CAFE", "-5.90"),
        ("09/05/2026", "CLEAR LAKE FOOD MART", "-26.82"),
        ("09/06/2026", "I3P*CLEAR LAKE WATER", "-32.86"),
        ("09/07/2026", "TACODELI WASHINGTON A", "-10.00"),
        ("09/08/2026", "TACODELI WASHINGTON A HOUSTON TX", "-12.00")]


class TestSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()
        self.imp("a.csv", ROWS)
        self.cats = {c["name"]: c["id"] for c in self.client.get("/api/meta").json["categories"]}

    def tearDown(self):
        self.tmp.cleanup()

    def imp(self, name, rows):
        body = "Date,Description,Amount\n" + "".join(f"{d},{x},{a}\n" for d, x, a in rows)
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), name)},
                             content_type="multipart/form-data").json
        p["account"] = "Card"
        self.client.post("/api/import/commit", json=p)

    def merchant(self, name):
        return next((m for m in self.client.get("/api/merchants").json["items"] if m["name"] == name), None)

    def tx(self, desc):
        return next(t for t in self.client.get("/api/transactions").json["items"] if t["description"] == desc)

    def test_groups_and_split_and_future_imports(self):
        cl = self.merchant("CLEAR LAKE")
        self.assertEqual(cl["tx_count"], 5)
        self.assertGreater(cl["n_variants"], 1)
        # set a (wrong for most of them) category on the lumped merchant
        self.client.patch(f"/api/merchants/{cl['id']}", json={"category_id": self.cats["Dining"]})
        groups = self.client.get(f"/api/merchants/{cl['id']}/split").json["groups"]
        self.assertEqual({g["key"] for g in groups},
                         {"CLEAR LAKE REG MED", "CLEAR LAKE HUNAN RESTAURA", "CLEAR LAKE FOOD MART", "CLEAR LAKE WATER"})
        names = {"CLEAR LAKE WATER": "Clear Lake Water", "CLEAR LAKE FOOD MART": "Clear Lake Food Mart"}
        out = [{"ids": g["ids"], "match": g["key"], "name": names.get(g["key"], "CLEAR LAKE")} for g in groups]
        r = self.client.post(f"/api/merchants/{cl['id']}/split", json={"groups": out}).json
        self.assertEqual(len(r["merchants"]), 2)
        self.assertEqual(self.merchant("CLEAR LAKE")["tx_count"], 3)
        water = self.merchant("CLEAR LAKE WATER")
        self.assertEqual(water["tx_count"], 1)
        # moved ones no longer get the old merchant's category; the rest keep it
        self.assertNotEqual(self.tx("I3P*CLEAR LAKE WATER")["category_id"], self.cats["Dining"])
        self.assertEqual(self.tx("CLEAR LAKE HUNAN RESTAURA")["category_id"], self.cats["Dining"])
        # future imports go to the new merchant, and follow its category
        self.client.patch(f"/api/merchants/{water['id']}", json={"category_id": self.cats["Bills & Utilities"]})
        self.imp("b.csv", [("10/06/2026", "WITHDRAWAL ACH CLEAR LAKE WATER TYPE: X CO: CLEAR LAKE WATER NAME: ME", "-40.00"),
                           ("10/07/2026", "CLEAR LAKE HUNAN RESTAURA", "-30.00")])
        new = self.tx("WITHDRAWAL ACH CLEAR LAKE WATER TYPE: X CO: CLEAR LAKE WATER NAME: ME")
        self.assertEqual((new["merchant"], new["category_id"]), ("CLEAR LAKE WATER", self.cats["Bills & Utilities"]))
        self.assertEqual(self.merchant("CLEAR LAKE")["tx_count"], 4)

    def test_names_are_always_capitals(self):
        t = self.merchant("TACODELI WASHINGTON")
        self.client.patch(f"/api/merchants/{t['id']}", json={"name": "  Tacodeli   Washington Ave "})
        self.assertIsNotNone(self.merchant("TACODELI WASHINGTON AVE"))

    def test_city_suffixes_stay_together(self):
        t = self.merchant("TACODELI WASHINGTON")
        groups = self.client.get(f"/api/merchants/{t['id']}/split").json["groups"]
        self.assertEqual([(g["key"], g["count"]) for g in groups], [("TACODELI WASHINGTON A", 2)])

    def test_split_into_existing_merchant_and_empty_one_is_removed(self):
        cl = self.merchant("CLEAR LAKE")
        groups = self.client.get(f"/api/merchants/{cl['id']}/split").json["groups"]
        out = [{"ids": g["ids"], "match": g["key"], "name": "TACODELI WASHINGTON" if g["key"] == "CLEAR LAKE FOOD MART"
                else g["key"]} for g in groups]
        self.client.post(f"/api/merchants/{cl['id']}/split", json={"groups": out})
        self.assertIsNone(self.merchant("CLEAR LAKE"))                       # nothing left: removed
        self.assertEqual(self.merchant("TACODELI WASHINGTON")["tx_count"], 3)
        self.assertEqual(self.merchant("CLEAR LAKE REG MED")["tx_count"], 2)


if __name__ == "__main__":
    unittest.main()

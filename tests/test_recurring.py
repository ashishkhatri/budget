import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from budget.app import create_app  # noqa: E402

TODAY = dt.date.today()


def ago(days):
    return (TODAY - dt.timedelta(days=days)).strftime("%m/%d/%Y")


class TestRecurring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()
        rows = []
        for i in range(6):                                     # monthly subscription, same price
            rows.append((ago(10 + 30 * i), "NETFLIX.COM 866-579-7172 CA", "-15.49"))
        for i in range(8):                                     # weekly class
            rows.append((ago(3 + 7 * i), "YOGA STUDIO HOUSTON", "-20.00"))
        for i, amt in enumerate(["-88.10", "-131.40", "-104.75", "-97.20"]):   # utility, amount varies
            rows.append((ago(5 + 30 * i), "CITY OF SPRINGFIELD WATER", amt))
        for i, amt in enumerate(["-52.31", "-18.40", "-140.05", "-33.99", "-75.12", "-12.00", "-61.80", "-9.75"]):
            rows.append((ago(2 + 7 * i), "KROGER #123 HOUSTON", amt))      # weekly groceries: not recurring
        for i in range(3):                                     # a subscription hidden among Amazon shopping
            rows.append((ago(15 + 30 * i), "AMAZON MKTPL*AB12 SEATTLE", "-14.99"))
        for d, amt in [(4, "-63.20"), (40, "-9.99"), (51, "-120.00"), (77, "-31.45")]:
            rows.append((ago(d), "AMAZON MKTPL*ZZ99 SEATTLE", amt))
        for i in range(5):                                     # stopped: last charge 5 months ago
            rows.append((ago(150 + 30 * i), "OLDGYM FITNESS", "-29.99"))
        for i in range(4):                                     # card autopay is a transfer, not a charge
            rows.append((ago(8 + 30 * i), "PAYMENT TO CHASE CARD ENDING IN 1234", "-500.00"))
        body = "Date,Description,Amount\n" + "".join(f"{d},{n},{a}\n" for d, n, a in rows)
        import io
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), "test.csv")},
                             content_type="multipart/form-data").json
        p["account"] = "Checking"
        self.client.post("/api/import/commit", json=p)

    def tearDown(self):
        self.tmp.cleanup()

    def found(self):
        return {(r["merchant"], r["frequency"]): r for r in self.client.get("/api/recurring").json["items"]}

    def test_detects_frequencies_and_ignores_shopping(self):
        f = self.found()
        names = {m for m, _ in f}
        self.assertIn(("NETFLIX.COM", "monthly"), f)
        self.assertIn(("YOGA STUDIO", "weekly"), f)
        water = next(r for (m, q), r in f.items() if m.startswith("CITY OF") and q == "monthly")
        self.assertTrue(water["varies"])
        self.assertNotIn("KROGER", names)
        self.assertFalse(any("CHASE" in m or "PAYMENT" in m for m in names))
        amazon = [r for (m, _), r in f.items() if m.startswith("AMAZON")]
        self.assertEqual([(r["amount_cents"], r["frequency"]) for r in amazon], [(1499, "monthly")])
        self.assertEqual(f[("NETFLIX.COM", "monthly")]["amount_cents"], 1549)

    def test_stopped_and_hidden(self):
        f = self.found()
        gym = f[("OLDGYM FITNESS", "monthly")]
        self.assertFalse(gym["active"])
        self.assertTrue(f[("NETFLIX.COM", "monthly")]["active"])
        before = self.client.get("/api/recurring").json
        key = f[("NETFLIX.COM", "monthly")]["key"]
        self.client.put("/api/recurring/dismissed", json={"key": key, "dismissed": True})
        after = self.client.get("/api/recurring").json
        self.assertEqual(after["active_count"], before["active_count"] - 1)
        self.assertEqual(after["monthly_cents"], before["monthly_cents"] - 1549)
        self.assertTrue(self.found()[("NETFLIX.COM", "monthly")]["dismissed"])
        self.client.put("/api/recurring/dismissed", json={"key": key, "dismissed": False})
        self.assertFalse(self.found()[("NETFLIX.COM", "monthly")]["dismissed"])


class _ImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def imp(self, rows):
        import io
        body = "Date,Description,Amount\n" + "".join(f"{d},{n},{a}\n" for d, n, a in rows)
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), "y.csv")},
                             content_type="multipart/form-data").json
        p["account"] = "Checking"
        self.client.post("/api/import/commit", json=p)

    def found(self):
        return {r["merchant"]: r for r in self.client.get("/api/recurring").json["items"]}


class TestYearly(_ImportCase):
    def test_property_tax_on_different_days_and_amounts(self):
        self.imp([("02/20/2024", "BRAZORIA COUNTY TAX", "-925.05"), ("02/27/2024", "BRAZORIA COUNTY TAX", "-9310.45"),
                  ("02/04/2025", "BRAZORIA COUNTY TAX", "-7926.78"), ("02/03/2026", "BRAZORIA COUNTY TAX", "-6787.16"),
                  # chance: a restaurant twice, about a year apart, different bills
                  ("03/01/2025", "FANCY STEAKHOUSE", "-120.00"), ("03/04/2026", "FANCY STEAKHOUSE", "-95.00"),
                  # chance: one price among many visits
                  *[(d, "STARBUCKS STORE", a) for d, a in [
                      ("01/15/2025", "-6.25"), ("01/18/2025", "-4.10"), ("02/02/2025", "-7.80"), ("02/05/2025", "-5.55"),
                      ("03/30/2025", "-4.95"), ("06/11/2025", "-8.40"), ("06/12/2025", "-3.75"), ("09/09/2025", "-5.15"),
                      ("01/15/2026", "-6.25"), ("01/20/2026", "-9.30")]]])
        f = self.found()
        tax = f["BRAZORIA COUNTY"]
        self.assertEqual((tax["frequency"], tax["amount_cents"]), ("yearly", 792678))
        self.assertTrue(tax["next_date"].startswith("2027-02"))
        self.assertNotIn("FANCY STEAKHOUSE", f)
        self.assertNotIn("STARBUCKS STORE", f)

    def test_marked_by_hand(self):
        # two cars' registrations, a year apart each but on no neat schedule
        self.imp([("05/19/2024", "BRAZORIA VEHREG", "-71.75"), ("02/23/2025", "BRAZORIA VEHREG", "-73.75"),
                  ("03/19/2025", "BRAZORIA VEHREG", "-73.75"), ("02/10/2026", "BRAZORIA VEHREG", "-74.75"),
                  ("04/26/2026", "BRAZORIA VEHREG", "-74.75")])
        self.assertNotIn("BRAZORIA VEHREG", self.found())
        mid = next(m["id"] for m in self.client.get("/api/merchants").json["items"] if m["name"] == "BRAZORIA VEHREG")
        self.client.post("/api/recurring/manual", json={"merchant_id": mid, "frequency": "yearly"})
        r = self.found()["BRAZORIA VEHREG"]
        self.assertEqual((r["manual"], r["amount_cents"], r["parts"], r["frequency"]), (True, 14950, 2, "yearly"))
        self.assertEqual(r["next_date"], "2027-02-10")
        self.client.post("/api/recurring/manual", json={"merchant_id": mid, "frequency": "yearly", "remove": True})
        self.assertNotIn("BRAZORIA VEHREG", self.found())


class TestChangingAmounts(_ImportCase):
    def test_price_increase_and_small_weekly_wobble(self):
        rows = []
        # a subscription at a store you also shop at, $14.99 then $17.99 after a price rise
        for i, d in enumerate(["01/05/2026", "02/05/2026", "03/05/2026", "04/05/2026", "05/05/2026", "06/05/2026"]):
            rows.append((d, "AMAZON MKTPL*SUB", "-14.99" if i < 3 else "-17.99"))
        rows += [("01/20/2026", "AMAZON MKTPL*X1", "-63.20"), ("03/11/2026", "AMAZON MKTPL*X2", "-9.99"),
                 ("04/22/2026", "AMAZON MKTPL*X3", "-120.00")]
        # a weekly lesson that's $40, sometimes $45 with materials
        for i in range(6):
            rows.append((f"0{1 + i // 4}/{1 + 7 * (i % 4):02d}/2026", "PIANO LESSONS", "-45.00" if i % 3 == 0 else "-40.00"))
        self.imp(rows)
        f = {r["merchant"]: r for r in self.client.get("/api/recurring").json["items"]}
        sub = f["AMAZON MKTPL"]
        self.assertEqual((sub["frequency"], sub["count"], sub["amount_cents"]), ("monthly", 6, 1799))
        self.assertEqual(f["PIANO LESSONS"]["frequency"], "weekly")


class TestFrequencies(unittest.TestCase):
    def fit(self, step, n, amt=-1000, jitter=(0,)):
        from budget.recurring import _fit
        start = dt.date(2025, 1, 3)
        txs = [{"date": (start + dt.timedelta(days=round(step * i) + jitter[i % len(jitter)])).isoformat(),
                "amount_cents": amt} for i in range(n)]
        f = _fit(txs)
        return f and f["frequency"]

    def test_each_frequency(self):
        self.assertEqual(self.fit(7, 5), "weekly")
        self.assertEqual(self.fit(14, 4, jitter=(0, 1, -1)), "biweekly")
        self.assertEqual(self.fit(30.44, 4, jitter=(0, 2, -2)), "monthly")
        self.assertEqual(self.fit(60.88, 3), "bimonthly")
        self.assertEqual(self.fit(91.31, 3), "quarterly")
        self.assertEqual(self.fit(121.75, 3, jitter=(0, 4, -3)), "every4months")
        self.assertEqual(self.fit(182.63, 3, jitter=(0, -6, 5)), "semiannual")
        self.assertEqual(self.fit(182.63, 4, amt=-5000, jitter=(0, 9)), "semiannual")
        self.assertEqual(self.fit(365.25, 2), "yearly")

    def test_not_enough_or_irregular(self):
        self.assertIsNone(self.fit(30.44, 2))                      # two monthly charges isn't a pattern yet
        self.assertIsNone(self.fit(7, 6, jitter=(0, 9, 3, 15)))    # irregular visits

    def test_skipped_month_is_tolerated(self):
        from budget.recurring import _fit
        dates = ["2026-01-05", "2026-02-05", "2026-03-05", "2026-05-05", "2026-06-05"]
        self.assertEqual(_fit([{"date": d, "amount_cents": -999} for d in dates])["frequency"], "monthly")


if __name__ == "__main__":
    unittest.main()

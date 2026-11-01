"""Adding transactions by hand and from a receipt photo."""
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


class FakeVision(BaseHTTPRequestHandler):
    answer = {}
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"data": [{"id": "qwen2.5-vl-7b"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeVision.requests.append(req)
        content = json.dumps(FakeVision.answer)
        self._send({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestManual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVision)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app(os.path.join(self.tmp.name, "t.sqlite")).test_client()
        self.client.put("/api/ai/settings", json={"ai_base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                                                  "ai_model": "text-model", "ai_vision_model": "qwen2.5-vl-7b"})
        self.cats = {c["name"]: c["id"] for c in self.client.get("/api/meta").json["categories"]}
        FakeVision.requests = []

    def tearDown(self):
        self.tmp.cleanup()

    def txs(self):
        return self.client.get("/api/transactions").json["items"]

    def test_add_by_hand_goes_to_cash_and_is_categorized(self):
        r = self.client.post("/api/transactions", json={"date": "2026-10-30", "description": "Farmers market",
                                                       "amount_cents": -2400, "note": "eggs, honey"})
        self.assertEqual(r.status_code, 200, r.json)
        t = self.txs()[0]
        self.assertEqual((t["account"], t["amount_cents"], t["note"], t["merchant"]), ("Cash", -2400, "eggs, honey", "FARMERS MARKET"))
        # with a category you pick, into an existing account
        aid = next(a["id"] for a in self.client.get("/api/meta").json["accounts"] if a["name"] == "Cash")
        self.client.post("/api/transactions", json={"date": "2026-10-31", "description": "Haircut", "amount_cents": -3000,
                                                   "category_id": self.cats["Personal Care"], "account_id": aid})
        hc = next(t for t in self.txs() if t["description"] == "Haircut")
        self.assertEqual((hc["category_id"], hc["category_source"]), (self.cats["Personal Care"], "manual"))
        # bad input is refused clearly
        self.assertEqual(self.client.post("/api/transactions", json={"date": "x", "description": "a", "amount_cents": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/transactions", json={"date": "2026-10-30", "description": "a", "amount_cents": 0}).status_code, 400)
        dup = self.client.post("/api/transactions", json={"date": "2026-10-30", "description": "Farmers market", "amount_cents": -2400})
        self.assertEqual(dup.status_code, 409)

    def test_receipt_is_read_with_the_image_model_and_matched(self):
        body = "Date,Description,Amount\n10/06/2026,CHIPOTLE 2231 HOUSTON TX,-23.10\n"
        p = self.client.post("/api/import/preview", data={"file": (io.BytesIO(body.encode()), "s.csv")},
                             content_type="multipart/form-data").json
        p["account"] = "Chase Credit Card 9743"
        self.client.post("/api/import/commit", json=p)
        FakeVision.answer = {"merchant": "Chipotle", "date": "2026-10-06", "total": "$23.10", "category": "Dining",
                             "items": ["Burrito bowl", "Chips & guac"]}
        r = self.client.post("/api/receipt", data={"image": (io.BytesIO(b"\xff\xd8fakejpeg"), "r.jpg")},
                             content_type="multipart/form-data").json
        self.assertEqual((r["merchant"], r["date"], r["total_cents"], r["category_id"]),
                         ("Chipotle", "2026-10-06", 2310, self.cats["Dining"]))
        self.assertEqual(r["items"], ["Burrito bowl", "Chips & guac"])
        self.assertEqual([m["description"] for m in r["matches"]], ["CHIPOTLE 2231 HOUSTON TX"])
        req = FakeVision.requests[-1]
        self.assertEqual(req["model"], "qwen2.5-vl-7b")                           # the image model, not the text one
        self.assertEqual(req["messages"][1]["content"][1]["type"], "image_url")

    def photo(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (3000, 4000), "white")
        d = ImageDraw.Draw(im)
        for y in range(0, 4000, 60):
            d.text((100, y), f"ITEM {y} ..... 3.99", fill=(20, 20, 20))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=95)
        return buf.getvalue()

    def test_receipt_photo_is_kept_small_and_attached(self):
        from PIL import Image
        FakeVision.answer = {"merchant": "Farmers market", "date": "2026-10-30", "total": 24, "category": "Groceries", "items": []}
        big = self.photo()
        r = self.client.post("/api/receipt", data={"image": (io.BytesIO(big), "r.jpg")}, content_type="multipart/form-data").json
        self.assertTrue(r["receipt_token"])
        add = self.client.post("/api/transactions", json={"date": r["date"], "description": r["merchant"], "amount_cents": -2400,
                                                         "receipt_token": r["receipt_token"]}).json
        t = next(t for t in self.txs() if t["id"] == add["id"])
        self.assertTrue(t["receipt"].startswith(f"{add['id']}-"))
        img = self.client.get(f"/api/receipts/{t['receipt']}")
        self.assertEqual(img.status_code, 200)
        self.assertLess(len(img.data), len(big) / 5)
        pic = Image.open(io.BytesIO(img.data))
        self.assertEqual((max(pic.size), pic.mode), (1400, "L"))
        img.close()
        # attach a photo to a transaction that's already there, then remove it
        other = self.client.post("/api/transactions", json={"date": "2026-10-29", "description": "Bakery", "amount_cents": -800}).json["id"]
        self.client.post(f"/api/transactions/{other}/receipt", data={"image": (io.BytesIO(big), "b.jpg")},
                         content_type="multipart/form-data")
        name = next(t for t in self.txs() if t["id"] == other)["receipt"]
        self.assertEqual(self.client.get(f"/api/receipts/{name}").status_code, 200)
        self.client.delete(f"/api/transactions/{other}/receipt")
        self.assertIsNone(next(t for t in self.txs() if t["id"] == other)["receipt"])
        self.assertEqual(self.client.get(f"/api/receipts/{name}").status_code, 404)        # file cleaned up
        # deleting the transaction removes its photo at the next start
        from budget.app import create_app as again
        self.client.post("/api/transactions/delete", json={"ids": [add["id"]]})
        again(os.path.join(self.tmp.name, "t.sqlite"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "receipts", t["receipt"])))
        self.assertEqual(self.client.get("/api/receipts/..%2Ft.sqlite").status_code, 404)

    def test_failed_reading_still_keeps_the_photo(self):
        FakeVision.answer = ["nope"]
        r = self.client.post("/api/receipt", data={"image": (io.BytesIO(self.photo()), "r.jpg")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 502)
        self.assertTrue(r.json["receipt_token"])

    def test_unreadable_answer(self):
        FakeVision.answer = ["not", "an", "object"]
        r = self.client.post("/api/receipt", data={"image": (io.BytesIO(b"x"), "r.jpg")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()

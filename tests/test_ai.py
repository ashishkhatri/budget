"""Tests for the local-AI categorizer against a fake OpenAI-compatible (LM Studio-like) server."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget import ai  # noqa: E402
from budget import db as dbm  # noqa: E402
from budget import pipeline  # noqa: E402

ANSWERS = {"PEARLAND HALAL": ("Groceries", 0.93), "ARMK DP": ("Dining", 0.71),
           "WINDSCRIBE TORONTO": ("Subscriptions", 0.88), "ZZQX": ("Shopping", 0.2),
           "BIG PAYCHECK": ("Income", 0.99), "GAYLORD TEXAN": ("Travel", 0.9)}


class FakeLMStudio(BaseHTTPRequestHandler):
    structured = True       # set False to emulate a server that rejects response_format
    think = False           # wrap reply in <think> like reasoning models
    reasoning = None        # None | "length" (thinks until cut off, like qwen3.8) | "inside" (answer only in reasoning)
    requests = []
    slow = 0                # seconds between streamed chunks
    completions_ok = True   # False = server without /v1/completions
    disconnected = threading.Event()   # set when the app hangs up mid-stream

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": "text-embedding-nomic"}, {"id": "qwen3-14b"}]})
        else:
            self._send(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        req = json.loads(self.rfile.read(n))
        FakeLMStudio.requests.append({**req, "path": self.path})
        if self.path == "/v1/completions":
            if not FakeLMStudio.completions_ok:
                return self._send(404, {"error": "Unexpected endpoint"})
            user = req["prompt"].split("<|im_start|>user\n", 1)[1].split("<|im_end|>", 1)[0]
            assert req["prompt"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
            return self._stream(json.dumps({"results": self._answer(user)}), field="text")
        if "response_format" in req and not FakeLMStudio.structured:
            return self._send(400, {"error": "response_format json_schema is not supported"})
        if FakeLMStudio.reasoning == "ramble":
            return self._ramble()
        results = self._answer(req["messages"][1]["content"])
        content = json.dumps({"results": results})
        if FakeLMStudio.think:
            content = "<think>hmm, let me see</think>\n```json\n" + content + "\n```"
        if FakeLMStudio.reasoning == "length":
            return self._send(200, {"choices": [{"finish_reason": "length", "message": {
                "role": "assistant", "content": "", "reasoning_content": "We need answer user's request... Micro Center"}}]})
        if FakeLMStudio.reasoning == "inside":
            return self._send(200, {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "", "reasoning_content": "Let me think. Final:\n" + content}}]})
        if req.get("stream"):
            return self._stream(content)
        self._send(200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})

    @staticmethod
    def _answer(user_text):
        items = json.loads(user_text.split("\n", 1)[1].replace("/no_think", "").strip())
        out = []
        for it in items:
            cat, conf = ANSWERS.get(ai.group_key(it["description"]), ("Uncategorized", 0.1))
            out.append({"id": it["id"], "category": cat, "confidence": conf})
        return out

    def _ramble(self):
        """A reasoning model that never stops thinking."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(100000):
                chunk = {"choices": [{"delta": {"reasoning_content": "Hmm, maybe Dining. Or Shopping? "}, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
        except OSError:
            FakeLMStudio.disconnected.set()

    def _stream(self, content, field="content"):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        pieces = [content[i:i + 8] for i in range(0, len(content), 8)]
        try:
            for i, p in enumerate(pieces):
                time.sleep(FakeLMStudio.slow)
                fin = "stop" if i == len(pieces) - 1 else None
                ch = {"delta": {"content": p}} if field == "content" else {"text": p}
                self.wfile.write(b"data: " + json.dumps({"choices": [{**ch, "finish_reason": fin}]}).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            FakeLMStudio.disconnected.set()


class TestAI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeLMStudio)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeLMStudio.structured, FakeLMStudio.think, FakeLMStudio.requests = True, False, []
        FakeLMStudio.reasoning = None
        FakeLMStudio.slow = 0
        FakeLMStudio.completions_ok = True
        ai._no_think_failed.clear()
        FakeLMStudio.disconnected.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sqlite")
        dbm.init_db(self.path)
        self.con = dbm.connect(self.path)
        with self.con:
            ai.save_settings(self.con, {"ai_enabled": True, "ai_base_url": self.base, "ai_min_confidence": "0.6",
                                        "ai_model": "plain-model"})
            aid = pipeline.get_or_create_account(self.con, "Card")
            rows = [("2026-10-01", "PEARLAND HALAL MEAT 123", -1200), ("2026-10-03", "PEARLAND HALAL MEAT 456", -900),
                    ("2026-10-02", "ARMK DP CLUB 77", -1700), ("2026-10-04", "WINDSCRIBE TORONTO ON", -400),
                    ("2026-10-05", "ZZQX 9", -500), ("2026-10-06", "BIG PAYCHECK", -100),
                    ("2026-10-07", "MYSTERY SHOP 1", -2300), ("2026-10-08", "MYSTERY SHOP 2", -1100)]
            pipeline.ingest(self.con, aid, [dict(date=d, description=x, amount_cents=a) for d, x, a in rows])
            ids = pipeline.category_ids(self.con)
            # the user hand-categorized one MYSTERY SHOP; the other should be "learned", not sent to the model
            self.con.execute("UPDATE transactions SET category_id = ?, category_source = 'manual' "
                             "WHERE description = 'MYSTERY SHOP 1'", (ids["travel"],))
        self.ids = ids

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def cat_of(self, desc):
        r = self.con.execute("SELECT c.name, t.category_source FROM transactions t JOIN categories c "
                             "ON c.id = t.category_id WHERE description = ?", (desc,)).fetchone()
        return tuple(r)

    def preview_rows(self):
        return [{"description": "PEARLAND HALAL MEAT 9", "amount_cents": -1500},
                {"description": "ARMK DP CLUB 1", "amount_cents": -800}]

    def test_streamed_suggest_matches_normal(self):
        with self.con:      # nothing in history, so both rows go to the model
            self.con.execute("DELETE FROM transactions")
        plain = ai.suggest(self.con, self.preview_rows())
        streamed = ai.suggest(self.con, self.preview_rows(), stop=threading.Event())
        self.assertEqual(plain, streamed)
        self.assertEqual(streamed[0]["category_source"], "ai")
        self.assertTrue(FakeLMStudio.requests[-1].get("stream"))

    def test_cancel_stops_the_model_mid_answer(self):
        with self.con:
            self.con.execute("DELETE FROM transactions")
        FakeLMStudio.slow = 0.15            # ~3 s for a full answer
        stop = ai.cancel_event("tok-1")
        threading.Timer(0.4, ai.cancel, args=("tok-1",)).start()
        t0 = time.time()
        with self.assertRaises(ai.Cancelled):
            ai.suggest(self.con, self.preview_rows(), stop=stop)
        self.assertLess(time.time() - t0, 1.2)
        self.assertTrue(FakeLMStudio.disconnected.wait(2))      # LM Studio saw the hang-up
        # a cancel that arrives before the request even starts also works
        ai.cancel("tok-2")
        with self.assertRaises(ai.Cancelled):
            ai.suggest(self.con, self.preview_rows(), stop=ai.cancel_event("tok-2"))

    def test_models_skip_embeddings(self):
        self.assertEqual(ai.list_models(self.base), ["qwen3-14b"])

    def test_full_run(self):
        out = ai.run(self.path)
        self.assertEqual(self.cat_of("PEARLAND HALAL MEAT 456"), ("Groceries", "ai"))
        self.assertEqual(self.cat_of("ARMK DP CLUB 77"), ("Dining", "ai"))
        self.assertEqual(self.cat_of("MYSTERY SHOP 2"), ("Travel", "learned"))
        self.assertEqual(self.cat_of("ZZQX 9"), ("Uncategorized", "none"))          # low confidence
        self.assertEqual(self.cat_of("BIG PAYCHECK"), ("Uncategorized", "none"))    # "income" but money out
        self.assertEqual(self.cat_of("MYSTERY SHOP 1"), ("Travel", "manual"))       # untouched
        self.assertEqual((out["applied"], out["learned"], out["unsure"]), (4, 1, 2))
        req = FakeLMStudio.requests[0]
        self.assertEqual(req["model"], "plain-model")
        self.assertIn("MYSTERY SHOP -> Travel", req["messages"][0]["content"])      # your choices used as examples
        sent = req["messages"][1]["content"]
        self.assertNotIn("MYSTERY", sent)                                            # learned ones not sent
        self.assertEqual(sent.count("PEARLAND"), 2)                                  # grouped: one item, 2 descriptions

    def test_fallback_without_structured_output_and_think_tags(self):
        FakeLMStudio.structured, FakeLMStudio.think = False, True
        out = ai.run(self.path)
        self.assertEqual(out["applied"], 4)
        self.assertNotIn("response_format", FakeLMStudio.requests[-1])

    def test_reasoning_model_runs_out_of_room_gives_clear_error(self):
        FakeLMStudio.reasoning = "length"
        with self.assertRaises(ai.AIError) as e:
            ai.run(self.path)
        self.assertIn("spent it all thinking", str(e.exception))

    def test_answer_found_inside_reasoning(self):
        FakeLMStudio.reasoning = "inside"
        self.assertEqual(ai.run(self.path)["applied"], 4)

    def use_model(self, name):
        with self.con:
            ai.save_settings(self.con, {"ai_model": name})

    def test_qwen_skips_thinking_via_completions(self):
        self.use_model("qwen/qwen3.8-27b")
        out = ai.run(self.path)
        self.assertEqual(out["applied"], 4)
        self.assertEqual({r["path"] for r in FakeLMStudio.requests}, {"/v1/completions"})
        self.assertLess(FakeLMStudio.requests[0]["max_tokens"], 2000)

    def test_qwen_falls_back_to_chat_when_no_completions_endpoint(self):
        self.use_model("qwen/qwen3.8-27b")
        FakeLMStudio.completions_ok = False
        self.assertEqual(ai.run(self.path)["applied"], 4)
        paths = [r["path"] for r in FakeLMStudio.requests]
        self.assertEqual(paths.count("/v1/completions"), 1)          # tried once, then remembered
        self.assertEqual(paths[-1], "/v1/chat/completions")

    def test_endless_thinking_is_stopped_with_advice(self):
        FakeLMStudio.reasoning = "ramble"
        t0 = time.time()
        with self.assertRaises(ai.AIError) as e:
            ai.run(self.path)
        self.assertLess(time.time() - t0, 5)
        self.assertIn("enable_thinking = false", str(e.exception))
        self.assertTrue(FakeLMStudio.disconnected.wait(2))

    def test_prompt_asks_for_no_thinking_and_explains_refunds(self):
        ai.run(self.path)
        req = FakeLMStudio.requests[0]
        self.assertTrue(req["messages"][1]["content"].endswith("/no_think"))
        self.assertIn("refund", req["messages"][0]["content"])
        self.assertGreaterEqual(req["max_tokens"], 16000)

    def test_ai_answer_is_saved_on_the_merchant_and_reused(self):
        ai.run(self.path)
        row = self.con.execute("""SELECT m.category_id, m.category_source FROM merchants m
                                  JOIN merchant_aliases a ON a.merchant_id = m.id WHERE a.key = 'PEARLAND HALAL'""").fetchone()
        self.assertEqual(tuple(row), (self.ids["groceries"], "ai"))
        asked = len(FakeLMStudio.requests)
        with self.con:
            aid = pipeline.get_or_create_account(self.con, "Card")
            pipeline.ingest(self.con, aid, [dict(date="2026-12-01", description="PEARLAND HALAL MEAT 777", amount_cents=-800)])
        self.assertEqual(self.cat_of("PEARLAND HALAL MEAT 777"), ("Groceries", "ai"))
        self.assertEqual(len(FakeLMStudio.requests), asked)      # no new question to the model

    def test_ai_never_overrides_a_merchant_category_you_set(self):
        mid = self.con.execute("SELECT merchant_id FROM merchant_aliases WHERE key = 'ARMK DP'").fetchone()[0]
        with self.con:
            pipeline.set_merchant_category(self.con, mid, self.ids["education"])
        ai.run(self.path)
        self.assertEqual(self.cat_of("ARMK DP CLUB 77"), ("Education", "merchant"))

    def test_recategorize_keeps_ai_choices(self):
        ai.run(self.path)
        with self.con:
            pipeline.recategorize(self.con)
        self.assertEqual(self.cat_of("ARMK DP CLUB 77"), ("Dining", "ai"))

    def test_unreachable_server_message(self):
        with self.con:
            ai.save_settings(self.con, {"ai_base_url": "http://127.0.0.1:1/v1"})
        with self.assertRaises(ai.AIError) as e:
            ai.run(self.path)
        self.assertIn("LM Studio", str(e.exception))

    def test_background_job_via_api(self):
        from budget.app import create_app
        import time
        client = create_app(self.path).test_client()
        self.assertEqual(client.post("/api/ai/categorize", json={}).status_code, 200)
        for _ in range(50):
            st = client.get("/api/ai/status").json
            if not st["running"]:
                break
            time.sleep(0.05)
        self.assertIsNone(st["error"])
        self.assertEqual(st["applied"], 4)
        txs = client.get("/api/transactions?source=ai").json
        self.assertEqual(txs["total"], 5)


    def test_preview_suggest_then_commit_keeps_ai_picks(self):
        from budget.app import create_app
        client = create_app(self.path).test_client()
        rows = [{"date": "2026-11-01", "description": "PEARLAND HALAL MEAT 999", "amount_cents": -1500},
                {"date": "2026-11-02", "description": "MYSTERY SHOP 3", "amount_cents": -700},
                {"date": "2026-11-03", "description": "ZZQX 10", "amount_cents": -100}]
        res = client.post("/api/ai/suggest", json={"rows": rows}).json["results"]
        self.assertEqual(res[0]["category_source"], "ai")
        self.assertEqual(res[1]["category_source"], "learned")
        self.assertIsNone(res[2])
        self.assertEqual(self.cat_of("ZZQX 9"), ("Uncategorized", "none"))       # suggest saved nothing
        for r, sg in zip(rows, res):
            if sg:
                r.update(sg)
        # flipping signs / renaming the account re-checks rows: AI picks must survive
        rc = client.post("/api/import/recheck", json={"account": "Card", "rows": [dict(r) for r in rows]}).json["rows"]
        self.assertEqual(rc[0]["category_source"], "ai")
        client.post("/api/import/commit", json={"account": "Card", "rows": rows, "filename": "x.csv"})
        self.assertEqual(self.cat_of("PEARLAND HALAL MEAT 999"), ("Groceries", "ai"))
        self.assertEqual(self.cat_of("MYSTERY SHOP 3"), ("Travel", "learned"))

    def test_suggest_reports_unreachable_server(self):
        from budget.app import create_app
        with self.con:
            ai.save_settings(self.con, {"ai_base_url": "http://127.0.0.1:1/v1"})
        r = create_app(self.path).test_client().post("/api/ai/suggest",
                                                    json={"rows": [{"description": "ZZQX", "amount_cents": -1}]})
        self.assertEqual(r.status_code, 502)
        self.assertIn("LM Studio", r.json["error"])


if __name__ == "__main__":
    unittest.main()

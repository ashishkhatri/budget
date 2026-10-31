"""Categorize leftover Uncategorized transactions with a local LLM.

Works with LM Studio (or any OpenAI-compatible server, e.g. Ollama's /v1).
Runs as a background job so the UI stays responsive:

  1. Collect transactions that are still Uncategorized (never ones you set by hand).
  2. Group them by merchant (description with numbers stripped).
  3. "Learned": if you've already categorized the same merchant elsewhere, reuse that.
  4. Ask the LLM about the rest in batches, with your own categorized merchants as examples.
     Answers below the confidence threshold are left Uncategorized.

AI-assigned rows get category_source = 'ai' (or 'learned'), so they can be reviewed,
and re-running the normal categorizer won't throw them away.
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from . import db as dbm
from .categorize import UNCATEGORIZED, merchant_key
from .parsing.common import norm

# Every request to the AI server is logged to data\ai.log (set up in __main__), to diagnose slow
# or failing answers.
log = logging.getLogger("budget.ai")

DEFAULTS = {
    "ai_enabled": "0",
    "ai_base_url": "http://127.0.0.1:1234/v1",
    "ai_model": "",
    "ai_auto": "1",               # suggest categories while reviewing an import
    "ai_min_confidence": "0.6",
}
BATCH_SIZE = 10
MAX_TOKENS = 16000   # hard cap; the thinking budget below normally stops a rambling model long before this


def think_budget(n_items: int) -> int:
    """How much "thinking" (in characters, ~4 per token) to allow before giving up on an answer.
    Picking a category needs little thought: about 1,000 tokens plus 150 per merchant."""
    return 4 * (1000 + 150 * n_items)


def no_think_capable(model: str) -> bool:
    """Qwen models use the ChatML format, where an empty <think></think> block switches thinking off."""
    m = model.lower()
    return "qwen" in m and "deepseek" not in m
KIND_HINT = {"income": "money coming in", "transfer": "moving money between your own accounts, card payments, investing",
             "expense": "spending"}


class AIError(Exception):
    pass


class ThoughtTooLong(AIError):
    """A reasoning model kept thinking past the budget without answering."""


class Cancelled(AIError):
    """The request was cancelled (e.g. you closed the import preview)."""


# Cancel tokens for in-flight preview requests. The browser sends a token with each request and
# calls /api/ai/cancel with it when you leave; the connection to LM Studio is then closed, which
# makes LM Studio stop generating.
_cancels: Dict[str, threading.Event] = {}
_cancels_lock = threading.Lock()


def cancel_event(token: str) -> threading.Event:
    with _cancels_lock:
        if len(_cancels) > 500:                       # forget old tokens
            for k in [k for k, e in _cancels.items() if e.is_set()][:400]:
                _cancels.pop(k, None)
        return _cancels.setdefault(token, threading.Event())


def cancel(token: str) -> None:
    cancel_event(token).set()


def release(token: str) -> None:
    with _cancels_lock:
        ev = _cancels.get(token)
        if ev is not None and not ev.is_set():
            _cancels.pop(token, None)


# ---------------- settings ----------------

def get_settings(con) -> Dict[str, str]:
    rows = dict(con.execute("SELECT key, value FROM settings WHERE key LIKE 'ai_%'").fetchall())
    return {**DEFAULTS, **{k: v for k, v in rows.items() if k in DEFAULTS}}


def save_settings(con, values: dict) -> None:
    for k in DEFAULTS:
        if k in values and values[k] is not None:
            v = values[k]
            if isinstance(v, bool):
                v = "1" if v else "0"
            con.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v).strip()))


def enabled(con) -> bool:
    return get_settings(con)["ai_enabled"] == "1"


# ---------------- HTTP ----------------

# Never send localhost traffic through a system proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url: str, payload: Optional[dict] = None, timeout: float = 10):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        raise AIError(f"The AI server returned an error ({e.code}): {body}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise AIError(f"Can't reach the AI server at {url}. Is LM Studio's local server running? ({reason})") from e
    except json.JSONDecodeError as e:
        raise AIError("The AI server sent back something that isn't JSON.") from e


def list_models(base_url: str) -> List[str]:
    data = _http(base_url.rstrip("/") + "/models", timeout=5)
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return [i for i in ids if "embed" not in i.lower()]


def _stream(url: str, payload: dict, timeout: float, stop: threading.Event, budget: int = 0) -> dict:
    """POST with stream=true and collect the answer, giving up as soon as ``stop`` is set.

    Cancelling closes the socket, which unblocks the read immediately (even while the model is
    still reading the prompt) and tells LM Studio to stop generating.
    """
    u = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=timeout)
    done = threading.Event()

    def watch():
        while not done.wait(0.2):
            if stop.is_set():
                try:
                    if conn.sock:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
                return
    threading.Thread(target=watch, daemon=True).start()
    content, reasoning, finish = [], [], None
    resp = sock = None
    t0 = time.time()
    first = None
    endpoint = u.path.rsplit("/", 1)[-1]
    size = len(payload.get("prompt") or json.dumps(payload.get("messages") or ""))
    log.info("-> %s model=%s prompt=%d chars max_tokens=%s budget=%s", endpoint, payload.get("model"), size,
             payload.get("max_tokens"), budget or "-")
    outcome = "ok"
    try:
        if stop.is_set():
            raise Cancelled("Cancelled.")
        path = u.path + (f"?{u.query}" if u.query else "")
        conn.request("POST", path, body=json.dumps({**payload, "stream": True}).encode("utf-8"),
                     headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio",
                              "Accept": "text/event-stream"})
        sock = conn.sock
        resp = conn.getresponse()
        if resp.status >= 400:
            body = resp.read().decode("utf-8", errors="replace")[:400]
            raise AIError(f"The AI server returned an error ({resp.status}): {body}")
        if "event-stream" not in (resp.getheader("Content-Type") or ""):
            return json.loads(resp.read().decode("utf-8"))            # server ignored stream=true
        for raw in resp:
            if first is None:
                first = time.time() - t0
            if stop.is_set():
                raise Cancelled("Cancelled.")
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                choice = json.loads(data)["choices"][0]
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or choice.get("text") or "")
            reasoning.append(delta.get("reasoning_content") or delta.get("reasoning") or "")
            finish = choice.get("finish_reason") or finish
            if budget:
                thought = sum(map(len, reasoning))
                text = "".join(content)
                if "<think>" in text and "</think>" not in text:   # thinking inside the answer text
                    thought += len(text)
                if thought > budget and "{" not in text.split("</think>")[-1]:
                    raise ThoughtTooLong(f"thought for ~{thought // 4} tokens without answering")
        if stop.is_set():
            raise Cancelled("Cancelled.")
    except (Cancelled, ThoughtTooLong) as e:
        outcome = type(e).__name__ + (f" ({e})" if isinstance(e, ThoughtTooLong) else "")
        raise
    except AIError as e:
        outcome = f"error: {e}"
        raise
    except (OSError, http.client.HTTPException) as e:
        outcome = f"connection error: {e!r}"
        if stop.is_set():
            raise Cancelled("Cancelled.") from e
        raise AIError(f"Can't reach the AI server at {url}. Is LM Studio's local server running? ({e})") from e
    except json.JSONDecodeError as e:
        raise AIError("The AI server sent back something that isn't JSON.") from e
    finally:
        text, thought = "".join(content), "".join(reasoning)
        log.info("<- %s %s in %.1fs (first data after %s) finish=%s answer=%d chars thinking=%d chars | answer: %s%s",
                 endpoint, outcome, time.time() - t0, f"{first:.1f}s" if first is not None else "never", finish,
                 len(text), len(thought), text[:300].replace("\n", " "),
                 f" | thinking ends: ...{thought[-300:]}".replace("\n", " ") if thought else "")
        done.set()
        if sock is not None:            # hang up properly, so the server stops generating right away
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if resp is not None:
            resp.close()
        conn.close()
    return {"choices": [{"finish_reason": finish, "message": {
        "content": "".join(content), "reasoning_content": "".join(reasoning)}}]}


def _chat(base_url: str, model: str, messages: list, schema: dict, timeout: float = 600,
          stop: Optional[threading.Event] = None, budget: int = 0) -> dict:
    """Returns {"content", "reasoning", "finish_reason"}. Streams, so it can be cancelled with ``stop``
    and abandoned once a reasoning model thinks past ``budget`` characters."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": MAX_TOKENS, "stream": False,
               # honored by llama.cpp / vLLM servers; harmless elsewhere
               "chat_template_kwargs": {"enable_thinking": False},
               "response_format": {"type": "json_schema",
                                   "json_schema": {"name": "categorization", "strict": True, "schema": schema}}}
    stop = stop or threading.Event()
    send = lambda: _stream(url, payload, timeout, stop, budget)   # noqa: E731
    try:
        res = send()
    except (Cancelled, ThoughtTooLong):
        raise
    except AIError as e:
        if "response_format" not in str(e) and "json_schema" not in str(e):
            raise
        payload.pop("response_format")          # server doesn't support structured output: ask nicely instead
        res = send()
    try:
        choice = res["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise AIError("Unexpected reply from the AI server.") from e
    return {"content": msg.get("content") or "",
            "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
            "finish_reason": choice.get("finish_reason")}


def _chat_no_think(base_url: str, model: str, messages: list, n_items: int, timeout: float = 600,
                   stop: Optional[threading.Event] = None) -> dict:
    """Ask a Qwen-style model with thinking switched off.

    LM Studio ignores the API switches for thinking, so this writes the ChatML prompt itself, ending
    with an empty <think></think> block (exactly what Qwen's own "thinking off" mode does), and sends
    it to the plain /completions endpoint. The model answers straight away."""
    prompt = "".join(f"<|im_start|>{m['role']}\n{m['content'].replace('/no_think', '').rstrip()}<|im_end|>\n"
                     for m in messages) + "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    payload = {"model": model, "prompt": prompt, "temperature": 0, "max_tokens": 300 + 80 * n_items,
               "stop": ["<|im_end|>", "<|im_start|>"]}
    res = _stream(base_url.rstrip("/") + "/completions", payload, timeout, stop or threading.Event(),
                  budget=think_budget(n_items))
    try:
        choice = res["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or choice.get("text") or ""
    except (KeyError, IndexError, TypeError) as e:
        raise AIError("Unexpected reply from the AI server.") from e
    return {"content": text, "reasoning": msg.get("reasoning_content") or "", "finish_reason": choice.get("finish_reason")}


# Models for which the no-thinking shortcut didn't work this session (fall back to normal chat).
_no_think_failed: set = set()


def ask(base: str, model: str, messages: list, schema: dict, n_items: int,
        stop: Optional[threading.Event] = None) -> dict:
    """Get the {"results": [...]} answer for one batch, as fast as the model allows."""
    if no_think_capable(model) and model not in _no_think_failed:
        try:
            return read_answer(_chat_no_think(base, model, messages, n_items, stop=stop))
        except Cancelled:
            raise
        except AIError as e:
            log.warning("no-thinking request failed for %s, using chat from now on: %s", model, e)
            _no_think_failed.add(model)            # e.g. the server has no /completions: use chat from now on
    try:
        return read_answer(_chat(base, model, messages, schema, stop=stop, budget=think_budget(n_items)))
    except ThoughtTooLong as e:
        raise AIError(
            f"The model {e} and was stopped. It's a reasoning model that thinks at length before every "
            "answer. In LM Studio open My Models → this model → Inference → Prompt Template and add "
            "{%- set enable_thinking = false %} as the first line, then reload it, "
            "or pick a model without thinking.") from e


def read_answer(reply: dict) -> dict:
    """Get the JSON answer out of a reply, coping with reasoning ("thinking") models."""
    for text in (reply["content"], reply["reasoning"]):
        if text and "results" in text:
            try:
                data = parse_json(text if text is reply["content"] else text[text.rfind('{"results"'):])
                if isinstance(data, dict) and isinstance(data.get("results"), list):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass
    if reply["finish_reason"] == "length":
        raise AIError("The model ran out of room before answering"
                      + (" because it spent it all thinking. " if reply["reasoning"] and not reply["content"] else ". ")
                      + "In LM Studio, turn off thinking/reasoning for this model, or pick a non-reasoning model.")
    if not reply["content"]:
        raise AIError("The model returned an empty answer. Try again, or pick a different model in LM Studio.")
    raise AIError("The model's answer wasn't in the expected JSON format. Try a different model.")


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


# ---------------- grouping ----------------

def group_key(description: str) -> str:
    """Merchant identity for grouping. Same as the merchant name used everywhere else."""
    return merchant_key(description)


def _money(c: int) -> str:
    return f"{'-' if c < 0 else '+'}${abs(c) / 100:,.2f}"


def build_messages(categories: List[dict], items: List[dict], examples: List[tuple]) -> list:
    cat_lines = "\n".join(f"- {c['name']} ({KIND_HINT[c['kind']]})" for c in categories)
    ex_lines = "\n".join(f"- {d} -> {c}" for d, c in examples) or "(none yet)"
    system = (
        "You categorize personal bank and credit card transactions for a US household budget.\n"
        "For each merchant, pick exactly ONE category from this list, using the name exactly as written:\n"
        f"{cat_lines}\n\n"
        "Guidance:\n"
        "- Negative amounts are money out. A positive amount at a store or restaurant is a refund: give it that\n"
        "  merchant's normal category (e.g. a Micro Center refund is Shopping). Income is only pay, salary,\n"
        "  interest, dividends, tax refunds and similar.\n"
        "- Payments to a credit card, transfers between the person's own accounts, and investing are Transfers.\n"
        "- Use the merchant name, typical amount and your knowledge of US businesses (restaurants, chains, tolls, etc.).\n"
        f"- If you genuinely can't tell, answer {UNCATEGORIZED} with low confidence rather than guessing.\n"
        "- confidence is 0 to 1: how sure you are.\n\n"
        "How this person has categorized other merchants (follow their style):\n"
        f"{ex_lines}\n\n"
        "Decide quickly from the name; don't deliberate at length. Unknown merchant -> Uncategorized, low confidence.\n"
        'Reply with JSON only: {"results": [{"id": <id>, "category": "<category name>", "confidence": <0-1>}]}'
    )
    user_items = [{"id": it["id"], "description": it["example"], "other_descriptions": it["others"],
                   "typical_amount": _money(it["typical_cents"]), "times_seen": it["count"]} for it in items]
    return [{"role": "system", "content": system},
            {"role": "user", "content": "Categorize these merchants:\n" + json.dumps(user_items, indent=1) + "\n/no_think"}]


def schema_for(names: List[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "category", "confidence"],
                "properties": {"id": {"type": "integer"}, "category": {"type": "string", "enum": names},
                               "confidence": {"type": "number"}}}}}}


# ---------------- the job ----------------

_lock = threading.Lock()
_state: dict = {"running": False}


def status() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw):
    with _lock:
        _state.update(kw)


def start(db_path: str, only_ids: Optional[List[int]] = None) -> bool:
    """Start a background run. Returns False if one is already running."""
    with _lock:
        if _state.get("running"):
            return False
        _state.clear()
        _state.update(running=True, phase="Starting", done=0, total=0, applied=0, learned=0, unsure=0,
                      error=None, started=time.time(), finished=None)
    t = threading.Thread(target=_run_safe, args=(db_path, only_ids), daemon=True)
    t.start()
    return True


def _run_safe(db_path, only_ids):
    try:
        run(db_path, only_ids, progress=_set)
    except Exception as e:  # report anything to the UI instead of dying silently
        _set(error=str(e) if isinstance(e, AIError) else f"{e.__class__.__name__}: {e}")
    finally:
        _set(running=False, finished=time.time(), phase="Done")


def suggest(con, rows: List[dict], progress=lambda **kw: None,
            stop: Optional[threading.Event] = None) -> List[Optional[dict]]:
    """Suggest categories for rows ({description, amount_cents}) without saving anything.

    Returns one entry per row: {category_id, category_source ('ai'|'learned'), confidence}
    or None when the AI wasn't sure. Used by the import preview and by the background job.
    """
    s = get_settings(con)
    base, min_conf = s["ai_base_url"], float(s["ai_min_confidence"] or 0.6)
    cats = [dict(r) for r in con.execute("SELECT id, name, kind FROM categories ORDER BY sort_order, name")]
    by_name = {c["name"].lower(): c for c in cats}
    uncat = by_name[UNCATEGORIZED.lower()]["id"]
    out: List[Optional[dict]] = [None] * len(rows)
    if not rows:
        return out

    groups: Dict[str, List[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[group_key(r["description"])].append(i)

    # 1. learned from your own history
    history: Dict[str, Counter] = defaultdict(Counter)
    for r in con.execute("""SELECT description, category_id FROM transactions
                            WHERE category_id != ? AND category_source IN ('manual','rule','ai','learned')""", (uncat,)):
        history[group_key(r[0])][r[1]] += 1
    pending = {}
    for key, idxs in groups.items():
        if key in history:
            cid = history[key].most_common(1)[0][0]
            for i in idxs:
                out[i] = {"category_id": cid, "category_source": "learned", "confidence": 1.0}
        else:
            pending[key] = idxs
    if not pending:
        return out

    # 2. ask the model
    model = s["ai_model"]
    if not model:
        models = list_models(base)
        if not models:
            raise AIError("LM Studio has no model loaded. Load one, or pick a model in Settings.")
        model = models[0]
    examples = [(r[0], r[1]) for r in con.execute("""
        SELECT t.merchant, c.name FROM transactions t JOIN categories c ON c.id = t.category_id
        WHERE t.category_source IN ('manual','rule') AND t.merchant IS NOT NULL
        GROUP BY t.merchant ORDER BY MAX(t.id) DESC LIMIT 40""")]
    schema = schema_for([c["name"] for c in cats])

    items = []
    for n, (key, idxs) in enumerate(pending.items(), start=1):
        descs = list(dict.fromkeys(rows[i]["description"] for i in idxs))
        amts = sorted(int(rows[i]["amount_cents"]) for i in idxs)
        items.append({"id": n, "idxs": idxs, "example": descs[0], "others": descs[1:4],
                      "typical_cents": amts[len(amts) // 2], "count": len(idxs)})
    progress(phase=f"Asking {model}", total=len(items), done=0)
    for b in range(0, len(items), BATCH_SIZE):
        if stop is not None and stop.is_set():
            raise Cancelled("Cancelled.")
        batch = items[b:b + BATCH_SIZE]
        results = ask(base, model, build_messages(cats, batch, examples), schema, len(batch), stop=stop)["results"]
        by_id = {it["id"]: it for it in batch}
        for res in results:
            it = by_id.get(res.get("id")) if isinstance(res, dict) else None
            if not it:
                continue
            cat = by_name.get(str(res.get("category", "")).strip().lower())
            try:
                conf = float(res.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            if (not cat or cat["id"] == uncat or conf < min_conf
                    or (cat["kind"] == "income" and it["typical_cents"] < 0)):
                continue
            for i in it["idxs"]:
                out[i] = {"category_id": cat["id"], "category_source": "ai", "confidence": round(conf, 2)}
        progress(done=min(len(items), b + BATCH_SIZE))
    return out


def run(db_path: str, only_ids: Optional[List[int]] = None, progress=lambda **kw: None) -> dict:
    """Categorize transactions already in the database that are still Uncategorized."""
    con = dbm.connect(db_path)
    try:
        uncat = con.execute("SELECT id FROM categories WHERE name = ?", (UNCATEGORIZED,)).fetchone()[0]
        rows = [dict(r) for r in con.execute(
            "SELECT id, description, amount_cents, merchant_id FROM transactions WHERE category_id = ? AND category_source != 'manual'",
            (uncat,))]
        if only_ids is not None:
            wanted = set(only_ids)
            rows = [r for r in rows if r["id"] in wanted]
        out = {"applied": 0, "learned": 0, "unsure": 0}
        if not rows:
            progress(phase="Nothing to categorize")
            return out
        progress(phase="Checking merchants you've already categorized")
        sugg = suggest(con, rows, progress)
        with con:
            for r, sg in zip(rows, sugg):
                if not sg:
                    out["unsure"] += 1
                    continue
                con.execute("UPDATE transactions SET category_id = ?, category_source = ? WHERE id = ? AND category_id = ?",
                            (sg["category_id"], sg["category_source"], r["id"], uncat))
                if sg["category_source"] == "ai" and r.get("merchant_id"):
                    # save the answer on the merchant, so this merchant is never asked about again
                    con.execute("UPDATE merchants SET category_id = ?, category_source = 'ai' "
                                "WHERE id = ? AND category_id IS NULL", (sg["category_id"], r["merchant_id"]))
                out["applied" if sg["category_source"] == "ai" else "learned"] += 1
        progress(**out)
        return out
    finally:
        con.close()

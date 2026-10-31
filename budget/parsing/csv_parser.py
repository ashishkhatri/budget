"""CSV statement parser with auto-detection for common US banks.

Detected layouts: Chase checking/savings, Chase credit card, Bank of America
(checking and credit card), Wells Fargo (headerless), Capital One, American
Express, Discover, Citi. Anything else falls back to guessing the Date /
Description / Amount (or Debit / Credit) columns by name.
"""
from __future__ import annotations

import csv
import io
import re
from typing import List, Optional

from .common import (Candidate, ParseError, ParseResult, guess_account_from_filename, last4_from_filename,
                     parse_amount, parse_date, with_last4)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _rows(text: str) -> List[List[str]]:
    return [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]


# (test, format label, account name, institution, account type, flip signs)
def _detect(h: List[str]):
    has = h.__contains__
    if has("details") and has("posting date"):
        return "Chase checking/savings", "Chase Checking", "Chase", "checking", False
    if has("transaction date") and has("post date") and has("category") and has("type"):
        return "Chase credit card", "Chase Credit Card", "Chase", "credit", False
    if has("card no.") and has("debit") and has("credit"):
        return "Capital One", "Capital One", "Capital One", "credit", False
    if has("trans. date") and has("post date"):
        return "Discover", "Discover", "Discover", "credit", True
    if has("status") and has("debit") and has("credit"):
        return "Citi", "Citi", "Citi", "credit", False
    if has("reference number") and has("payee"):
        return "Bank of America credit card", "Bank of America Credit Card", "Bank of America", "credit", False
    if has("running bal."):
        return "Bank of America", "Bank of America", "Bank of America", "checking", False
    if has("card member") or has("extended details") or has("appears on your statement as"):
        return "American Express", "American Express", "American Express", "credit", True
    return None


_DIRECTION = {"credit": 1, "cr": 1, "deposit": 1, "dep": 1, "in": 1, "+": 1,
              "debit": -1, "dr": -1, "withdrawal": -1, "wd": -1, "out": -1, "-": -1}


def _is_direction_header(h: str) -> bool:
    h = h.lower()
    return bool(re.search(r"credit\s*(or|/|&|and)\s*debit|debit\s*(or|/|&|and)\s*credit|^dr\s*/?\s*cr$|^cr\s*/?\s*dr$|"
                          r"^(transaction )?direction$|^debit\s*credit$|^credit\s*debit$", h))


def parse_csv(data: bytes, filename: str = "") -> ParseResult:
    rows = _rows(_decode(data))
    if not rows:
        raise ParseError("The file is empty.")

    notes: List[str] = []
    header_idx = -1
    headers: Optional[List[str]] = None
    detected = None

    r0 = rows[0]
    if len(r0) >= 5 and parse_date(r0[0]) and parse_amount(r0[1]) is not None and r0[2].strip() == "*":
        headers = ["date", "amount", "star", "check", "description"]
        detected = ("Wells Fargo", "Wells Fargo", "Wells Fargo", "checking", False)
    else:
        for i, row in enumerate(rows[:20]):
            h = [c.strip().lower() for c in row]
            if (any("date" in x for x in h)
                    and any(k in x for x in h for k in ("desc", "payee", "merchant", "name", "memo"))
                    and any(k in x for x in h for k in ("amount", "debit", "credit", "withdrawal", "deposit"))):
                header_idx, headers = i, h
                break
        if headers is None:
            sample = rows[0]
            di = next((j for j, x in enumerate(sample) if parse_date(x)), -1)
            ai = next((j for j, x in enumerate(sample) if j != di and parse_amount(x) is not None), -1)
            if di < 0 or ai < 0:
                raise ParseError("Couldn't find Date / Description / Amount columns in this CSV.")
            headers = [("date" if j == di else "amount" if j == ai else f"col{j}") for j in range(len(sample))]
            others = [j for j in range(len(sample)) if j not in (di, ai)]
            if others:
                headers[max(others, key=lambda j: len(sample[j]))] = "description"
            notes.append("No header row found, so the columns were guessed.")
        detected = _detect(headers)

    def find(*pats) -> int:
        for p in pats:
            for j, x in enumerate(headers):
                if (p(x) if callable(p) else x == p):
                    return j
        return -1

    ci = {
        "date": find("transaction date", "trans. date", "trans date", "date", "posting date", "posted date", lambda x: "date" in x),
        "desc": find("description", "payee", "merchant", "merchant name", "name",
                     lambda x: "desc" in x, lambda x: "payee" in x, lambda x: "merchant" in x, "memo"),
        "amount": find("amount", lambda x: x.startswith("amount"), lambda x: "amount" in x),
        "debit": find("debit", lambda x: "debit" in x and not _is_direction_header(x), lambda x: "withdrawal" in x),
        "credit": find("credit", lambda x: x.startswith("credit") and not _is_direction_header(x), lambda x: "deposit" in x),
        "cat": find("category"),
        "type": find("type"),
        "acctname": find("account name", "account"),
    }
    # Direction column (e.g. "Credit or Debit" = Credit/Debit) for files whose amounts are all positive
    ci["direction"] = -1
    for j, h in enumerate(headers):
        if j in (ci["amount"], ci["desc"], ci["date"]):
            continue
        vals = [r[j].strip().lower() for r in rows[header_idx + 1:header_idx + 200] if j < len(r) and r[j].strip()]
        if vals and (_is_direction_header(h) or h in ("type", "transaction type", "details")) \
                and sum(v in _DIRECTION for v in vals) >= 0.9 * len(vals):
            ci["direction"] = j
            break
    if ci["date"] < 0 or ci["desc"] < 0 or (ci["amount"] < 0 and ci["debit"] < 0 and ci["credit"] < 0):
        raise ParseError("Couldn't find Date / Description / Amount columns in this CSV.")

    def cell(r, k):
        j = ci[k]
        return r[j] if 0 <= j < len(r) else ""

    out: List[Candidate] = []
    for r in rows[header_idx + 1:]:
        date = parse_date(cell(r, "date"))
        if not date:
            continue
        raw_amt = cell(r, "amount").strip() if ci["amount"] >= 0 else ""
        if raw_amt:
            amt = parse_amount(raw_amt)
        else:
            d = parse_amount(cell(r, "debit")) if ci["debit"] >= 0 else None
            c = parse_amount(cell(r, "credit")) if ci["credit"] >= 0 else None
            if d is None and c is None:
                continue
            amt = abs(c or 0) - abs(d or 0)
        if amt is None:
            continue
        if ci["direction"] >= 0 and raw_amt:
            sign = _DIRECTION.get(cell(r, "direction").strip().lower())
            if sign:
                amt = sign * abs(amt)
        desc = " ".join(cell(r, "desc").split())
        if not desc:
            continue
        out.append(Candidate(date=date, description=desc, amount_cents=amt,
                             bank_category=cell(r, "cat").strip(), bank_type=cell(r, "type").strip()))

    if detected:
        label, account, institution, acct_type, flip = detected
    else:
        label, account, institution, acct_type, flip = "CSV", guess_account_from_filename(filename), None, "other", False
        names = {cell(r, "acctname").strip() for r in rows[header_idx + 1:] if cell(r, "acctname").strip()}
        if ci["acctname"] >= 0 and len(names) == 1:
            account = names.pop()[:60]                      # the file says which account it is
        if ci["direction"] >= 0:
            label = "CSV (credit/debit column)"
        elif ci["amount"] >= 0 and len(out) >= 3:
            pos = sum(1 for t in out if t.amount_cents > 0) / len(out)
            if pos > 0.7:
                flip = True
                acct_type = "credit"
                notes.append("Most amounts were positive, so this looks like a credit card that lists "
                             "charges as positive. The signs were flipped; use \"Flip +/- signs\" if that's wrong.")
    if flip:
        for t in out:
            t.amount_cents = -t.amount_cents
    if not out:
        raise ParseError("No transactions found in this CSV.")
    if detected:
        account = with_last4(account, last4_from_filename(filename))
    return ParseResult(candidates=out, format=label, account=account, institution=institution,
                       account_type=acct_type, notes=notes)

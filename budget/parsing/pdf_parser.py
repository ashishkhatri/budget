"""Best-effort PDF statement parser.

Bank PDFs aren't designed to be machine-read, so this looks for lines that
start with a MM/DD date and end with an amount, and uses the statement's
section headings and type (credit card vs. bank account) to decide the sign.
The import preview lets the user check and flip signs.
"""
from __future__ import annotations

import io
import re
from typing import List

from .common import (Candidate, ParseError, ParseResult, guess_account_from_filename, last4_from_filename,
                     parse_amount, with_last4)

MONTHNAMES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

AMT = r"\(?-?\$?\d{1,3}(?:,\d{3})*\.\d{2}\)?-?"
TX_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+)?(.+?)\s+(" + AMT + r")(?:\s+(" + AMT + r"))?$")
SKIP = re.compile(r"beginning balance|ending balance|opening balance|closing balance|balance forward|"
                  r"previous balance|new balance|^total|subtotal|daily balance", re.I)
INFLOW = re.compile(r"deposits?|additions|payments?(,| and) (other )?credits|^credits\b|other credits|refunds", re.I)
OUTFLOW = re.compile(r"withdrawals?|checks? paid|purchases?|fees|debits?|atm & debit|card activity|"
                     r"cash advances?|interest charged|electronic payments", re.I)
BANKS = [("chase", "Chase"), ("bank of america", "Bank of America"), ("wells fargo", "Wells Fargo"),
         ("capital one", "Capital One"), ("american express", "American Express"), ("discover", "Discover"),
         ("citi", "Citi")]


def _lines(data: bytes) -> List[str]:
    try:
        import pdfplumber
    except ImportError as e:  # pragma: no cover
        raise ParseError("PDF support isn't installed (pdfplumber). Run start.bat again.") from e
    lines: List[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                lines.extend(" ".join(l.split()) for l in text.splitlines() if l.strip())
    except Exception as e:
        raise ParseError(f"Couldn't open this PDF ({e.__class__.__name__}). If it's password-protected, "
                         "download a CSV instead.") from e
    return lines


def _statement_end(text: str):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s*(?:-|–|through|thru|to)\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", text, re.I)
    if m:
        y = int(m[6])
        return (y + 2000 if y < 100 else y), int(m[4])
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\s*(?:-|–|through|thru|to)\s*"
                  r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text, re.I)
    if m and m[4][:3].lower() in MONTHNAMES:
        return int(m[6]), MONTHNAMES[m[4][:3].lower()]
    dates = []
    for x in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text):
        y = int(x[3]); y = y + 2000 if y < 100 else y
        if 1 <= int(x[1]) <= 12 and 2000 < y < 2100:
            dates.append((y, int(x[1]), int(x[2])))
    if dates:
        y, mo, _ = max(dates)
        return y, mo
    from datetime import date
    t = date.today()
    return t.year, t.month


def parse_pdf(data: bytes, filename: str = "") -> ParseResult:
    lines = _lines(data)
    text = "\n".join(lines)
    if len(lines) < 3:
        raise ParseError("No text found in this PDF (it may be a scanned image). Try a CSV download instead.")

    is_card = bool(re.search(r"minimum payment|payment due date|credit (limit|access line)|new balance", text, re.I))
    end_y, end_m = _statement_end(text)
    low = text.lower()
    bank = next((name for key, name in BANKS if key in low), None)
    account = f"{bank} {'Credit Card' if is_card else 'Checking'}" if bank else guess_account_from_filename(filename)
    last4 = last4_from_filename(filename)
    if not last4:
        m = re.search(r"account (?:number|no\.?|#)\s*:?\s*(?:[X*•x]+[\s-]*)+(\d{4})\b", text, re.I)
        last4 = m.group(1) if m else None
    if bank:
        account = with_last4(account, last4)

    section = None
    out: List[Candidate] = []
    for line in lines:
        m = TX_RE.match(line)
        if not m or not (1 <= int(m[1]) <= 12 and 1 <= int(m[2]) <= 31):
            if len(line) < 70 and not re.search(r"\d+\.\d{2}", line):
                if INFLOW.search(line):
                    section = "in"
                elif OUTFLOW.search(line):
                    section = "out"
            continue
        desc = m[4].strip()
        if SKIP.search(desc):
            continue
        mo, d = int(m[1]), int(m[2])
        if m[3]:
            y = int(m[3]); y = y + 2000 if y < 100 else y
        else:
            y = end_y - 1 if mo > end_m else end_y
        raw = parse_amount(m[5])
        if raw is None:
            continue
        val = abs(raw)
        if is_card:
            amount = val if raw < 0 else -val          # card statements print charges as positive
        elif m[6]:
            amount = raw                               # has a balance column: the printed sign is real
            if raw > 0 and section == "out":
                amount = -val
        elif section == "in":
            amount = val
        else:
            amount = -val
        out.append(Candidate(date=f"{y:04d}-{mo:02d}-{d:02d}", description=desc, amount_cents=amount))

    if not out:
        raise ParseError("Couldn't find any transaction lines in this PDF. A CSV download from your bank will work better.")
    return ParseResult(candidates=out, format=f"{bank or 'Bank'} PDF statement" + (" (card)" if is_card else ""),
                       account=account, institution=bank, account_type="credit" if is_card else "checking",
                       notes=["Read from a PDF. Please check the amounts and signs before importing."])

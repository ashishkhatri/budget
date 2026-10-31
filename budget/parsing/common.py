"""Shared helpers and data types for statement parsers.

Every importer (CSV, PDF, and future bank-API providers) produces a list of
``Candidate`` objects. The ingest pipeline takes it from there, so new sources
only have to get data into this shape.

Sign convention everywhere: negative = money out, positive = money in.
Amounts are integer cents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional


class ParseError(ValueError):
    """Raised when a file can't be understood. The message is shown to the user."""


@dataclass
class Candidate:
    date: str                      # YYYY-MM-DD
    description: str
    amount_cents: int              # negative = money out
    bank_category: str = ""
    bank_type: str = ""
    external_id: Optional[str] = None   # stable id from a bank API, if any
    pending: bool = False


@dataclass
class ParseResult:
    candidates: List[Candidate]
    format: str                    # human label, e.g. "Chase credit card"
    account: str                   # suggested account name
    institution: Optional[str] = None
    account_type: str = "other"    # checking | savings | credit | other
    notes: List[str] = field(default_factory=list)


def norm(s: str) -> str:
    """Uppercase, turn * and # into spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[*#]", " ", str(s or "").upper())).strip()


_AMOUNT_RE = re.compile(r"\d*\.?\d+")


def parse_amount(s) -> Optional[int]:
    """Parse '$1,234.56', '(12.00)', '12.00-', '-5', '3.10 CR' into signed cents."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.upper().endswith("CR"):
        s, neg = s[:-2].strip(), not neg
    if s.endswith("-"):
        s, neg = s[:-1], not neg
    s = re.sub(r"[$,\s]", "", s)
    if s.startswith("-"):
        s, neg = s[1:], not neg
    if s.startswith("+"):
        s = s[1:]
    if not _AMOUNT_RE.fullmatch(s):
        return None
    try:
        cents = int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    return -cents if neg else cents


def parse_date(s) -> Optional[str]:
    s = str(s or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
        if not m:
            return None
        mo, d, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def last4_from_filename(fn: str) -> Optional[str]:
    """Chase names downloads like '20261009-statements-9743-.pdf' and 'Chase9743_Activity_20261018.csv'."""
    m = re.search(r"statements-(\d{4})-", fn or "", re.I) or re.search(r"chase(\d{4})_activity", fn or "", re.I)
    return m.group(1) if m else None


def with_last4(account: str, last4: Optional[str]) -> str:
    return f"{account} {last4}" if last4 else account


def guess_account_from_filename(fn: str) -> str:
    f = (fn or "").lower()
    for key, name in [("chase", "Chase"), ("amex", "American Express"), ("american", "American Express"),
                      ("discover", "Discover"), ("capital", "Capital One"), ("citi", "Citi"),
                      ("wells", "Wells Fargo"), ("bofa", "Bank of America"), ("bankofamerica", "Bank of America")]:
        if key in f:
            return name
    base = re.sub(r"\.[^.]+$", "", fn or "Imported")
    return re.sub(r"[_-]+", " ", base)[:40] or "Imported"

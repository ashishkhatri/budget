"""Transaction categorization.

Order of precedence:
  1. User rules ("description contains X" -> category); longest pattern wins.
  2. Bank transaction type codes (e.g. Chase ACCT_XFER -> Transfers).
  3. Built-in merchant keywords (keywords.py); longest keyword wins.
  4. The bank's own category column, when the file has one.
  5. Positive deposits that look like income.
  6. Uncategorized.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from .keywords import KEYWORDS
from .parsing.common import norm

UNCATEGORIZED = "Uncategorized"


def _compile(keyword: str):
    k = norm(keyword)
    right = r"(?![A-Z0-9])" if len(k) <= 4 or not k[-1].isalnum() else ""
    return k, re.compile(r"(?:^|[^A-Z0-9])" + re.escape(k) + right)


_KW: List[Tuple[str, "re.Pattern[str]", str]] = sorted(
    ((k, rx, cat) for cat, kws in KEYWORDS.items() for k, rx in map(_compile, kws)),
    key=lambda t: -len(t[0]),
)

_BANKCAT = [(re.compile(p, re.I), c) for p, c in [
    (r"grocer|supermarket", "Groceries"), (r"food|drink|restaurant|dining", "Dining"),
    (r"^transfer|loan payment|credit card payment", "Transfers"), (r"^income", "Income"),
    (r"^gas|gasoline|fuel", "Gas & Fuel"), (r"automotive|transport", "Transportation"),
    (r"travel|airfare|lodging|hotel|car rental", "Travel"), (r"entertain", "Entertainment"),
    (r"health|medical|pharmac", "Health & Fitness"), (r"bills|utilit|phone|cable|internet", "Bills & Utilities"),
    (r"shopping|merchandise|retail", "Shopping"), (r"^home", "Home"), (r"personal", "Personal Care"),
    (r"education", "Education"), (r"gift|donation|charit", "Gifts & Donations"),
    (r"fee|interest", "Fees & Interest"), (r"insurance", "Insurance"),
]]

Rule = Tuple[str, str]  # (normalized pattern, category name)


def prepare_rules(rules: Iterable[Tuple[str, str]]) -> List[Rule]:
    """Normalize and sort (pattern, category) pairs, longest pattern first."""
    out = [(norm(p), c) for p, c in rules if norm(p)]
    out.sort(key=lambda r: -len(r[0]))
    return out


def categorize(description: str, amount_cents: int, bank_category: str = "", bank_type: str = "",
               rules: Sequence[Rule] = (), merchant: Optional[Tuple[str, str]] = None) -> Tuple[str, str]:
    """Return (category name, source) where source is merchant|rule|builtin|bank|ai|none.

    ``merchant`` is (category name, 'manual'|'ai') when the transaction's merchant has a category.
    A category you set on a merchant wins; one the AI set comes after rules and built-in keywords.
    """
    if merchant and merchant[1] == "manual":
        return merchant[0], "merchant"
    n = norm(description)
    for pattern, cat in rules:
        if pattern in n:
            return cat, "rule"
    bt = (bank_type or "").upper()
    if bt in ("ACCT_XFER", "LOAN_PMT"):
        return "Transfers", "builtin"
    if bt == "ATM":
        return "Cash & ATM", "builtin"
    for _k, rx, cat in _KW:
        if rx.search(n):
            if cat == "Income" and amount_cents < 0:
                continue
            return cat, "builtin"
    if bank_category:
        for rx, cat in _BANKCAT:
            if rx.search(bank_category):
                return cat, "bank"
    if merchant and merchant[1] == "ai":
        return merchant[0], "ai"
    if amount_cents > 0 and re.search(r"DEPOSIT|ZELLE FROM|VENMO CASHOUT", n):
        return "Income", "builtin"
    return UNCATEGORIZED, "none"


_PREFIX = re.compile(r"^(RECURRING )?(CARD PURCHASE( WITH PIN)?|PURCHASE AUTHORIZED ON|POS (DEBIT|PURCHASE)|"
                     r"DEBIT CARD PURCHASE|CHECKCARD|ACH (DEBIT|CREDIT)|PURCHASE)\s*")
_STOP = {"PPD", "CCD", "WEB", "ID", "ID:", "ACH", "TEL", "POS"}
_KEEP_DIGITS = {"7-ELEVEN", "76", "AT&T"}


def merchant_key(description: str) -> str:
    return _name(description, 2)


def detail_key(description: str) -> str:
    """A longer name (up to four words), to tell apart businesses whose names start the same:
    'CLEAR LAKE HUNAN RESTAURA' and 'CLEAR LAKE WATER' both have the merchant key 'CLEAR LAKE'."""
    return _name(description, 4)


def _name(description: str, max_words: int) -> str:
    """A short, stable merchant name: the first two words of the name, without store numbers,
    dates, payment-processor prefixes or the city. Used for AI grouping, rule suggestions and
    Top merchants, so all three agree.

    'Card Purchase 09/14 STARBUCKS STORE 1234 CHICAGO' -> 'STARBUCKS STORE'
    '0252-GAYLORD TEXAN GRAPVINE TX'                  -> 'GAYLORD TEXAN'
    """
    s = norm(description)
    # ACH descriptions often name the company after "CO:" (e.g. "... TYPE: PAYMENT CO: CITI AUTOPAY NAME: ...")
    m = re.search(r"\bCO:\s*(.+?)(?=\s+(?:NAME|ENTRY|TYPE|ID|CO ID|ACH|TRACE|DESC|SEC)\b|:|$)", s)
    if m and m.group(1).strip():
        s = m.group(1).strip()
    s = re.sub(r"^(DEPOSIT|WITHDRAWAL|EXTERNAL|ONLINE|ELECTRONIC|PREAUTHORIZED|RECURRING)?\s*"
               r"(ACH|EFT)\s*(DEPOSIT|WITHDRAWAL|DEBIT|CREDIT|PMT|PAYMENT)?\s+", "", s)
    s = _PREFIX.sub("", s)
    s = re.sub(r"^\d{1,2}/\d{1,2}(/\d{2,4})?\s*", "", s)
    s = re.sub(r"^\d{3,}\s*-\s*|^\d{3,}\s+(?=[A-Z])", "", s)            # leading store number: '0252-' / '0252 '
    s = re.sub(r"^(SQ|TST|SP|PY|DD|PP|PAYPAL)\s+", "", s)
    keep: List[str] = []
    for tk in s.split(" "):
        if not tk or re.fullmatch(r"[^A-Z0-9&]+", tk):     # lone punctuation: 'TACODELI - KATY' ends at the dash
            if keep:
                break
            continue
        if tk in _STOP or (re.search(r"\d", tk) and tk not in _KEEP_DIGITS):
            if keep:
                break
            continue
        keep.append(tk)
        if len([k for k in keep if k not in ("&", "AND", "OF", "THE")]) >= max_words:
            break
    k = re.sub(r"[^A-Z0-9 &'.\-]", "", " ".join(keep)).strip().strip("-&.' ").strip()
    if len(k) < 2:
        k = s[:20].strip()
    return k


def pattern_matches(pattern: str, description: str) -> bool:
    p = norm(pattern)
    return bool(p) and p in norm(description)

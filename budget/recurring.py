"""Detect recurring charges (subscriptions, bills, memberships) from transaction history.

For each merchant we look at its charges, both all together (bills whose amount varies, like
utilities) and split into groups of similar amounts (so a $14.99 subscription is still found at a
merchant where you also shop, like Amazon). A group is recurring when the gaps between charges
mostly match one of the frequencies below.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from typing import Dict, List, Optional

# name, typical days between charges, allowed range, fewest charges needed to call it recurring
FREQUENCIES = [
    ("weekly", 7, (5, 9), 4),
    ("biweekly", 14, (12, 17), 3),
    ("monthly", 30.44, (25, 36), 3),
    ("bimonthly", 60.88, (54, 68), 3),
    ("quarterly", 91.31, (80, 102), 3),
    ("every4months", 121.75, (110, 135), 3),
    ("semiannual", 182.63, (165, 200), 3),
    ("yearly", 365.25, (340, 390), 2),
]
FREQ = {f[0]: f for f in FREQUENCIES}
LABELS = {"weekly": "Weekly", "biweekly": "Every 2 weeks", "monthly": "Monthly",
          "bimonthly": "Every 2 months", "quarterly": "Every 3 months", "every4months": "Every 4 months",
          "semiannual": "Every 6 months", "yearly": "Yearly"}
DISMISSED_KEY = "recurring_dismissed"
MANUAL_KEY = "recurring_manual"        # merchants you marked as recurring yourself


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _similar(a: int, b: int) -> bool:
    """Amounts close enough to be the same subscription (price changes and tax wobble allowed)."""
    a, b = abs(a), abs(b)
    return abs(a - b) <= max(100, 0.08 * max(a, b))


def _amount_groups(txs: List[dict]) -> List[List[dict]]:
    groups: List[List[dict]] = []
    for t in sorted(txs, key=lambda t: abs(t["amount_cents"])):
        for g in groups:
            if _similar(statistics.median(x["amount_cents"] for x in g), t["amount_cents"]):
                g.append(t)
                break
        else:
            groups.append([t])
    groups = [sorted(g, key=lambda t: t["date"]) for g in groups]
    return groups + _price_changes(groups, txs)


def _price_changes(groups: List[List[dict]], txs: List[dict]) -> List[List[dict]]:
    """A subscription whose price went up (or down) shows as two same-price runs, one after the
    other: $15.49 until March, $17.99 from April. Join runs like that when the new price is within
    50% of the old one and starts within ~6 months of the old one ending (for charges every 6 months)."""
    runs = sorted((g for g in groups if len(g) >= 2), key=lambda g: g[0]["date"])
    out = []
    for i, a in enumerate(runs):
        chain = list(a)
        for b in runs[i + 1:]:
            gap = (_d(b[0]["date"]) - _d(chain[-1]["date"])).days
            old, new = abs(chain[-1]["amount_cents"]), abs(b[0]["amount_cents"])
            if 0 < gap <= 200 and max(old, new) <= 1.5 * min(old, new):
                chain += b
        # Only when the chain is most of what the merchant charged over that time; otherwise it's
        # a few restaurant bills that happen to line up a month apart.
        lo, hi = chain[0]["date"], chain[-1]["date"]
        during = sum(1 for t in txs if lo <= t["date"] <= hi)
        if len(chain) > len(a) and len(chain) >= 0.6 * during:
            out.append(chain)
    return out


def _fit(txs: List[dict]) -> Optional[dict]:
    """Return the frequency these charges follow, or None."""
    # several charges on the same day count once (split shipments, retries)
    days = sorted({_d(t["date"]) for t in txs})
    if len(days) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    med = statistics.median(gaps)
    for name, period, (lo, hi), need in FREQUENCIES:
        if not lo <= med <= hi or len(days) < need:
            continue
        # a skipped period (gap about twice as long) is tolerated, not counted as a match
        ok = sum(lo <= g <= hi for g in gaps)
        skipped = sum(2 * lo <= g <= 2 * hi for g in gaps)
        if ok >= max(1, 0.7 * (len(gaps) - skipped)) and ok + skipped >= 0.8 * len(gaps):
            amounts = [abs(t["amount_cents"]) for t in txs]
            same = all(_similar(a, statistics.median(amounts)) for a in amounts)
            spread = max(amounts) / max(1, min(amounts))
            # Amounts may change: price increases, utility bills, a class that sometimes costs more.
            # How much depends on how often: monthly or slower may vary up to 3x (utilities);
            # weekly and every-2-weeks only up to 1.5x, because frequent visits with different
            # totals (groceries, gas) are shopping habits, not recurring charges.
            if not same and spread > (3 if period >= 28 else 1.5):
                continue
            if len(days) == 2 and not (spread <= 1.2 and 355 <= gaps[0] <= 376):
                continue                         # seen only twice: about the same price, ~a year apart
            return {"frequency": name, "period_days": period, "days": days, "gaps": gaps, "amounts": amounts}
    return None


def _fit_yearly(txs: List[dict]) -> Optional[dict]:
    """Yearly bills that don't fall on the same date or cost the same each time, like property tax:
    paid around the same time each year, sometimes in two installments a week apart.

    Charges within 45 days of each other count as one payment (their total). It's yearly when
    there are at least three payments (or two of the same amount), each 320-410 days after the one
    before, and no payment is more than three times another. A payment can be at most three
    charges; more is shopping.
    """
    groups: List[List[dict]] = []
    for t in sorted(txs, key=lambda t: t["date"]):
        if groups and (_d(t["date"]) - _d(groups[-1][0]["date"])).days <= 45:
            groups[-1].append(t)
        else:
            groups.append([t])
    if len(groups) < 2 or any(len(g) > 3 for g in groups):
        return None
    starts = [_d(g[0]["date"]) for g in groups]
    if not all(320 <= (b - a).days <= 410 for a, b in zip(starts, starts[1:])):
        return None
    totals = [sum(abs(t["amount_cents"]) for t in g) for g in groups]
    if max(totals) > 3 * min(totals):
        return None
    # Two payments a year apart happen by chance (the same restaurant on two birthdays), so two
    # need the same amount within about two weeks of a year apart; three or more years is a pattern.
    if len(groups) == 2 and not (max(totals) <= 1.2 * min(totals) and 350 <= (starts[1] - starts[0]).days <= 380):
        return None
    return {"frequency": "yearly", "period_days": 365.25, "days": starts,
            "gaps": [(b - a).days for a, b in zip(starts, starts[1:])], "amounts": totals}


def manual(con) -> List[dict]:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (MANUAL_KEY,)).fetchone()
    try:
        return [m for m in json.loads(row["value"]) if m.get("frequency") in FREQ] if row else []
    except (ValueError, TypeError, AttributeError):
        return []


def set_manual(con, merchant_id: int, frequency: str, add: bool) -> None:
    items = [m for m in manual(con) if not (m["merchant_id"] == merchant_id and m["frequency"] == frequency)]
    if add:
        items.append({"merchant_id": merchant_id, "frequency": frequency})
    con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (MANUAL_KEY, json.dumps(items)))


def _fit_manual(txs: List[dict], frequency: str) -> Optional[dict]:
    """You said this merchant is recurring: each period's amount is what was charged in the most
    recent period (e.g. two cars' registrations in a year), and the next charge is due a period
    after each of those."""
    if not txs:
        return None
    period = FREQ[frequency][1]
    last = _d(txs[-1]["date"])
    recent = [t for t in txs if (last - _d(t["date"])).days < period * 0.9]
    return {"frequency": frequency, "period_days": period, "manual": True,
            "days": sorted({_d(t["date"]) for t in txs}), "amounts": [sum(abs(t["amount_cents"]) for t in recent)],
            "dues": sorted(_d(t["date"]) + dt.timedelta(days=round(period)) for t in recent), "parts": len(recent)}


def dismissed(con) -> List[str]:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (DISMISSED_KEY,)).fetchone()
    try:
        return list(json.loads(row["value"])) if row else []
    except (ValueError, TypeError):
        return []


def set_dismissed(con, key: str, hide: bool) -> None:
    keys = set(dismissed(con))
    (keys.add if hide else keys.discard)(key)
    con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (DISMISSED_KEY, json.dumps(sorted(keys))))


def detect(con, today: Optional[dt.date] = None) -> Dict[str, object]:
    today = today or dt.date.today()
    rows = con.execute("""
        SELECT t.id, t.date, t.amount_cents, t.merchant_id, t.category_id, t.account_id,
               m.name AS merchant, c.kind
        FROM transactions t
        JOIN merchants m ON m.id = t.merchant_id
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.amount_cents < 0 AND COALESCE(c.kind, 'expense') != 'transfer'
        ORDER BY t.date""").fetchall()
    by_merchant: Dict[int, List[dict]] = {}
    for r in rows:
        by_merchant.setdefault(r["merchant_id"], []).append(dict(r))

    # How far each account's imported history goes. A charge isn't late or stopped just because
    # newer statements haven't been imported yet.
    covered = {r[0]: _d(r[1]) for r in con.execute("SELECT account_id, MAX(date) FROM transactions GROUP BY account_id")}
    hidden = set(dismissed(con))
    marked: Dict[int, List[str]] = {}
    for m in manual(con):
        marked.setdefault(m["merchant_id"], []).append(m["frequency"])
    found = []
    for mid, txs in by_merchant.items():
        hits = []
        whole = _fit(txs)
        if whole:
            hits.append((whole, txs))
        else:
            for g in _amount_groups(txs):
                f = _fit(g)
                # a yearly price among a merchant's other purchases is chance ($4.18 at Starbucks
                # twice, a year apart), so yearly needs the merchant's charges to be only that
                if f and f["frequency"] != "yearly":
                    hits.append((f, g))
            # a price-change chain covers its runs: keep the longest, drop the parts it includes
            kept = []
            for f, g in sorted(hits, key=lambda h: -len(h[1])):
                ids = {t["id"] for t in g}
                if not any(ids <= k for k, _ in kept):
                    kept.append((ids, (f, g)))
            hits = [h for _, h in kept]
            if not hits:
                y = _fit_yearly(txs)
                if y:
                    hits.append((y, txs))
        for freq in marked.get(mid, []):
            if not any(f["frequency"] == freq for f, _ in hits):
                hits.append((_fit_manual(txs, freq), txs))
        for f, g in hits:
            amounts = f["amounts"]
            recent = amounts[-3:]
            typical = int(statistics.median(recent)) if not f.get("manual") else amounts[-1]
            low, high = min(amounts), max(amounts)
            varies = not all(_similar(a, typical) for a in amounts)
            last = f["days"][-1]
            next_due = last + dt.timedelta(days=round(f["period_days"]))
            if f.get("dues"):
                last = max(_d(t["date"]) for t in g)
                upcoming = [d for d in f["dues"] if d >= today]
                next_due = upcoming[0] if upcoming else f["dues"][-1]
            as_of = min(today, max(covered.get(t["account_id"], last) for t in g))
            overdue = (as_of - next_due).days
            # quiet for more than half a period past when the next charge was due -> probably stopped
            active = overdue <= max(5, f["period_days"] * 0.5)
            late = 3 < overdue and active
            key = (f"manual:{mid}:{f['frequency']}" if f.get("manual")
                   else f"{mid}:{f['frequency']}:{typical if len(hits) > 1 else 0}")
            cats = [t["category_id"] for t in g]
            found.append({
                "key": key, "merchant_id": mid, "merchant": g[0]["merchant"],
                "category_id": max(set(cats), key=cats.count),
                "frequency": f["frequency"], "label": LABELS[f["frequency"]],
                "amount_cents": typical, "min_cents": low, "max_cents": high, "varies": varies,
                "monthly_cents": round(typical * 30.44 / f["period_days"]),
                "yearly_cents": round(typical * 365.25 / f["period_days"]),
                "count": len(g), "first_date": f["days"][0].isoformat(), "last_date": last.isoformat(),
                "next_date": next_due.isoformat(), "days_until": (next_due - today).days, "late": late,
                "imported_through": as_of.isoformat(),
                "active": active, "dismissed": key in hidden and not f.get("manual"),
                "manual": bool(f.get("manual")), "parts": f.get("parts", 1),
                "transaction_ids": [t["id"] for t in g],
            })
    found.sort(key=lambda r: -r["monthly_cents"])
    act = [r for r in found if r["active"] and not r["dismissed"]]
    return {
        "items": found,
        "monthly_cents": sum(r["monthly_cents"] for r in act),
        "yearly_cents": sum(r["yearly_cents"] for r in act),
        "active_count": len(act),
    }

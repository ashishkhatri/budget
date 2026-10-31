"""Read-only queries for the dashboard and budgets pages."""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional


def shift_month(ym: str, delta: int) -> str:
    y, m = map(int, ym.split("-"))
    m += delta
    while m < 1:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    return f"{y:04d}-{m:02d}"


def months(con, account_id: Optional[int] = None) -> List[str]:
    q = "SELECT DISTINCT substr(date, 1, 7) m FROM transactions"
    args = []
    if account_id:
        q += " WHERE account_id = ?"; args.append(account_id)
    return [r[0] for r in con.execute(q + " ORDER BY m DESC", args)]


def _acct(account_id):
    return (" AND t.account_id = ?", [account_id]) if account_id else ("", [])


def range_totals(con, start: str, end_excl: str, account_id: Optional[int] = None) -> dict:
    """Income, spending and per-category spending for start <= date < end_excl."""
    extra, args = _acct(account_id)
    rows = con.execute(f"""
        SELECT c.id, c.name, c.color, c.kind, SUM(t.amount_cents) total, COUNT(*) n
        FROM transactions t JOIN categories c ON c.id = t.category_id
        WHERE t.date >= ? AND t.date < ? {extra}
        GROUP BY c.id""", [start, end_excl, *args]).fetchall()
    income = sum(r["total"] for r in rows if r["kind"] == "income")
    spend = -sum(r["total"] for r in rows if r["kind"] == "expense")
    by_cat = {r["id"]: {"category_id": r["id"], "name": r["name"], "color": r["color"], "spent_cents": -r["total"],
                        "count": r["n"]} for r in rows if r["kind"] == "expense"}
    return {"income_cents": income, "spend_cents": spend, "by_category": by_cat}


def month_totals(con, month: str, account_id: Optional[int] = None) -> dict:
    return range_totals(con, month + "-01", shift_month(month, 1) + "-01", account_id)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _month_aligned(start: date, end_incl: date) -> Optional[int]:
    """Number of whole calendar months if the range is exactly whole months, else None."""
    if start.day != 1 or (end_incl + timedelta(days=1)).day != 1:
        return None
    return (end_incl.year - start.year) * 12 + end_incl.month - start.month + 1


def summary(con, month: Optional[str] = None, account_id: Optional[int] = None,
            start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """Dashboard numbers for a month (default: latest with data) or any date range (start/end inclusive)."""
    ms = months(con, account_id)
    if not ms:
        return {"empty": True, "months": []}
    if start and end:
        s, e = _d(start), _d(end)
        if e < s:
            s, e = e, s
    else:
        if not month or month not in ms:
            month = ms[0]
        s = _d(month + "-01")
        e = _d(shift_month(month, 1) + "-01") - timedelta(days=1)
    e_excl = e + timedelta(days=1)
    n_months = _month_aligned(s, e)
    days = (e_excl - s).days
    months_factor = n_months if n_months else round(days / 30.4375, 2)

    # the same-length period just before, for "▲ vs previous" comparisons
    if n_months:
        ps = _d(shift_month(s.strftime("%Y-%m"), -n_months) + "-01")
        pe_excl = s
    else:
        ps, pe_excl = s - timedelta(days=days), s
    cur = range_totals(con, s.isoformat(), e_excl.isoformat(), account_id)
    prev = range_totals(con, ps.isoformat(), pe_excl.isoformat(), account_id)

    budgets = {r[0]: r[1] for r in con.execute("SELECT category_id, monthly_cents FROM budgets")}
    cats = {r["id"]: r for r in con.execute("SELECT id, name, color, kind FROM categories")}
    # Budgets cover only the part of the period you have data for (no budget for months not imported yet).
    extra0, args0 = _acct(account_id)
    dfirst, dlast = con.execute(f"SELECT MIN(t.date), MAX(t.date) FROM transactions t WHERE 1=1 {extra0}", args0).fetchone()
    bs = max(s, _d(dfirst[:7] + "-01"))
    be = min(e, _d(shift_month(dlast[:7], 1) + "-01") - timedelta(days=1))
    if be < bs:
        budget_factor = 0
    elif (bs, be) == (s, e):
        budget_factor = months_factor
    else:
        bm = _month_aligned(bs, be)
        budget_factor = bm if bm else round(((be - bs).days + 1) / 30.4375, 2)
    scaled = {k: round(v * budget_factor) for k, v in budgets.items()}

    by = dict(cur["by_category"])
    for cid in budgets:
        if cid not in by and cid in cats and cats[cid]["kind"] == "expense":
            c = cats[cid]
            by[cid] = {"category_id": cid, "name": c["name"], "color": c["color"], "spent_cents": 0, "count": 0}
    rows = sorted((dict(v, budget_cents=scaled.get(k)) for k, v in by.items() if v["spent_cents"] or budgets.get(k)),
                  key=lambda r: -r["spent_cents"])

    # trend: each month in the period, or the 12 months ending at it when the period is short
    extra, args = _acct(account_id)
    first_m, last_m = s.strftime("%Y-%m"), e.strftime("%Y-%m")
    span = (e.year - s.year) * 12 + e.month - s.month + 1
    if span < 3:
        first_m = shift_month(last_m, -11)
        span = 12
    if span > 60:                                        # keep the chart readable
        first_m, span = shift_month(last_m, -59), 60
    trend_months = [shift_month(first_m, i) for i in range(span)]
    trend = {m: {"income_cents": 0, "spend_cents": 0} for m in trend_months}
    for r in con.execute(f"""
        SELECT substr(t.date, 1, 7) m, c.kind, SUM(t.amount_cents) total
        FROM transactions t JOIN categories c ON c.id = t.category_id
        WHERE t.date >= ? AND t.date < ? {extra} GROUP BY m, c.kind""",
            [first_m + "-01", shift_month(trend_months[-1], 1) + "-01", *args]):
        if r["m"] in trend:
            if r["kind"] == "income":
                trend[r["m"]]["income_cents"] += r["total"]
            elif r["kind"] == "expense":
                trend[r["m"]]["spend_cents"] -= r["total"]
    in_period = {m for m in trend_months if s.strftime("%Y-%m") <= m <= e.strftime("%Y-%m")}

    rng = [s.isoformat(), e_excl.isoformat(), *args]
    merchants = con.execute(f"""
        SELECT COALESCE(m.name, t.merchant), -SUM(t.amount_cents) spent, t.merchant_id
        FROM transactions t JOIN categories c ON c.id = t.category_id
        LEFT JOIN merchants m ON m.id = t.merchant_id
        WHERE c.kind = 'expense' AND t.amount_cents < 0 AND t.date >= ? AND t.date < ? {extra}
        GROUP BY COALESCE(t.merchant_id, t.merchant) ORDER BY spent DESC LIMIT 8""", rng).fetchall()
    uncat = con.execute(f"""
        SELECT COUNT(*) FROM transactions t JOIN categories c ON c.id = t.category_id
        WHERE c.name = 'Uncategorized' AND t.date >= ? AND t.date < ? {extra}""", rng).fetchone()[0]
    tx_count = con.execute(f"SELECT COUNT(*) FROM transactions t WHERE t.date >= ? AND t.date < ? {extra}", rng).fetchone()[0]
    budgeted = [k for k in budgets if k in cats and cats[k]["kind"] == "expense"]
    total_budget = sum(scaled[k] for k in budgeted)
    budgeted_spend = sum(cur["by_category"].get(k, {}).get("spent_cents", 0) for k in budgeted)
    first_date, last_date = con.execute(
        f"SELECT MIN(t.date), MAX(t.date) FROM transactions t WHERE 1=1 {extra}", args).fetchone()

    return {
        "empty": False, "start": s.isoformat(), "end": e.isoformat(), "months": ms,
        "month": last_m if n_months == 1 else None, "n_months": n_months, "months_factor": months_factor,
        "budget_months": budget_factor,
        "prev_start": ps.isoformat(), "prev_end": (pe_excl - timedelta(days=1)).isoformat(),
        "income_cents": cur["income_cents"], "spend_cents": cur["spend_cents"],
        "prev_income_cents": prev["income_cents"], "prev_spend_cents": prev["spend_cents"],
        "by_category": rows,
        "trend": [dict(month=k, in_period=k in in_period, **v) for k, v in trend.items()],
        "top_merchants": [{"merchant": r[0], "spent_cents": r[1], "merchant_id": r[2]} for r in merchants],
        "uncategorized": uncat, "tx_count": tx_count, "total_budget_cents": total_budget,
        "budgeted_spend_cents": budgeted_spend, "data_first": first_date, "data_last": last_date,
    }


def budget_table(con, month: Optional[str]) -> dict:
    ms = months(con)
    if not month:
        month = ms[0] if ms else None
    if not month:
        from datetime import date
        month = date.today().strftime("%Y-%m")
    cur = month_totals(con, month)["by_category"]
    prior = [shift_month(month, -i) for i in (1, 2, 3)]
    prior_with_data = [m for m in prior if m in ms] or prior[:1]
    prev = [month_totals(con, m)["by_category"] for m in prior_with_data]
    budgets = {r[0]: r[1] for r in con.execute("SELECT category_id, monthly_cents FROM budgets")}
    rows = []
    for c in con.execute("SELECT id, name, color FROM categories WHERE kind = 'expense' ORDER BY sort_order, name"):
        avg = sum(p.get(c["id"], {}).get("spent_cents", 0) for p in prev) / len(prev)
        rows.append({"category_id": c["id"], "name": c["name"], "color": c["color"],
                     "budget_cents": budgets.get(c["id"]), "spent_cents": cur.get(c["id"], {}).get("spent_cents", 0),
                     "avg3_cents": round(avg)})
    return {"month": month, "rows": rows}

"""SQLite storage: connection, schema migrations, seed data, backups.

Schema changes go in MIGRATIONS as a new numbered entry. Never edit an old
one: existing databases have already run it. ``PRAGMA user_version`` records
which migrations a database file has.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

DEFAULT_CATEGORIES = [
    # name, color, kind
    ("Income", "#2f6b4f", "income"), ("Groceries", "#5b8c3a", "expense"), ("Dining", "#d9822b", "expense"),
    ("Gas & Fuel", "#a05a2c", "expense"), ("Transportation", "#3c7fb0", "expense"), ("Shopping", "#8e5bb5", "expense"),
    ("Home", "#7a6a4f", "expense"), ("Bills & Utilities", "#4a6fa5", "expense"), ("Rent & Mortgage", "#355070", "expense"),
    ("Insurance", "#5c7c8a", "expense"), ("Subscriptions", "#c2549d", "expense"), ("Entertainment", "#e0a100", "expense"),
    ("Travel", "#1f9e9e", "expense"), ("Health & Fitness", "#d1495b", "expense"), ("Personal Care", "#c97c9e", "expense"),
    ("Education", "#6c8ead", "expense"), ("Pets", "#9c7a3c", "expense"), ("Gifts & Donations", "#b56576", "expense"),
    ("Fees & Interest", "#8d6e63", "expense"), ("Cash & ATM", "#7d8a6a", "expense"),
    ("Transfers", "#9aa0a6", "transfer"), ("Uncategorized", "#b0aea6", "expense"),
]
PROTECTED_CATEGORIES = {"Income", "Transfers", "Uncategorized"}
# Note: migrations run once each, in order, and are recorded in PRAGMA user_version.

MIGRATIONS = {
    1: """
    CREATE TABLE categories (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
        color       TEXT NOT NULL DEFAULT '#b0aea6',
        kind        TEXT NOT NULL DEFAULT 'expense' CHECK (kind IN ('expense','income','transfer')),
        sort_order  INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE accounts (
        id           INTEGER PRIMARY KEY,
        name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        institution  TEXT,
        type         TEXT NOT NULL DEFAULT 'other',      -- checking | savings | credit | other
        source       TEXT NOT NULL DEFAULT 'file',       -- file | plaid | simplefin | ...
        external_id  TEXT,                               -- id at the bank API, if linked
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE imports (
        id            INTEGER PRIMARY KEY,
        source        TEXT NOT NULL,                     -- file | provider name
        filename      TEXT,
        account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        format        TEXT,
        row_count     INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE transactions (
        id               INTEGER PRIMARY KEY,
        account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        date             TEXT NOT NULL,                  -- YYYY-MM-DD
        description      TEXT NOT NULL,
        amount_cents     INTEGER NOT NULL,               -- negative = money out
        category_id      INTEGER NOT NULL REFERENCES categories(id),
        category_source  TEXT NOT NULL DEFAULT 'none',   -- manual | rule | builtin | bank | none
        merchant         TEXT,
        bank_category    TEXT,
        bank_type        TEXT,
        note             TEXT,
        external_id      TEXT,                           -- bank API transaction id
        pending          INTEGER NOT NULL DEFAULT 0,
        import_id        INTEGER REFERENCES imports(id) ON DELETE SET NULL,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX ix_tx_date ON transactions(date);
    CREATE INDEX ix_tx_account_date ON transactions(account_id, date);
    CREATE INDEX ix_tx_category ON transactions(category_id);
    CREATE UNIQUE INDEX ux_tx_external ON transactions(account_id, external_id) WHERE external_id IS NOT NULL;
    CREATE TABLE rules (
        id           INTEGER PRIMARY KEY,
        pattern      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE budgets (
        category_id    INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
        monthly_cents  INTEGER NOT NULL
    );
    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT
    );
    """,
    2: """
    ALTER TABLE imports ADD COLUMN file_sha256 TEXT;
    CREATE INDEX ix_imports_sha ON imports(file_sha256);
    """,
    # Merchants become their own table. Transactions point at them; a merchant keeps its
    # name, category and alternate names even when it has no transactions.
    3: """
    CREATE TABLE merchants (
        id               INTEGER PRIMARY KEY,
        name             TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- display name (you can rename it)
        category_id      INTEGER REFERENCES categories(id) ON DELETE SET NULL,  -- NULL = not set
        category_source  TEXT,                                  -- manual (you) | ai
        notes            TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE merchant_aliases (
        key          TEXT PRIMARY KEY COLLATE NOCASE,   -- cleaned-up name from bank descriptions
        merchant_id  INTEGER NOT NULL REFERENCES merchants(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_alias_merchant ON merchant_aliases(merchant_id);
    ALTER TABLE transactions ADD COLUMN merchant_id INTEGER REFERENCES merchants(id) ON DELETE SET NULL;
    CREATE INDEX ix_tx_merchant ON transactions(merchant_id);
    """,
    4: """
    -- "descriptions containing X belong to merchant Y", from splitting a merchant. Checked before
    -- the short merchant name, longest pattern first.
    CREATE TABLE merchant_patterns (
        pattern      TEXT PRIMARY KEY COLLATE NOCASE,
        merchant_id  INTEGER NOT NULL REFERENCES merchants(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_pattern_merchant ON merchant_patterns(merchant_id);
    """,
    5: """
    -- merchant names are always in capitals, like the bank's
    UPDATE merchants SET name = UPPER(name) WHERE name != UPPER(name);
    """,
    6: """
    -- bank connections through Plaid (access tokens are encrypted, see secrets.py)
    CREATE TABLE plaid_items (
        id              INTEGER PRIMARY KEY,
        item_id         TEXT NOT NULL UNIQUE,
        access_token    TEXT NOT NULL,
        institution     TEXT,
        institution_id  TEXT,
        cursor          TEXT,
        last_sync       TEXT,
        last_error      TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    ALTER TABLE accounts ADD COLUMN plaid_item INTEGER REFERENCES plaid_items(id) ON DELETE SET NULL;
    """,
}


def _migrate_3_data(con) -> None:
    """Create a merchant for every merchant name already on transactions, link them, and turn
    rules that are exactly a merchant name into that merchant's category."""
    from .categorize import merchant_key
    keys = {}
    for tid, desc, m in con.execute("SELECT id, description, merchant FROM transactions").fetchall():
        k = m or merchant_key(desc)
        keys.setdefault(k, []).append(tid)
    for k, tids in keys.items():
        mid = con.execute("INSERT INTO merchants(name) VALUES (?)", (k,)).lastrowid
        con.execute("INSERT INTO merchant_aliases(key, merchant_id) VALUES (?,?)", (k, mid))
        con.executemany("UPDATE transactions SET merchant_id = ?, merchant = ? WHERE id = ?", [(mid, k, t) for t in tids])
    for rid, pattern, cid in con.execute("SELECT id, pattern, category_id FROM rules").fetchall():
        row = con.execute("SELECT merchant_id FROM merchant_aliases WHERE key = ?", (pattern,)).fetchone()
        if row:
            con.execute("UPDATE merchants SET category_id = ?, category_source = 'manual' WHERE id = ?", (cid, row[0]))
            con.execute("DELETE FROM rules WHERE id = ?", (rid,))


MIGRATION_DATA = {3: _migrate_3_data}


def squash(text) -> str:
    """Lowercase letters and digits only, for searches that ignore punctuation and spacing:
    'H-E-B', 'heb' and 'H E B' all become 'heb'."""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("squash", 1, squash, deterministic=True)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for v in sorted(MIGRATIONS):
            if v > version:
                # schema change + data conversion + version bump succeed or fail together
                try:
                    con.executescript("BEGIN;\n" + MIGRATIONS[v])
                    if v in MIGRATION_DATA:
                        MIGRATION_DATA[v](con)
                    con.execute(f"PRAGMA user_version = {v}")
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
        if con.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            with con:
                con.executemany("INSERT INTO categories(name, color, kind, sort_order) VALUES (?,?,?,?)",
                                [(n, c, k, i) for i, (n, c, k) in enumerate(DEFAULT_CATEGORIES)])
    finally:
        con.close()


def backup(db_path, backup_dir, label: str = "") -> Path:
    """Copy the database safely (works while the app is running)."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = backup_dir / f"budget_{stamp}{('_' + label) if label else ''}.sqlite"
    src = connect(db_path)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def list_backups(backup_dir) -> List[Path]:
    d = Path(backup_dir)
    return sorted(d.glob("budget_*.sqlite"), reverse=True) if d.exists() else []


def auto_backup(db_path, backup_dir, keep: int = 14) -> None:
    """Make one automatic backup per day and keep the newest ``keep`` of them."""
    today = datetime.now().strftime("%Y-%m-%d")
    autos = sorted(Path(backup_dir).glob("budget_*_auto.sqlite")) if Path(backup_dir).exists() else []
    if not Path(db_path).exists():
        return
    if not any(p.name.startswith(f"budget_{today}") for p in autos):
        backup(db_path, backup_dir, "auto")
        autos = sorted(Path(backup_dir).glob("budget_*_auto.sqlite"))
    for old in autos[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass

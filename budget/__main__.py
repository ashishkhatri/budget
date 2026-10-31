"""Start the app:  python -m budget  [--port 8765] [--no-browser]"""
from __future__ import annotations

import argparse
import logging
import socket
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from . import db as dbm
from .app import create_app


def _in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _log_ai(path: Path) -> None:
    """Write every AI request (timing, answer, thinking) to data\\ai.log, kept small."""
    try:
        h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg = logging.getLogger("budget.ai")
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    except OSError as e:
        print("Warning: can't write the AI log:", e)


def _sync_banks(db_path: str) -> None:
    """Fetch new transactions from connected banks (Plaid) in the background when the app starts."""
    from . import plaid
    con = dbm.connect(db_path)
    try:
        if con.execute("SELECT 1 FROM plaid_items LIMIT 1").fetchone():
            res = plaid.sync_all(con)
            n = sum(r["added"] for r in res["results"])
            print(f"Bank sync: {n} new transaction(s)." + (" Problems: " + "; ".join(res["errors"]) if res["errors"] else ""))
    except Exception as e:  # never stop the app over a sync problem
        print("Bank sync failed:", e)
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="budget")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"

    if _in_use(args.port):
        print(f"Budget is already running at {url}. Opening it in your browser.")
        webbrowser.open(url)
        return

    app = create_app()
    _log_ai(Path(app.config["DB_PATH"]).parent / "ai.log")
    try:
        dbm.auto_backup(app.config["DB_PATH"], app.config["BACKUP_DIR"])
    except Exception as e:  # a failed backup should never stop the app
        print("Warning: automatic backup failed:", e)

    threading.Thread(target=_sync_banks, args=(app.config["DB_PATH"],), daemon=True).start()
    print(f"Budget v{__version__} running at {url}")
    print(f"Database: {app.config['DB_PATH']}")
    print("Keep this window open while you use the app. Close it to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

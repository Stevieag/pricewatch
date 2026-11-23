"""SQLite storage (stdlib only). One file, lives in the data dir, never in the repo."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id               INTEGER PRIMARY KEY,
    parent_id        INTEGER REFERENCES items(id) ON DELETE CASCADE,
    url              TEXT NOT NULL,
    title            TEXT,
    currency         TEXT,
    current_price    TEXT,
    target_price     TEXT,
    interval_minutes INTEGER NOT NULL,
    next_check_at    INTEGER NOT NULL,
    last_checked_at  INTEGER,
    last_error       TEXT,
    extract_method   TEXT,
    in_stock         INTEGER,
    active           INTEGER NOT NULL DEFAULT 1,
    source           TEXT NOT NULL DEFAULT 'user',   -- user | agent
    created_at       INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS items_url_parent ON items(url, IFNULL(parent_id, 0));
CREATE INDEX IF NOT EXISTS items_due ON items(active, next_check_at);

CREATE TABLE IF NOT EXISTS prices (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    price      TEXT NOT NULL,
    currency   TEXT,
    checked_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS prices_item ON prices(item_id, checked_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    status      TEXT NOT NULL,      -- running | done | error
    summary     TEXT
);

-- v4 fair price: one row per persona fetch, plus 'you' rows for prices the user saw
CREATE TABLE IF NOT EXISTS fair_checks (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    persona    TEXT NOT NULL,
    price      TEXT,
    currency   TEXT,
    error      TEXT,
    checked_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS fair_item ON fair_checks(item_id, checked_at);
"""


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
        if str(path) != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    # ---------------------------------------------------------------- items

    def add_item(self, *, url, title, price, currency, interval_minutes, target_price=None,
                 parent_id=None, source="user", method=None, in_stock=None) -> int:
        t = now()
        with self.tx() as c:
            existing = c.execute(
                "SELECT id FROM items WHERE url=? AND IFNULL(parent_id,0)=IFNULL(?,0)",
                (url, parent_id)).fetchone()
            if existing:
                return existing["id"]
            cur = c.execute(
                """INSERT INTO items(parent_id,url,title,currency,current_price,target_price,
                   interval_minutes,next_check_at,last_checked_at,extract_method,in_stock,
                   source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (parent_id, url, title, currency, _s(price), _s(target_price), interval_minutes,
                 t + interval_minutes * 60, t if price is not None else None, method,
                 _b(in_stock), source, t))
            iid = cur.lastrowid
            if price is not None:
                c.execute("INSERT INTO prices(item_id,price,currency,checked_at) VALUES(?,?,?,?)",
                          (iid, _s(price), currency, t))
            return iid

    def record_check(self, item_id: int, *, price=None, currency=None, title=None,
                     error=None, method=None, in_stock=None) -> None:
        t = now()
        with self.tx() as c:
            row = c.execute("SELECT interval_minutes FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return
            nxt = t + row["interval_minutes"] * 60
            if error:
                c.execute("UPDATE items SET last_error=?, last_checked_at=?, next_check_at=? WHERE id=?",
                          (error[:500], t, nxt, item_id))
                return
            c.execute(
                """UPDATE items SET current_price=?, currency=COALESCE(?,currency),
                   title=COALESCE(title,?), last_error=NULL, last_checked_at=?, next_check_at=?,
                   extract_method=?, in_stock=? WHERE id=?""",
                (_s(price), currency, title, t, nxt, method, _b(in_stock), item_id))
            c.execute("INSERT INTO prices(item_id,price,currency,checked_at) VALUES(?,?,?,?)",
                      (item_id, _s(price), currency, t))

    def due_items(self, limit=20):
        return self.q("SELECT * FROM items WHERE active=1 AND next_check_at<=? "
                      "ORDER BY next_check_at LIMIT ?", (now(), limit))

    def get_item(self, item_id: int):
        return self.one("SELECT * FROM items WHERE id=?", (item_id,))

    def top_items(self):
        return self.q("""
            SELECT i.*,
              (SELECT MIN(CAST(a.current_price AS REAL)) FROM items a
                 WHERE a.parent_id=i.id AND a.active=1 AND a.current_price IS NOT NULL
                   AND IFNULL(a.currency,'')=IFNULL(i.currency,'')) AS best_alt_price,
              (SELECT COUNT(*) FROM items a WHERE a.parent_id=i.id) AS alt_count,
              (SELECT p.price FROM prices p WHERE p.item_id=i.id ORDER BY checked_at ASC LIMIT 1) AS first_price
            FROM items i WHERE i.parent_id IS NULL ORDER BY i.created_at DESC""")

    def alternatives(self, item_id: int):
        return self.q("SELECT * FROM items WHERE parent_id=? ORDER BY CAST(current_price AS REAL)",
                      (item_id,))

    def history(self, item_id: int, since: int = 0):
        return self.q("SELECT price, currency, checked_at FROM prices WHERE item_id=? AND checked_at>=? "
                      "ORDER BY checked_at", (item_id, since))

    def update_item(self, item_id: int, **fields):
        allowed = {"interval_minutes", "target_price", "active", "title"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        if "interval_minutes" in sets:
            sets["next_check_at"] = now() + sets["interval_minutes"] * 60
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.tx() as c:
            c.execute(f"UPDATE items SET {cols} WHERE id=?", (*sets.values(), item_id))

    def force_due(self, item_id: int):
        with self.tx() as c:
            c.execute("UPDATE items SET next_check_at=0 WHERE id=?", (item_id,))

    def delete_item(self, item_id: int):
        with self.tx() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))

    # ---------------------------------------------------------------- agent runs

    def start_run(self, item_id: int) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO agent_runs(item_id,started_at,status) VALUES(?,?,'running')",
                             (item_id, now())).lastrowid

    def finish_run(self, run_id: int, status: str, summary: str):
        with self.tx() as c:
            c.execute("UPDATE agent_runs SET finished_at=?, status=?, summary=? WHERE id=?",
                      (now(), status, summary[:2000], run_id))

    def last_run(self, item_id: int):
        return self.one("SELECT * FROM agent_runs WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,))


def _s(v):
    return None if v is None else str(Decimal(str(v)))


def _b(v):
    return None if v is None else int(bool(v))

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  rack_count INTEGER NOT NULL DEFAULT 2,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS hang_tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL UNIQUE,
  store_code TEXT NOT NULL,
  pickup_code TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  due_date TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ticket_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  garment TEXT NOT NULL,
  qty INTEGER NOT NULL,
  remark TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS process_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  operator TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pickups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL UNIQUE,
  pickup_code TEXT NOT NULL,
  picked_at TEXT NOT NULL,
  operator TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS day_gates (
  day TEXT PRIMARY KEY,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
-- 跨店转挂回执：口径A（同票改所属店）。票号不变，仅迁所属店；
-- 一行回执对应一次原子转挂，是审计/重放的留痕，不是占用计数器。
CREATE TABLE IF NOT EXISTS transfer_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  from_store TEXT NOT NULL,
  to_store TEXT NOT NULL,
  status_snapshot TEXT NOT NULL,
  operator TEXT NOT NULL,
  idempotency_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transfer_receipts_ticket
  ON transfer_receipts(ticket_no, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_idempotency
  ON transfer_receipts(idempotency_key)
  WHERE idempotency_key <> '';
"""

STATUSES = ("已收", "水洗", "烘干", "熨烫", "待取", "已取", "超期隔离", "丢失隔离")
FORWARD = {
    "已收": ("水洗", "烘干", "熨烫", "待取"),
    "水洗": ("烘干", "熨烫", "待取"),
    "烘干": ("熨烫", "待取"),
    "熨烫": ("待取",),
    "待取": (),
}

# 不在途的票：已取关账、丢失隔离（与 gate.refresh_in_flight 的水位口径一致）。
# 跨店转挂仅允许在途票；超期隔离仍属在途，可转挂。
NOT_IN_FLIGHT = ("已取", "丢失隔离")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)

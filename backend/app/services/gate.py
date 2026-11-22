from __future__ import annotations

from datetime import date, datetime

from app.db import NOT_IN_FLIGHT, connect


def today_str() -> str:
    return date.today().isoformat()


def is_day_sealed(day: str | None = None) -> bool:
    d = day or today_str()
    conn = connect()
    try:
        row = conn.execute("SELECT sealed FROM day_gates WHERE day=?", (d,)).fetchone()
        return bool(row and row["sealed"])
    finally:
        conn.close()


def ensure_day_row(conn, day: str | None = None) -> str:
    d = day or today_str()
    row = conn.execute("SELECT day FROM day_gates WHERE day=?", (d,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO day_gates(day, sealed, sealed_at, note) VALUES (?,?,?,?)",
            (d, 0, None, ""),
        )
    return d


def seal_day(day: str | None = None, note: str = "") -> dict:
    d = day or today_str()
    conn = connect()
    try:
        ensure_day_row(conn, d)
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            "UPDATE day_gates SET sealed=1, sealed_at=?, note=? WHERE day=?",
            (now, note, d),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM day_gates WHERE day=?", (d,)).fetchone())
    finally:
        conn.close()


def refresh_in_flight(conn) -> int:
    # 全局在途水位：始终从挂牌现行行重放，不做独立加减，
    # 因此转挂（只改 store_code、不改在途状态）不会让水位双计或漂移。
    placeholders = ",".join("?" for _ in NOT_IN_FLIGHT)
    n = conn.execute(
        f"SELECT COUNT(*) AS c FROM hang_tickets WHERE status NOT IN ({placeholders})",
        NOT_IN_FLIGHT,
    ).fetchone()["c"]
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    conn.execute(
        """INSERT INTO watermarks(key, value, updated_at) VALUES ('在途票', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (n, now),
    )
    return n

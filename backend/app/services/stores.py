from __future__ import annotations

from app.db import NOT_IN_FLIGHT, connect

# 门店在途/占用口径（口径A：同票改所属店）：
# 一律由 hang_tickets 现行行重放，不存独立计数器。
# 票号不变、只改 store_code 的转挂，会在同一事务里把占用从旧店「挪」到新店，
# 因此不可能出现旧店未减、新店加一，或两边同时把同一衣物算在途。
_NOT_IN = ",".join("?" for _ in NOT_IN_FLIGHT)


def store_occupancy(conn) -> list[dict]:
    """在给定连接（可处于事务内）读取各门店在途占用：票数 + 件数 + 剩余轨位。"""
    rows = conn.execute(
        f"""
        SELECT
          s.code AS code,
          s.name AS name,
          s.rack_count AS rack_count,
          s.active AS active,
          COUNT(CASE WHEN t.status NOT IN ({_NOT_IN}) THEN 1 END) AS in_flight_tickets,
          COALESCE(SUM(CASE WHEN t.status NOT IN ({_NOT_IN})
                            THEN ti.piece_qty ELSE 0 END), 0) AS in_flight_pieces
        FROM stores s
        LEFT JOIN hang_tickets t ON t.store_code = s.code
        LEFT JOIN (
          SELECT ticket_no, SUM(qty) AS piece_qty
          FROM ticket_items GROUP BY ticket_no
        ) ti ON ti.ticket_no = t.ticket_no
        GROUP BY s.id
        ORDER BY s.id
        """,
        (*NOT_IN_FLIGHT, *NOT_IN_FLIGHT),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        # 剩余轨位可能为负（超载），如实呈现，不做掩盖。
        d["free_racks"] = d["rack_count"] - d["in_flight_tickets"]
        result.append(d)
    return result


def list_store_occupancy() -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in store_occupancy(conn)]
    finally:
        conn.close()


def tickets_in_flight_for_store(conn, store_code: str) -> int:
    return conn.execute(
        f"""SELECT COUNT(*) AS c FROM hang_tickets
            WHERE store_code=? AND status NOT IN ({_NOT_IN})""",
        (store_code, *NOT_IN_FLIGHT),
    ).fetchone()["c"]

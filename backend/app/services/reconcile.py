"""跨店转挂对账（口径A：以现行店码 + 两侧占用净变迁为准）。

不变量
------
1. ``hang_tickets.store_code`` 是衣物归属的唯一权威；一张票只属于一个店。
2. 门店占用一律由现行行重放（见 ``services.stores``），不存独立计数器。
3. 回执必须能重放：把某票的回执按 id 依次折回，结果必须等于该票现行店码；
   任何一张回执的 ``from_store`` 必须等于上一次折回后的店码。
4. 全局在途水位是现行行重放的缓存，转挂不改变在途总数。
5. 出现“两侧双在途”（同一票被两个店各计一次）说明存在假转挂，
   按现行店码回滚假回执与假事件，绝不把同一件衣物留在两个店。

本模块负责把已经撕裂的历史数据收口到上述不变量；正常写入路径本身
（见 ``services.transfer``）在单个事务内完成，不依赖此模块兜底。

修复只做“回滚假转挂”，不按回执反向改店码：现行店码是权威，
回执若与店码无法重放，说明该次转挂从未真正生效，连回执与事件一并撤销。
"""
from __future__ import annotations

import sqlite3

from app.db import connect, transaction
from app.services.gate import refresh_in_flight

# 旧实现遗留的独立门店计数器 / 伪造成功标记。它们不是占用权威，
# 同时写入两店还会直接制造“两侧双在途”，必须清除。
_LEGACY_WATERMARK = "店占用:%"
_LEGACY_SETTINGS = ("transfer_surface%",)


def _receipt_rows(conn: sqlite3.Connection, ticket_no: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transfer_receipts WHERE ticket_no=? ORDER BY id",
        (ticket_no,),
    ).fetchall()


def _fold_keep(rows: list[sqlite3.Row], store_code: str) -> int:
    """返回能重放到 ``store_code`` 的最长回执前缀长度。

    前缀必须首尾相接：第 i 张的 from_store == 第 i-1 张折回后的店码。
    断链点之后的回执全部视为假转挂。长度为 0 表示没有任何回执能落在现行店码上。
    """
    if not rows:
        return 0
    expected = rows[0]["from_store"]
    best = 0
    for i, r in enumerate(rows):
        if r["from_store"] != expected:
            break
        expected = r["to_store"]
        if expected == store_code:
            best = i + 1
    return best


def _ticket_store_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["ticket_no"]: r["store_code"]
        for r in conn.execute("SELECT ticket_no, store_code FROM hang_tickets")
    }


def receipt_chain_ok(conn: sqlite3.Connection, ticket_no: str, store_code: str) -> bool:
    """该票全部回执能否首尾相接地重放到 ``store_code``。

    供写入路径在提交前自校验，也是 ``find_tears`` 判定回执链分叉的同一口径。
    """
    rows = _receipt_rows(conn, ticket_no)
    return _fold_keep(rows, store_code) == len(rows)


def _receipts_by_ticket(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """一次取全部回执并按票分组（id 升序），避免逐票 N+1 查询。"""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM transfer_receipts ORDER BY id"):
        grouped.setdefault(r["ticket_no"], []).append(r)
    return grouped


def find_tears(conn: sqlite3.Connection) -> list[dict]:
    """只读探测：返回需要收口的撕裂项；空列表表示各处互证。"""
    tears: list[dict] = []
    for r in conn.execute(
        "SELECT key FROM watermarks WHERE key LIKE ?", (_LEGACY_WATERMARK,)
    ):
        tears.append({"kind": "legacy_store_counter", "key": r["key"]})
    for pattern in _LEGACY_SETTINGS:
        for r in conn.execute(
            "SELECT key FROM settings WHERE key LIKE ?", (pattern,)
        ):
            tears.append({"kind": "legacy_surface_setting", "key": r["key"]})
    grouped = _receipts_by_ticket(conn)
    for ticket_no, store_code in _ticket_store_map(conn).items():
        rows = grouped.get(ticket_no, [])
        keep = _fold_keep(rows, store_code)
        if keep != len(rows):
            tears.append(
                {
                    "kind": "receipt_chain",
                    "ticket_no": ticket_no,
                    "store_code": store_code,
                    "kept": keep,
                    "rolled_back": len(rows) - keep,
                }
            )
    return tears


def _delete_transfer_event(
    conn: sqlite3.Connection, receipt: sqlite3.Row
) -> None:
    """删除被回滚回执对应的迁店事件。

    事件与回执在同一事务内成对写入，且被回滚的一定是回执链尾部，
    因此取该票最后一条同 from→to 的迁店事件即可精确配对，不会误删有效事件。
    """
    prefix = f"跨店转挂：{receipt['from_store']} → {receipt['to_store']}%"
    row = conn.execute(
        """SELECT id FROM process_events
           WHERE ticket_no=? AND note LIKE ?
           ORDER BY id DESC LIMIT 1""",
        (receipt["ticket_no"], prefix),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM process_events WHERE id=?", (row["id"],))


def _alive_in_flight(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM hang_tickets WHERE status NOT IN ('已取','丢失隔离')"
    ).fetchone()["c"]


def _repair(conn: sqlite3.Connection) -> list[dict]:
    repairs: list[dict] = []

    # 1) 清掉独立门店计数器与伪造成功标记：占用只从现行行重放。
    for r in conn.execute(
        "SELECT key FROM watermarks WHERE key LIKE ?", (_LEGACY_WATERMARK,)
    ).fetchall():
        conn.execute("DELETE FROM watermarks WHERE key=?", (r["key"],))
        repairs.append({"kind": "drop_legacy_store_counter", "key": r["key"]})
    for pattern in _LEGACY_SETTINGS:
        for r in conn.execute(
            "SELECT key FROM settings WHERE key LIKE ?", (pattern,)
        ).fetchall():
            conn.execute("DELETE FROM settings WHERE key=?", (r["key"],))
            repairs.append({"kind": "drop_legacy_surface_setting", "key": r["key"]})

    # 2) 回执链无法重放到现行店码的尾部，整段按假转挂回滚（回执 + 事件）。
    #    店码、占用、工序态都不动——现行店码才是权威。
    grouped = _receipts_by_ticket(conn)
    for ticket_no, store_code in _ticket_store_map(conn).items():
        rows = grouped.get(ticket_no, [])
        keep = _fold_keep(rows, store_code)
        for receipt in reversed(rows[keep:]):
            _delete_transfer_event(conn, receipt)
            conn.execute(
                "DELETE FROM transfer_receipts WHERE id=?", (receipt["id"],)
            )
            repairs.append(
                {
                    "kind": "rollback_fake_transfer",
                    "ticket_no": ticket_no,
                    "receipt_id": receipt["id"],
                    "from_store": receipt["from_store"],
                    "to_store": receipt["to_store"],
                    "store_code": store_code,
                }
            )

    # 3) 水位缓存重放，确保与现行行一致。
    refresh_in_flight(conn)
    return repairs


def repair() -> list[dict]:
    """在一个写事务内收口全部撕裂；返回修复明细。"""
    with transaction() as conn:
        return _repair(conn)


def ensure_reconciled() -> list[dict]:
    """廉价探测：只有真的存在撕裂时才开写事务修复。"""
    conn = connect()
    try:
        tears = find_tears(conn)
    finally:
        conn.close()
    if not tears:
        return []
    return repair()


def consistent(conn: sqlite3.Connection | None = None) -> bool:
    """只读：当前各面是否互证（不触发修复）。"""
    own = conn is None
    conn = conn or connect()
    try:
        return not find_tears(conn)
    finally:
        if own:
            conn.close()


def report() -> dict:
    """对账入口：先收口，再报告。"""
    repairs = ensure_reconciled()
    conn = connect()
    try:
        tears = find_tears(conn)
        in_flight = _alive_in_flight(conn)
        wm_row = conn.execute(
            "SELECT value FROM watermarks WHERE key='在途票'"
        ).fetchone()
    finally:
        conn.close()
    return {
        "一致": not tears,
        "修复": repairs,
        "撕裂": tears,
        "在途水位": wm_row["value"] if wm_row else 0,
        "在途重放": in_flight,
        "水位一致": (wm_row["value"] if wm_row else 0) == in_flight,
    }

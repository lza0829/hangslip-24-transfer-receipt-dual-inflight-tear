from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import HTTPException

from app.db import NOT_IN_FLIGHT, connect, transaction
from app.services import reconcile
from app.services.gate import ensure_day_row, is_day_sealed, refresh_in_flight
from app.services.stores import store_occupancy


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def list_receipts(ticket_no: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if ticket_no:
            rows = conn.execute(
                "SELECT * FROM transfer_receipts WHERE ticket_no=? ORDER BY id DESC",
                (ticket_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transfer_receipts ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _result(conn, receipt_id: int, idempotent: bool) -> dict:
    r = conn.execute(
        "SELECT * FROM transfer_receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    t = conn.execute(
        "SELECT ticket_no, store_code, status FROM hang_tickets WHERE ticket_no=?",
        (r["ticket_no"],),
    ).fetchone()
    return {
        "receipt_id": r["id"],
        "ticket_no": r["ticket_no"],
        "from_store": r["from_store"],
        "to_store": r["to_store"],
        "status": t["status"],
        "store_code": t["store_code"],
        "operator": r["operator"],
        "created_at": r["created_at"],
        "idempotency_key": r["idempotency_key"],
        "idempotent": idempotent,
    }


def transfer_ticket(payload: dict) -> dict:
    """跨店转挂（口径A：同票改所属店）。

    票号不变、工序态不变，仅在同一事务内把 hang_tickets.store_code 由旧店改到新店，
    并写事件与转挂回执。门店占用与全局在途水位全部从现行行重放，
    因此两侧占用变更要么同时生效、要么随事务回滚，绝不留半截。
    """
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止跨店转挂")

    ticket_no = (payload.get("ticket_no") or "").strip()
    from_store = (payload.get("from_store") or "").strip()
    to_store = (payload.get("to_store") or "").strip()
    operator = (payload.get("operator") or "店员").strip() or "店员"
    note = (payload.get("note") or "").strip()
    idem = (payload.get("idempotency_key") or "").strip()

    if not ticket_no or not from_store or not to_store:
        raise HTTPException(400, "票号、原门店、目标门店必填")

    with transaction() as conn:
        ensure_day_row(conn)

        # 幂等：同键且同票的重试才回放首次回执；异票同键不在这里命中，
        # 落到回执唯一索引上被拒绝（见下方 IntegrityError 处理）。
        if idem:
            existed = conn.execute(
                "SELECT * FROM transfer_receipts WHERE idempotency_key=? AND ticket_no=?",
                (idem, ticket_no),
            ).fetchone()
            if existed:
                # 回放前也要互证：旧回执的落点必须等于现行店码，
                # 否则它是一次假转挂留下的痕迹，不能当成功回放。
                cur_store = conn.execute(
                    "SELECT store_code FROM hang_tickets WHERE ticket_no=?",
                    (ticket_no,),
                ).fetchone()
                if not cur_store or cur_store["store_code"] != existed["to_store"]:
                    raise HTTPException(409, "幂等键对应的回执与现行店码分叉，拒绝回放")
                return _result(conn, existed["id"], idempotent=True)

        ticket = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not ticket:
            raise HTTPException(404, "挂牌不存在")

        if ticket["status"] in NOT_IN_FLIGHT:
            raise HTTPException(
                409, f"票态「{ticket['status']}」不在途，禁止转挂"
            )

        # 乐观并发守卫：调用方认定的原门店必须等于现行行所属店。
        # BEGIN IMMEDIATE 已让写事务串行；此处再比对，
        # 使并发/陈旧的第二次转挂显式失败，而不是把衣物静默改到第三个店。
        if ticket["store_code"] != from_store:
            raise HTTPException(
                409,
                f"转挂冲突：{ticket_no} 现行所属店为「{ticket['store_code']}」，"
                f"与请求原门店「{from_store}」不符，已拒绝以防重复迁移",
            )

        if to_store == from_store:
            raise HTTPException(400, "目标门店与原门店相同，无需转挂")

        target = conn.execute(
            "SELECT * FROM stores WHERE code=? AND active=1", (to_store,)
        ).fetchone()
        if not target:
            raise HTTPException(400, "目标门店不可用或已停用")

        cur_status = ticket["status"]
        now = _now()
        pieces = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS q FROM ticket_items WHERE ticket_no=?",
            (ticket_no,),
        ).fetchone()["q"]
        # 迁移前的两侧占用快照（同一事务内读，提交后据此核对净变迁）。
        before_by_code = {o["code"]: o for o in store_occupancy(conn)}

        # 唯一的所属店变更点：改现行行。票号、工序态、暗码、件数均不动。
        conn.execute(
            "UPDATE hang_tickets SET store_code=? WHERE ticket_no=? AND store_code=?",
            (to_store, ticket_no, from_store),
        )

        # 工序事件：to_status 与 from_status 相同，表达「工序未被打断」，
        # 迁店信息写在 note，保证工序连续性可从事件流重放。
        move_note = f"跨店转挂：{from_store} → {to_store}" + (f"；{note}" if note else "")
        conn.execute(
            """INSERT INTO process_events
               (ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, cur_status, cur_status, operator, now, move_note),
        )

        try:
            cur = conn.execute(
                "INSERT INTO transfer_receipts"
                "(ticket_no, from_store, to_store, status_snapshot, operator, idempotency_key, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (ticket_no, from_store, to_store, cur_status, operator, idem, now),
            )
        except sqlite3.IntegrityError as e:
            # 并发下幂等键唯一索引冲突：回滚并显式拒绝，不产生第二条回执。
            raise HTTPException(409, "幂等键已被使用，拒绝重复转挂") from e
        # 全局在途水位重放：转挂不改在途状态，水位数值不变，仅刷新时刻。
        refresh_in_flight(conn)
        receipt_id = cur.lastrowid

        # 提交前自洽校验（口径A）：回执必须能由现行店码与两侧净变迁重放。
        # 任何一项不成立都回滚整笔，绝不允许“成功”返回而各面撕裂。
        moved = conn.execute(
            "SELECT store_code FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if moved["store_code"] != to_store:
            raise HTTPException(409, "转挂未生效：现行店码未迁到目标店，已回滚")
        chain = reconcile.receipt_chain_ok(conn, ticket_no, to_store)
        if not chain:
            raise HTTPException(409, "转挂回执无法由现行店码重放，已回滚")
        occupancy = store_occupancy(conn)
        occ = {o["code"]: o for o in occupancy}

        def moved_by(code: str, field: str) -> int:
            return occ[code][field] - before_by_code[code][field]

        if not (
            moved_by(from_store, "in_flight_tickets") == -1
            and moved_by(to_store, "in_flight_tickets") == 1
            and moved_by(from_store, "in_flight_pieces") == -pieces
            and moved_by(to_store, "in_flight_pieces") == pieces
        ):
            # 两侧净变迁必须是“旧店减、新店加”，件数同额；否则整笔回滚。
            raise HTTPException(409, "两侧占用净变迁不自洽，已回滚")
        result = _result(conn, receipt_id, idempotent=False)

    result["occupancy"] = occupancy
    return result


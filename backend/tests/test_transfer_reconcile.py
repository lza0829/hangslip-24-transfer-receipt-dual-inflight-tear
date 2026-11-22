"""撕裂夹具 → 收口（口径A）回归。

直接向库里注入题目列出的四种假转挂撕裂，再通过公开读接口验证：
  1. 回执写已到新店，挂牌详情店码仍旧店
  2. 店码已改，旧店占用未减、新店未加（独立店占用计数器双加）
  3. 半截：店码已改但占用未迁
  4. 并发/历史双在途：同一票被两店各计一次

收口口径：以现行店码 + 两侧占用净变迁为准。回执无法由店码折回的，
按假转挂回滚回执与事件；独立店占用计数器直接删除；占用一律重放。
失败（回滚）后不得留双店占用。

运行：HANGSLIP_DB 指向临时库，python -I test_transfer_reconcile.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="hangslip-recon-")
os.environ["HANGSLIP_DB"] = os.path.join(_tmp, "recon.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import reconcile  # noqa: E402
from app.services import tickets as ticket_svc  # noqa: E402
from app.services import transfer as transfer_svc  # noqa: E402
from app.services.stores import list_store_occupancy  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def occ() -> dict[str, dict]:
    return {o["code"]: o for o in list_store_occupancy()}


def get(no: str) -> dict:
    return ticket_svc.get_ticket(no)


def open_ticket(no: str, store: str, qty: int = 1) -> None:
    ticket_svc.create_ticket(
        {
            "ticket_no": no,
            "store_code": store,
            "pickup_code": f"码-{no}",
            "customer_name": "夹具客",
            "due_date": "2026-12-31",
            "items": [{"garment": "外套", "qty": qty, "remark": ""}],
            "operator": "夹具",
        }
    )


def inject_receipt(ticket_no: str, from_store: str, to_store: str) -> None:
    """只写回执流水，不改店码：制造“回执新店、详情仍旧店”。"""
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO transfer_receipts
               (ticket_no, from_store, to_store, status_snapshot, operator, idempotency_key, created_at)
               VALUES (?,?,?,?,?,'','2026-11-22 00:00:00')""",
            (ticket_no, from_store, to_store, "待取", "假转挂"),
        )
        conn.commit()
    finally:
        conn.close()


def inject_double_counter(ticket_no: str, a: str, b: str) -> None:
    """旧实现遗留：给两店各加一个独立占用计数器，制造“两侧双在途”。"""
    conn = connect()
    try:
        for store in (a, b):
            conn.execute(
                "INSERT INTO watermarks(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"店占用:{store}", 1, "2026-11-22 00:00:00"),
            )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('transfer_surface_ok','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    seed_if_empty()
    A, B, C = "店-河西", "店-城东", "店-南苑"

    print("\n[夹具1] 回执写已到新店，挂牌详情店码仍旧店")
    no = "挂-R001"
    open_ticket(no, A, qty=2)
    before1 = occ()
    inject_receipt(no, A, B)
    check("撕裂：回执新店 vs 详情仍旧店",
          transfer_svc.list_receipts(no)[0]["to_store"] == B and get(no)["store_code"] == A)
    conn = connect()
    try:
        detected = any(t["kind"] == "receipt_chain" for t in reconcile.find_tears(conn))
    finally:
        conn.close()
    check("探测出回执链分叉", detected)
    rep = reconcile.report()
    check("收口报告一致", rep["一致"] is True, str(rep["撕裂"]))
    check("假回执已回滚", len(transfer_svc.list_receipts(no)) == 0)
    check("店码仍为旧店（现行行为准）", get(no)["store_code"] == A)
    check("两侧占用重放无变化", occ()[A]["in_flight_pieces"] == before1[A]["in_flight_pieces"])

    print("\n[夹具2] 店码已改，旧店占用未减、新店未加（双加计数器）")
    no2 = "挂-R002"
    open_ticket(no2, A, qty=3)
    before = occ()
    # 真实改码 + 真实回执，但再伪造两侧独立计数器 + 成功标记
    transfer_svc.transfer_ticket({"ticket_no": no2, "from_store": A, "to_store": B, "operator": "x"})
    inject_double_counter(no2, A, B)
    check("旧店票数已减", occ()[A]["in_flight_tickets"] == before[A]["in_flight_tickets"] - 1)
    check("新店票数已加", occ()[B]["in_flight_tickets"] == before[B]["in_flight_tickets"] + 1)
    rep = reconcile.report()
    conn = connect()
    try:
        legacy = conn.execute("SELECT COUNT(*) AS c FROM watermarks WHERE key LIKE '店占用:%'").fetchone()["c"]
        flag = conn.execute("SELECT COUNT(*) AS c FROM settings WHERE key LIKE 'transfer_surface%'").fetchone()["c"]
    finally:
        conn.close()
    check("独立店计数器已清除", legacy == 0)
    check("伪造成功标记已清除", flag == 0)
    check("收口后一致", rep["一致"] is True, str(rep["撕裂"]))
    life = occ()
    check("该票只在新店计一次",
          life[B]["in_flight_pieces"] == before[B]["in_flight_pieces"] + 3
          and life[A]["in_flight_pieces"] == before[A]["in_flight_pieces"] - 3)

    print("\n[夹具3] 半截：店码已改但占用未迁（无回执、无事件）")
    no3 = "挂-R003"
    open_ticket(no3, A, qty=1)
    conn = connect()
    try:
        conn.execute("UPDATE hang_tickets SET store_code=? WHERE ticket_no=?", (C, no3))
        conn.commit()
    finally:
        conn.close()
    # 占用由现行行重放，改码即迁占用；此夹具验证“无回执的改码”不会伪造转挂成功。
    check("改码后占用随现行行迁到 C", occ()[C]["in_flight_tickets"] >= 1)
    rep = reconcile.report()
    check("无回执即无假转挂可回滚（一致）", rep["一致"] is True, str(rep["撕裂"]))
    check("该票回执为 0", len(transfer_svc.list_receipts(no3)) == 0)

    print("\n[夹具4] 双在途：同一票被两店各计一次")
    no4 = "挂-R004"
    open_ticket(no4, A, qty=1)
    inject_double_counter(no4, A, C)
    conn = connect()
    try:
        counters = conn.execute(
            "SELECT COUNT(*) AS c FROM watermarks WHERE key LIKE '店占用:%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    check("伪造出两店独立计数", counters == 2)
    before4 = occ()
    rep = reconcile.report()
    hits = sum(
        1
        for store in (A, B, C)
        for t in ticket_svc.list_tickets(store_code=store)
        if t["ticket_no"] == no4
    )
    check("收口后该票全库只计一次", hits == 1, str(hits))
    check("水位与重放一致", rep["水位一致"] is True and rep["一致"] is True, str(rep))

    print("\n[互证] 转挂 API / 详情店码 / 甲乙衣架 / 水位 / 事件 五面一致")
    no5 = "挂-R005"
    open_ticket(no5, A, qty=2)
    pre = occ()
    res = transfer_svc.transfer_ticket(
        {"ticket_no": no5, "from_store": A, "to_store": B, "operator": "互证", "idempotency_key": "recon-5"}
    )
    detail = get(no5)
    post = occ()
    ev = detail["events"][-1]
    receipts = transfer_svc.list_receipts(no5)
    wm = connect()
    try:
        water = wm.execute("SELECT value FROM watermarks WHERE key='在途票'").fetchone()["value"]
        live = wm.execute(
            "SELECT COUNT(*) AS c FROM hang_tickets WHERE status NOT IN ('已取','丢失隔离')"
        ).fetchone()["c"]
    finally:
        wm.close()
    check("回执新店 == 详情店码 == 响应店码",
          res["to_store"] == detail["store_code"] == B and res["store_code"] == B)
    check("甲店减、乙店加（票数）",
          post[A]["in_flight_tickets"] == pre[A]["in_flight_tickets"] - 1
          and post[B]["in_flight_tickets"] == pre[B]["in_flight_tickets"] + 1)
    check("甲店减、乙店加（件数）",
          post[A]["in_flight_pieces"] == pre[A]["in_flight_pieces"] - 2
          and post[B]["in_flight_pieces"] == pre[B]["in_flight_pieces"] + 2)
    check("两侧在途总数不变", sum(o["in_flight_tickets"] for o in pre.values())
          == sum(o["in_flight_tickets"] for o in post.values()))
    check("水位 == 重放", water == live)
    check("事件与回执同指向", "跨店转挂" in ev["note"] and ev["from_status"] == ev["to_status"]
          and len(receipts) == 1 and receipts[0]["to_store"] == B)

    print("\n[回滚] 假转挂失败后不得留双店占用")
    no6 = "挂-R006"
    open_ticket(no6, A, qty=1)
    inject_receipt(no6, A, B)
    snap = occ()
    reconcile.report()
    after6 = occ()
    check("回滚假回执后占用未变（未双计）",
          all(after6[s]["in_flight_tickets"] == snap[s]["in_flight_tickets"] for s in (A, B, C)))
    check("该票仍只在原店计一次",
          sum(1 for s in (A, B, C) for t in ticket_svc.list_tickets(store_code=s)
              if t["ticket_no"] == no6) == 1)
    conn = connect()
    try:
        legacy_left = conn.execute(
            "SELECT COUNT(*) AS c FROM watermarks WHERE key LIKE '店占用:%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    check("无残留店占用计数器", legacy_left == 0)

    print("\n[夹具5] 链式假转挂：真 A→B 之后，假 B→C 又把店码退回 B")
    no7 = "挂-R007"
    open_ticket(no7, A, qty=1)
    transfer_svc.transfer_ticket({"ticket_no": no7, "from_store": A, "to_store": B, "operator": "真"})
    pre7 = get(no7)
    # 复刻旧 after_transfer：单事务内写 B→C 回执+事件，随后又把店码改回 B。
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,'跨店转挂：店-城东 → 店-南苑')""",
            (no7, pre7["status"], pre7["status"], "假", "2026-11-22 00:00:00"),
        )
        conn.execute(
            """INSERT INTO transfer_receipts
               (ticket_no, from_store, to_store, status_snapshot, operator, idempotency_key, created_at)
               VALUES (?,?,?,?,'假','','2026-11-22 00:00:00')""",
            (no7, B, C, pre7["status"]),
        )
        conn.commit()
    finally:
        conn.close()
    check("撕裂：两条回执链到 C，店码仍 B", len(transfer_svc.list_receipts(no7)) == 2)
    rep = reconcile.report()
    kept = transfer_svc.list_receipts(no7)
    evs = get(no7)["events"]
    check("只回滚假 B→C，保留真 A→B",
          len(kept) == 1 and kept[0]["from_store"] == A and kept[0]["to_store"] == B)
    check("假迁店事件同步删除、真事件保留",
          sum(1 for e in evs if "跨店转挂" in e["note"]) == 1
          and "店-城东 → 店-南苑" not in evs[-1]["note"], str(evs[-1]))
    check("店码仍为 B（现行行为准）", get(no7)["store_code"] == B)
    check("收口后一致", rep["一致"] is True, str(rep["撕裂"]))

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

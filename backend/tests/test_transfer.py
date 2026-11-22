"""跨店转挂（口径A：同票改所属店）端到端验证。

运行：HANGSLIP_DB 指向临时库，直接 python -I test_transfer.py
覆盖：
  1. 票号/工序不变、所属店迁移、事件与回执留痕
  2. 两侧占用从现行行重放、件数正确、全局水位不变且与重放一致
  3. 失败回滚（目标店停用、原门店不符、同店、不在途票）不留半截
  4. 并发转挂不得双计：同票仅一次成功，串行迁移，无重复回执
  5. 幂等键：同键重试回放、异票同键拒绝
  6. 日闸结案后禁止转挂
"""
from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前指定临时库
_tmp = tempfile.mkdtemp(prefix="hangslip-test-")
os.environ["HANGSLIP_DB"] = os.path.join(_tmp, "test.db")

# -I 隔离模式忽略 PYTHONPATH，这里显式把 backend 根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.db import connect  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import tickets as ticket_svc  # noqa: E402
from app.services import transfer as transfer_svc  # noqa: E402
from app.services.gate import seal_day  # noqa: E402
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


def expect_http(name: str, status: int, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        check(name, False, f"应抛 {status} 但未抛")
        return None
    except HTTPException as e:
        check(name, e.status_code == status, f"得 {e.status_code} 想要 {status}：{e.detail}")
        return e


def occ_map() -> dict[str, dict]:
    return {o["code"]: o for o in list_store_occupancy()}


def get(no: str) -> dict:
    return ticket_svc.get_ticket(no)


def open_ticket(no: str, store: str, qty: int = 1, garment: str = "衬衣") -> None:
    ticket_svc.create_ticket(
        {
            "ticket_no": no,
            "store_code": store,
            "pickup_code": f"码-{no}",
            "customer_name": "测试客",
            "due_date": "2026-12-31",
            "items": [{"garment": garment, "qty": qty, "remark": ""}],
            "operator": "测试",
        }
    )


def main() -> int:
    seed_if_empty()
    A, B, C = "店-河西", "店-城东", "店-南苑"

    print("\n[1] 基础转挂：票号/工序不变，所属店迁移，事件与回执")
    no = "挂-T001"
    open_ticket(no, A, qty=2)
    ticket_svc.advance(no, "水洗", operator="工序员")
    before = get(no)
    before_occ = occ_map()
    res = transfer_svc.transfer_ticket(
        {"ticket_no": no, "from_store": A, "to_store": B, "operator": "转挂员",
         "note": "调店", "idempotency_key": "k-001"}
    )
    after = get(no)
    check("票号不变", after["ticket_no"] == no)
    check("工序态不变（连续性）", after["status"] == "水洗", after["status"])
    check("所属店已迁到目标店", after["store_code"] == B, after["store_code"])
    check("回执返回原/目标店", (res["from_store"], res["to_store"]) == (A, B))
    check("回执非幂等首次", res["idempotent"] is False)
    evs = after["events"]
    last = evs[-1]
    check("事件工序 from==to（未被打断）",
          last["from_status"] == last["to_status"] == "水洗", str(last))
    check("事件含迁店留痕", "跨店转挂" in last["note"] and A in last["note"] and B in last["note"])
    receipts = transfer_svc.list_receipts(no)
    check("回执表恰好一条", len(receipts) == 1 and receipts[0]["from_store"] == A)

    print("\n[2] 两侧占用重放、件数、全局水位一致")
    after_occ = occ_map()
    check("旧店票数 -1",
          after_occ[A]["in_flight_tickets"] == before_occ[A]["in_flight_tickets"] - 1)
    check("新店票数 +1",
          after_occ[B]["in_flight_tickets"] == before_occ[B]["in_flight_tickets"] + 1)
    check("旧店件数 -2",
          after_occ[A]["in_flight_pieces"] == before_occ[A]["in_flight_pieces"] - 2)
    check("新店件数 +2",
          after_occ[B]["in_flight_pieces"] == before_occ[B]["in_flight_pieces"] + 2)
    total_before = sum(o["in_flight_tickets"] for o in before_occ.values())
    total_after = sum(o["in_flight_tickets"] for o in after_occ.values())
    check("全部门店在途总数不变（无双在途）", total_before == total_after)
    conn = connect()
    try:
        wm = conn.execute("SELECT value FROM watermarks WHERE key='在途票'").fetchone()["value"]
        live = conn.execute(
            "SELECT COUNT(*) AS c FROM hang_tickets WHERE status NOT IN ('已取','丢失隔离')"
        ).fetchone()["c"]
    finally:
        conn.close()
    check("全局水位 == 现行行重放", wm == live == total_after, f"wm={wm} live={live}")
    check("剩余轨位随占用变化",
          after_occ[B]["free_racks"] == after_occ[B]["rack_count"] - after_occ[B]["in_flight_tickets"])

    print("\n[3] 失败回滚：不留半截（门店码/占用/回执都不变）")
    no2 = "挂-T002"
    open_ticket(no2, A, qty=1)
    base = get(no2)
    base_occ = occ_map()
    base_receipts = len(transfer_svc.list_receipts())

    expect_http("停用目标店 -> 400", 400, transfer_svc.transfer_ticket,
                {"ticket_no": no2, "from_store": A, "to_store": "店-不存在", "operator": "x"})
    expect_http("同店转挂 -> 400", 400, transfer_svc.transfer_ticket,
                {"ticket_no": no2, "from_store": A, "to_store": A, "operator": "x"})
    expect_http("原门店不符(陈旧) -> 409", 409, transfer_svc.transfer_ticket,
                {"ticket_no": no2, "from_store": C, "to_store": B, "operator": "x"})
    # 已取票不在途
    no3 = "挂-T003"
    open_ticket(no3, A)
    ticket_svc.advance(no3, "待取", operator="x")
    ticket_svc.pickup(no3, f"码-{no3}", operator="x")
    expect_http("已取票转挂 -> 409", 409, transfer_svc.transfer_ticket,
                {"ticket_no": no3, "from_store": A, "to_store": B, "operator": "x"})

    check("失败后票籍门店码未改", get(no2)["store_code"] == base["store_code"] == A)
    check("失败后工序态未改", get(no2)["status"] == base["status"])
    rollback_occ = occ_map()
    check("失败后各店占用完全不变",
          all(rollback_occ[s]["in_flight_tickets"] == base_occ[s]["in_flight_tickets"]
              for s in (A, B, C)))
    check("失败不写任何回执", len(transfer_svc.list_receipts()) == base_receipts)

    print("\n[4] 并发转挂不得双计（同票多线程，仅一次成功，串行迁移）")
    no4 = "挂-T004"
    open_ticket(no4, A)
    # 两个线程都声称从 A 出发，分别想去 B / C
    payloads = [
        {"ticket_no": no4, "from_store": A, "to_store": B, "operator": "并发1"},
        {"ticket_no": no4, "from_store": A, "to_store": C, "operator": "并发2"},
        {"ticket_no": no4, "from_store": A, "to_store": B, "operator": "并发3"},
    ]
    outcomes: list[str] = []

    def run(p):
        try:
            transfer_svc.transfer_ticket(p)
            return "ok"
        except HTTPException as e:
            return f"err{e.status_code}"

    with ThreadPoolExecutor(max_workers=3) as ex:
        outcomes = list(ex.map(run, payloads))
    ok_n = outcomes.count("ok")
    check("并发下仅 1 次成功", ok_n == 1, str(outcomes))
    check("其余均被 409 拒绝", outcomes.count("err409") == 2, str(outcomes))
    final4 = get(no4)
    check("票只属于一个店（无双占）", final4["store_code"] in (B, C), final4["store_code"])
    check("该票回执仅 1 条（无双计）", len(transfer_svc.list_receipts(no4)) == 1)
    occ4 = occ_map()
    belongs = sum(1 for s in (A, B, C)
                  if any(t["ticket_no"] == no4 for t in _tickets_of(s)))
    check("该票在全部门店占用中只被计一次", belongs == 1, str(belongs))
    # 串行迁移：再从落点迁到第三个店应成功，证明第一次提交后状态自洽
    cur_store = final4["store_code"]
    other = C if cur_store == B else B
    r2 = transfer_svc.transfer_ticket(
        {"ticket_no": no4, "from_store": cur_store, "to_store": other, "operator": "串行"})
    check("提交后可继续串行转挂", get(no4)["store_code"] == other)
    check("串行转挂累计 2 条回执", len(transfer_svc.list_receipts(no4)) == 2)

    print("\n[5] 幂等键")
    first = transfer_svc.transfer_ticket(
        {"ticket_no": no, "from_store": B, "to_store": C, "idempotency_key": "k-001"})
    # 注意 k-001 已在第1步被 no 使用，这里应直接回放旧回执而非迁移
    check("同键重试回放且不再迁移", first["idempotent"] is True and get(no)["store_code"] == B)
    check("同键不新增回执", len(transfer_svc.list_receipts(no)) == 1)
    no5 = "挂-T005"
    open_ticket(no5, A)
    expect_http("异票复用幂等键 -> 409", 409, transfer_svc.transfer_ticket,
                {"ticket_no": no5, "from_store": A, "to_store": B, "idempotency_key": "k-001"})
    check("被拒票籍未动", get(no5)["store_code"] == A)

    print("\n[6] 日闸结案阻断 + 总览联动")
    seal_day(note="结案测试")
    expect_http("结案后转挂 -> 409", 409, transfer_svc.transfer_ticket,
                {"ticket_no": no5, "from_store": A, "to_store": B, "operator": "x"})

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


def _tickets_of(store: str) -> list[dict]:
    return ticket_svc.list_tickets(store_code=store)


if __name__ == "__main__":
    raise SystemExit(main())

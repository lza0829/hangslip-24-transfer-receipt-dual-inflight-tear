"""HTTP 层冒烟：真实 ASGI 路由 + 临时库，验证转挂端到端与总览/门店联动。"""
from __future__ import annotations

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="hangslip-http-")
os.environ["HANGSLIP_DB"] = os.path.join(_tmp, "http.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

c = TestClient(app)
fails = 0


def expect(label, cond, extra=""):
    global fails
    print(("  ✓ " if cond else "  ✗ ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        fails += 1


# 开两张在途票到河西
for no in ("挂-H1", "挂-H2"):
    r = c.post("/api/挂牌", json={
        "ticket_no": no, "store_code": "店-河西", "pickup_code": f"m-{no}",
        "customer_name": "客", "items": [{"garment": "外套", "qty": 3}]})
    expect(f"开票 {no} -> 200", r.status_code == 200, r.text)

r = c.get("/api/门店/占用")
occ0 = {o["code"]: o for o in r.json()}
print("\n转挂前门店占用：", {k: (v["in_flight_tickets"], v["in_flight_pieces"]) for k, v in occ0.items()})

# 正常转挂 H1 河西 -> 城东
r = c.post("/api/转挂", json={
    "ticket_no": "挂-H1", "from_store": "店-河西", "to_store": "店-城东",
    "operator": "店长", "note": "HTTP 转挂", "idempotency_key": "http-1"})
expect("转挂 -> 200", r.status_code == 200, r.text)
body = r.json()
expect("回执含两侧门店与票号",
       body["from_store"] == "店-河西" and body["to_store"] == "店-城东"
       and body["ticket_no"] == "挂-H1" and body["status"] == "已收")
expect("响应内联占用自洽",
       {o["code"]: o["in_flight_tickets"] for o in body["occupancy"]})

# 幂等重试
r2 = c.post("/api/转挂", json={
    "ticket_no": "挂-H1", "from_store": "店-河西", "to_store": "店-城东",
    "idempotency_key": "http-1"})
expect("同键重试 200 且标记 idempotent", r2.status_code == 200 and r2.json()["idempotent"] is True)

# 陈旧原门店 -> 409（H1 已在城东）
r = c.post("/api/转挂", json={"ticket_no": "挂-H1", "from_store": "店-河西", "to_store": "店-南苑"})
expect("陈旧原门店 -> 409", r.status_code == 409, r.text)

# 同店 -> 400
r = c.post("/api/转挂", json={"ticket_no": "挂-H2", "from_store": "店-河西", "to_store": "店-河西"})
expect("同店 -> 400", r.status_code == 400, r.text)

# 占用视图校验
r = c.get("/api/门店/占用")
occ1 = {o["code"]: o for o in r.json()}
print("转挂后门店占用：", {k: (v["in_flight_tickets"], v["in_flight_pieces"]) for k, v in occ1.items()})
expect("河西票数 -1", occ1["店-河西"]["in_flight_tickets"] == occ0["店-河西"]["in_flight_tickets"] - 1)
expect("城东票数 +1", occ1["店-城东"]["in_flight_tickets"] == occ0["店-城东"]["in_flight_tickets"] + 1)
expect("河西件数 -3", occ1["店-河西"]["in_flight_pieces"] == occ0["店-河西"]["in_flight_pieces"] - 3)
expect("城东件数 +3", occ1["店-城东"]["in_flight_pieces"] == occ0["店-城东"]["in_flight_pieces"] + 3)

# 总览联动与水位自洽
d = c.get("/api/总览").json()
expect("总览含门店在途", len(d["门店在途"]) >= 3)
expect("水位与重放一致", d["水位一致"] is True, str(d))
expect("在途水位数值未因转挂变化", d["在途水位"] == d["在途重放"])

# 票详情含转挂事件（工序 from==to）
t = c.get("/api/挂牌/挂-H1").json()
e = t["events"][-1]
expect("票籍所属店已更新", t["store_code"] == "店-城东")
expect("详情末条事件工序连续", e["from_status"] == e["to_status"] == "已收" and "跨店转挂" in e["note"])

# 回执列表
rc = c.get("/api/转挂/回执", params={"ticket_no": "挂-H1"}).json()
expect("回执查询仅 1 条（幂等未重复）", len(rc) == 1 and rc[0]["to_store"] == "店-城东")

print(f"\nHTTP 冒烟结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
raise SystemExit(1 if fails else 0)

from __future__ import annotations

from fastapi import APIRouter

from app.db import connect
from app.services import reconcile
from app.services.gate import today_str
from app.services.stores import store_occupancy

router = APIRouter(tags=["总览"])


def _dashboard(repairs: list[dict]) -> dict:
    conn = connect()
    try:
        def cnt(status: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM hang_tickets WHERE status=?", (status,)
            ).fetchone()["c"]

        day = today_str()
        gate = conn.execute("SELECT * FROM day_gates WHERE day=?", (day,)).fetchone()
        wm = conn.execute(
            "SELECT value FROM watermarks WHERE key='在途票'"
        ).fetchone()
        # 自洽校验：水位（重算缓存）必须等于现行行重放的在途总数，否则立即暴露漂移。
        occupancy = store_occupancy(conn)
        replay_total = sum(o["in_flight_tickets"] for o in occupancy)
        # 回执链可重放性：任何票的回执都不能与现行店码分叉。
        tears = reconcile.find_tears(conn)
        return {
            "今日": day,
            "日闸": "已结案" if gate and gate["sealed"] else "开放",
            "在途水位": wm["value"] if wm else 0,
            "在途重放": replay_total,
            "水位一致": (wm["value"] if wm else 0) == replay_total,
            "门店在途": occupancy,
            "转挂一致": not tears,
            "转挂撕裂": tears,
            "转挂收口": repairs,
            "已收": cnt("已收"),
            "水洗": cnt("水洗"),
            "烘干": cnt("烘干"),
            "熨烫": cnt("熨烫"),
            "待取": cnt("待取"),
            "已取": cnt("已取"),
            "超期隔离": cnt("超期隔离"),
            "丢失隔离": cnt("丢失隔离"),
        }
    finally:
        conn.close()


@router.get("/总览")
def dashboard():
    # 读前先收口历史撕裂：总览、门店页、详情页因此永远读同一份权威。
    repairs = reconcile.ensure_reconciled()
    return _dashboard(repairs)


@router.get("/对账")
def reconcile_report():
    return reconcile.report()

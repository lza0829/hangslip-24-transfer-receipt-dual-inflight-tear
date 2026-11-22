from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.services import reconcile
from app.services.stores import list_store_occupancy

router = APIRouter(tags=["门店"])


class StoreIn(BaseModel):
    code: str
    name: str
    rack_count: int = Field(ge=1, default=2)


@router.get("/门店")
def list_stores():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM stores ORDER BY id").fetchall()]
    finally:
        conn.close()


@router.get("/门店/占用")
def store_occupancy_view():
    # 门店页占用：先收口假转挂，再由挂牌现行行重放（票数/件数/剩余轨位）。
    reconcile.ensure_reconciled()
    return list_store_occupancy()


@router.post("/门店")
def create_store(body: StoreIn):
    conn = connect()
    try:
        try:
            conn.execute(
                "INSERT INTO stores(code, name, rack_count, active) VALUES (?,?,?,1)",
                (body.code.strip(), body.name.strip(), body.rack_count),
            )
            conn.commit()
        except Exception as e:
            raise HTTPException(409, f"门店写入失败：{e}") from e
        return dict(
            conn.execute("SELECT * FROM stores WHERE code=?", (body.code.strip(),)).fetchone()
        )
    finally:
        conn.close()

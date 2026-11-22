from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services import transfer as svc

router = APIRouter(tags=["跨店转挂"])


class TransferIn(BaseModel):
    ticket_no: str
    from_store: str
    to_store: str
    operator: str = "店员"
    note: str = ""
    idempotency_key: str = ""


@router.post("/转挂")
def transfer(body: TransferIn):
    return svc.transfer_ticket(body.model_dump())


@router.get("/转挂/回执")
def list_receipts(ticket_no: str | None = Query(None)):
    return svc.list_receipts(ticket_no)

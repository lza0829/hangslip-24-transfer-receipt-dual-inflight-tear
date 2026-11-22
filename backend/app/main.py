from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import PROJECT_NAME
from app.routers import dashboard, gates, quarantine, settings, stores, tickets, transfer
from app.seed import seed_if_empty
from app.services import reconcile

seed_if_empty()
# 启动即收口历史假转挂：回执链与现行店码分叉、两侧双在途等，一律回滚假回执。
reconcile.ensure_reconciled()

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(dashboard.router, prefix="/api")
app.include_router(stores.router, prefix="/api")
app.include_router(tickets.router, prefix="/api")
app.include_router(transfer.router, prefix="/api")
app.include_router(gates.router, prefix="/api")
app.include_router(quarantine.router, prefix="/api")
app.include_router(settings.router, prefix="/api")


@app.get("/api/健康")
def health():
    return {"产品": PROJECT_NAME, "状态": "正常"}

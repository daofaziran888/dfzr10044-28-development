"""湖表工坊 · 湖表分区演化与小文件归并平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8001
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.lake_seed import seed_lake_store
from app.routers import PREFIXED_ROUTERS, ROUTERS
from app.store import store

# 启动即写入列画像所需的样本批次（幂等），保证筛选入口开箱可用。
seed_lake_store()

app = FastAPI(title="湖表工坊 · 湖表分区演化与小文件归并平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in PREFIXED_ROUTERS:
    app.include_router(module.router)
for module in ROUTERS:
    app.include_router(module.router)

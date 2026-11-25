"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import profiles, resource

ROUTERS = [resource]

# 具体子路径路由必须排在通用资源路由之前注册，避免 /api/{module}/{entry_id}
# 抢先匹配 /api/column_stat/profiles 这类定名子路径。
PREFIXED_ROUTERS = [profiles]

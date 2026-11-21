"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import business, resource, schema_impact

# 固定前缀的业务路由要先于 `/api/{module}` 注册，
# 否则 `/api/schema/write-impact` 会被资源路由当成 schema 模块的 entry_id。
ROUTERS = [business, schema_impact, resource]

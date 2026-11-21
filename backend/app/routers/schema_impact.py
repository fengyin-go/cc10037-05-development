"""Schema 写入影响与迁移演练接口。

挂载在 `/api/schema/*`：先注册本路由，再注册通用资源路由 `/api/{module}`，
否则 `/api/schema/write-impact` 会被当成模块 schema 的 entry_id 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.services import service_for

router = APIRouter(prefix="/api/schema", tags=["Schema写入影响"])


def _schema_service():
    return service_for("schema_evolution")


@router.get("/write-impact")
def schema_write_impact(
    table: str | None = None,
    field: str | None = None,
    new_field: str | None = None,
    change_id: int | None = None,
) -> dict[str, Any]:
    """金额列写入影响明细：字段差异 + 表血缘上的实际写入点（直传/派生）。"""
    return _schema_service().analyze_write_impact(
        table_name=table, field_name=field, new_field=new_field, change_id=change_id)


@router.get("/write-sources")
def list_schema_write_sources(table: str | None = None) -> dict[str, Any]:
    """登记在案的金额列写入来源。"""
    sources = _schema_service().list_write_sources(table)
    return {"items": sources, "total": len(sources)}


@router.post("/write-sources")
def register_schema_write_sources(body: dict[str, Any]) -> dict[str, Any]:
    """同时登记金额列的直传边和派生边。

    body: {"sources": [{edge_id, target_table, old_field, new_field, mode, ...}]}
    """
    service = _schema_service()
    sources = body.get("sources") or [body]
    registered = service.register_write_sources(sources)
    return {"ok": True, "message": "写入来源已登记", "items": registered}


@router.get("/drills")
def list_schema_drills() -> dict[str, Any]:
    """演练列表。"""
    drills = _schema_service().list_drills()
    return {"items": drills, "total": len(drills)}


@router.post("/drills")
def create_schema_drill(body: dict[str, Any]) -> dict[str, Any]:
    """读取写入影响明细，编制迁移演练并生成迁移映射。"""
    return _schema_service().create_drill(body)


@router.get("/drills/{drill_id}")
def get_schema_drill(drill_id: int) -> dict[str, Any]:
    drill = _schema_service().get_drill(drill_id)
    if drill is None:
        raise HTTPException(status_code=404, detail=f"演练 {drill_id} 不存在")
    return drill


@router.get("/drills/{drill_id}/paths")
def lookup_schema_drill_paths(drill_id: int) -> dict[str, Any]:
    """回查本次演练关联的字段路径、Schema 版本和映射状态。"""
    result = _schema_service().lookup_drill_paths(drill_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"演练 {drill_id} 不存在")
    return result


@router.patch("/drills/{drill_id}/mapping")
def update_schema_drill_mapping(drill_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """补上应收额映射或核实转换边。

    body: {edge_id, new_field, conversion_basis, conversion_factor, verified, expression}
    """
    drill, message = _schema_service().update_drill_mapping(drill_id, body)
    if drill is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "drill": drill}


@router.patch("/drills/{drill_id}/rename")
def change_schema_drill_rename(drill_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """改名口径变更：重算表达式和迁移数据，保留未改动列。"""
    drill, message = _schema_service().change_drill_rename(drill_id, body)
    if drill is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "drill": drill}


@router.post("/drills/{drill_id}/trial")
def run_schema_drill_trial(drill_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """用候选数据试写，返回输入就绪列、试写值和结算任务阻断结论。"""
    drill, message = _schema_service().run_trial(drill_id, body or {})
    if drill is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "drill": drill, "trial": drill.get("trial")}


@router.post("/drills/{drill_id}/submit")
def submit_schema_drill(drill_id: int) -> dict[str, Any]:
    """提交迁移演练；仍有待核实边时拒绝提交。"""
    drill, message = _schema_service().submit_drill(drill_id)
    if drill is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": drill.get("status") == "已提交", "message": message, "drill": drill}

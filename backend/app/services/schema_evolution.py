"""Schema变更领域服务：管理表字段变更、兼容性评估和影响分析。

上游：异常捕获字段级异常 → schema_evolution 记录变更
消费：quality_rule 根据变更结果调整校验规则
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from app.engine.schema_write_impact import (
    DEFAULT_CANDIDATE_ROWS,
    DEFAULT_COLUMN_DEFAULTS,
    DEFAULT_NEW_COLUMNS,
    DEFAULT_OLD_COLUMNS,
    DEFAULT_RENAME,
    DEFAULT_TARGET_TABLE,
    DEFAULT_WRITE_SOURCES,
    ColumnDiff,
    WriteSource,
    build_trial,
    build_write_path,
    diff_columns,
    find_rename,
    recalculate_expression,
    substitute_field,
    trace_lineage_chain,
    column_name,
)
from app.services.base import BaseModuleService
from app.store import store

class SchemaEvolutionService(BaseModuleService):
    MODULE = "schema_evolution"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["变更时间", "目标表", "变更类型", "变更详情", "兼容性评估", "影响管线", "审批状态"]
    REQUIRED_FIELDS = ["变更时间", "目标表", "变更类型"]
    STATUS_ORDER = ["待评估", "兼容", "不兼容", "已执行", "已回滚"]
    ACTION_RULES = {"评估兼容": "待评估", "执行变更": "兼容", "回滚变更": "不兼容"}
    NEGATIVE_ACTIONS = ["回滚变更"]

    def query_by_table(self, table_name: str) -> list[dict[str, Any]]:
        """按目标表查询 Schema 变更记录。

        从 store 中筛选指定表的字段变更历史，
        返回变更列表供数据治理和审计消费。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if table_name.casefold() in str(row.get("目标表", "")).casefold():
                result.append(dict(row))
        return result

    def evaluate_compatibility(self, change_id: int) -> dict[str, Any]:
        """评估 Schema 变更的兼容性。

        分析变更类型和影响范围，根据变更类型判断
        是向前兼容还是向后不兼容的变更。
        """
        entry = store.find(self.MODULE, change_id)
        if entry is None:
            return {"error": f"变更记录 {change_id} 不存在"}

        change_type = str(entry.get("变更类型", ""))
        change_detail = str(entry.get("变更详情", ""))
        table_name = str(entry.get("目标表", ""))

        is_compatible = True
        risk_level = "低"

        compatible_ops = {"增加字段", "新增列", "添加字段", "ADD COLUMN"}
        incompatible_ops = {"删除字段", "重命名字段", "修改类型", "删除列",
                             "DROP COLUMN", "MODIFY COLUMN", "CHANGE COLUMN"}

        if change_type in compatible_ops:
            is_compatible = True
            risk_level = "低"
        elif change_type in incompatible_ops:
            is_compatible = False
            risk_level = "高"
        else:
            is_compatible = True
            risk_level = "中"

        affected_pipelines = str(entry.get("影响管线", ""))
        pipeline_list = [
            p.strip() for p in affected_pipelines.split(",") if p.strip()
        ] if affected_pipelines else []

        entry["兼容性评估"] = "兼容" if is_compatible else "不兼容"

        return {
            "change_id": change_id,
            "table": table_name,
            "change_type": change_type,
            "change_detail": change_detail,
            "is_compatible": is_compatible,
            "risk_level": risk_level,
            "affected_pipelines": pipeline_list,
            "recommendation": (
                "变更可安全执行" if is_compatible
                else "变更不兼容，需制定回滚方案并通知下游管线"
            ),
            "evaluated_at": datetime.now().isoformat(),
        }

    def analyze_field_impact(self, table_name: str, field_name: str) -> dict[str, Any]:
        """分析字段变更的影响范围。

        通过 lineage_graph 血缘信息回溯该字段的下游使用情况，
        统计受影响的处理和校验规则。
        """
        changes = self.query_by_table(table_name)
        related_changes = [
            c for c in changes
            if field_name.casefold() in str(c.get("变更详情", "")).casefold()
        ]

        lineage_edges = store.rows("lineage_graph") if "lineage_graph" in store._tables else []
        affected_targets = []
        for edge in lineage_edges:
            pass_fields = str(edge.get("传递字段", ""))
            if field_name.casefold() in pass_fields.casefold():
                affected_targets.append(edge.get("目标节点"))

        quality_rules = store.rows("quality_rule") if "quality_rule" in store._tables else []
        affected_rules = []
        for rule in quality_rules:
            if field_name.casefold() in str(rule.get("校验字段", "")).casefold():
                affected_rules.append(rule.get("规则名称"))

        return {
            "table": table_name,
            "field": field_name,
            "change_history_count": len(related_changes),
            "affected_downstream_nodes": affected_targets,
            "affected_quality_rules": affected_rules,
            "total_impact_items": len(affected_targets) + len(affected_rules),
            "risk_summary": (
                "高影响：涉及多个下游节点和质量规则" if len(affected_targets) + len(affected_rules) > 3
                else "影响范围可控"
            ),
            "analyzed_at": datetime.now().isoformat(),
        }

    def compute_evolution_stats(self, table_name: str) -> dict[str, Any]:
        """统计表 Schema 变更的演化趋势。

        汇总指定表的字段变更频率和类型分布，
        产出 Schema 稳定性评估数据。
        """
        changes = self.query_by_table(table_name)
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        compatible = 0
        incompatible = 0

        for c in changes:
            ctype = str(c.get("变更类型", "未知"))
            cstatus = str(c.get("status", "未知"))
            by_type[ctype] = by_type.get(ctype, 0) + 1
            by_status[cstatus] = by_status.get(cstatus, 0) + 1
            if c.get("兼容性评估") == "兼容":
                compatible += 1
            elif c.get("兼容性评估") == "不兼容":
                incompatible += 1

        return {
            "table": table_name,
            "total_changes": len(changes),
            "compatible_changes": compatible,
            "incompatible_changes": incompatible,
            "by_type": by_type,
            "by_status": by_status,
            "stability_score": round(compatible / max(len(changes), 1) * 100, 1),
            "snapshot_at": datetime.now().isoformat(),
        }

    # ---- 写入影响明细 ----

    def __init__(self) -> None:
        self._write_sources: list[WriteSource] = [copy.deepcopy(s) for s in DEFAULT_WRITE_SOURCES]
        self._drills: dict[int, dict[str, Any]] = {}
        self._drill_seq = 1000
        self._ensure_default_drill()

    def _lineage_edges(self) -> list[dict[str, Any]]:
        if "lineage_graph" not in store.module_names():
            return []
        return [dict(row) for row in store.rows("lineage_graph")]

    def _default_schema(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return copy.deepcopy(DEFAULT_OLD_COLUMNS), copy.deepcopy(DEFAULT_NEW_COLUMNS)

    def list_write_sources(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """登记在案的金额列写入来源（直传 + 派生）。"""
        rows = [
            source.to_dict() for source in self._write_sources
            if table_name is None or source.target_table == table_name
        ]
        return rows

    def register_write_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """登记金额列的写入来源；同一 edge_id 重复登记视为更新。"""
        known = {source.edge_id: index for index, source in enumerate(self._write_sources)}
        for payload in sources:
            source = WriteSource.from_dict(payload)
            if source.edge_id in known:
                self._write_sources[known[source.edge_id]] = source
            else:
                self._write_sources.append(source)
                known[source.edge_id] = len(self._write_sources) - 1
        return [
            source.to_dict() for source in self._write_sources
            if any(source.edge_id == item.get("edge_id") for item in sources)
        ]

    def analyze_write_impact(
        self,
        table_name: str | None = None,
        field_name: str | None = None,
        new_field: str | None = None,
        old_columns: list[str | dict[str, Any]] | None = None,
        new_columns: list[str | dict[str, Any]] | None = None,
        change_id: int | None = None,
        sources: list[WriteSource] | None = None,
    ) -> dict[str, Any]:
        """在兼容性结论之外，给出金额列改名的实际写入影响明细。

        - 解析旧/新 Schema 的金额列差异；
        - 沿表血缘追到实际写入点；
        - 同一金额列的直接传值和派生表达式分别展开成路径；
        - 标出缺少新字段映射、缺少换算依据、转换边待核实的位置。
        """
        table = table_name or DEFAULT_TARGET_TABLE
        old_cols = list(old_columns) if old_columns is not None else self._default_schema()[0]
        new_cols = list(new_columns) if new_columns is not None else self._default_schema()[1]
        diffs = diff_columns(old_cols, new_cols)
        rename = find_rename(diffs, field_name, new_field) or (
            DEFAULT_RENAME["old_field"], DEFAULT_RENAME["new_field"]
        )
        old_field, renamed_field = rename
        diffs = self._reconcile_rename(diffs, old_field, renamed_field)

        if change_id is not None:
            change = store.find(self.MODULE, change_id)
            if change:
                table = str(change.get("目标表") or table)
                old_field = field_name or old_field

        lineage = self._lineage_edges()
        if sources is None:
            sources = [
                source for source in self._write_sources
                if source.target_table == table and source.old_field == old_field
            ]
        else:
            sources = [
                source for source in sources
                if source.target_table == table and source.old_field == old_field
            ]

        # 逐条构建实际写入路径。上游是否存在未核实边要看全局登记表，
        # 否则跨表的下游写入会被误判为可执行。
        all_sources = list(sources)
        known_edges = {source.edge_id for source in sources}
        all_sources.extend(
            source for source in self._write_sources if source.edge_id not in known_edges
        )
        paths: list[dict[str, Any]] = []
        for source in sources:
            upstream_ok, upstream_reason = self._upstream_readiness(
                source, all_sources, lineage)
            path = build_write_path(
                source,
                lineage_edges=lineage,
                upstream_executable=upstream_ok,
                upstream_reason=upstream_reason,
            )
            paths.append(path.to_dict())

        write_points: dict[str, dict[str, Any]] = {}
        for source in sources:
            point = write_points.setdefault(source.write_point, {
                "write_point": source.write_point,
                "pipeline": source.pipeline,
                "target_table": source.target_table,
                "source_tables": [],
                "lineage_paths": [],
                "modes": [],
                "edge_ids": [],
            })
            if source.source_table not in point["source_tables"]:
                point["source_tables"].append(source.source_table)
            if source.mode not in point["modes"]:
                point["modes"].append(source.mode)
            point["edge_ids"].append(source.edge_id)
            chain = trace_lineage_chain(source.source_table, source.target_table, lineage)
            for hop in (chain[0] if chain else [{"source": source.source_table,
                                                  "target": source.target_table}]):
                entry = {"source": hop.get("source"), "target": hop.get("target")}
                if entry not in point["lineage_paths"]:
                    point["lineage_paths"].append(entry)

        amount_diffs = [d.to_dict() for d in diffs if d.is_amount]
        direct = [s for s in sources if s.mode == "direct"]
        derived = [s for s in sources if s.mode == "derived"]
        missing_mapping = [s.edge_id for s in sources if not s.new_field]
        missing_basis = [s.edge_id for s in sources if s.mode == "derived" and not s.conversion_basis]
        unverified = [s.edge_id for s in sources if not s.verified]

        return {
            "table": table,
            "field": old_field,
            "new_field": renamed_field,
            "change_id": change_id,
            "rename": {
                "old_field": old_field,
                "new_field": renamed_field,
                "confidence": next(
                    (d.confidence for d in diffs if d.change == "rename"
                     and d.old_field == old_field), 1.0
                ),
                "declared": bool(field_name and new_field),
                "reason": next(
                    (d.reason for d in diffs if d.change == "rename"
                     and d.old_field == old_field), "",
                ),
            },
            "schema_versions": {
                "old": [column_name(c) for c in old_cols],
                "new": [column_name(c) for c in new_cols],
            },
            "amount_column_diff": amount_diffs,
            "column_diff": [d.to_dict() for d in diffs],
            "write_points": list(write_points.values()),
            "write_sources": [s.to_dict() for s in sources],
            "paths": paths,
            "summary": {
                "write_source_count": len(sources),
                "direct_count": len(direct),
                "derived_count": len(derived),
                "missing_mapping": missing_mapping,
                "missing_conversion_basis": missing_basis,
                "unverified_edges": unverified,
                "blocked_writes": [p["edge_id"] for p in paths if not p["executable"]],
            },
            "analyzed_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _reconcile_rename(
        diffs: list[Any], old_field: str, new_field: str
    ) -> list[Any]:
        """显式指定的改名口径要覆盖自动推断结果。"""
        matched = False
        reconciled = []
        for diff in diffs:
            if diff.change == "rename" and (diff.old_field == old_field or diff.new_field == new_field):
                reconciled.append(ColumnDiff(
                    old_field=old_field, new_field=new_field,
                    data_type=diff.data_type, new_type=diff.new_type,
                    change="rename", is_amount=diff.is_amount,
                    confidence=max(diff.confidence, 0.9), reason=diff.reason,
                ))
                matched = True
            else:
                reconciled.append(diff)
        if not matched:
            reconciled.append(ColumnDiff(
                old_field=old_field, new_field=new_field, data_type="", new_type="",
                change="rename", is_amount=True, confidence=1.0,
                reason="调用方显式指定的改名口径",
            ))
        return reconciled

    @staticmethod
    def _upstream_readiness(
        source: WriteSource,
        sources: list[WriteSource],
        lineage: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """派生写入依赖上游表的写入结果：上游边未核实，则下游写入不能执行。"""
        if not source.depends_on_table:
            return True, ""
        upstream_edges = [other for other in sources if other.target_table == source.depends_on_table]
        if not upstream_edges:
            return False, f"依赖的上游表 {source.depends_on_table} 没有登记写入来源"
        # 同名目标列的上游边若全部未核实，说明新列在上游还没落下来。
        failing = [
            other for other in upstream_edges
            if not other.verified or not other.new_field
            or (other.mode == "derived" and not other.conversion_basis)
        ]
        if failing and len(failing) == len(upstream_edges):
            first = failing[0]
            return False, f"依赖的上游写入 {first.write_point} 未核实"
        if failing:
            names = "、".join(sorted({item.edge_id for item in failing}))
            return False, (
                f"上游表 {source.depends_on_table} 仍有未核实写入：{names}"
            )
        return True, ""

    # ---- 演练编制 ----

    def list_drills(self) -> list[dict[str, Any]]:
        return [self._drill_summary(drill) for drill in self._drills.values()]

    def get_drill(self, drill_id: int | str) -> dict[str, Any] | None:
        drill = self._find_drill(drill_id)
        if drill is None:
            return None
        return self._compose_drill(drill)

    def create_drill(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """读取写入影响明细，生成带旧字段、新字段和待核实转换边的迁移映射。"""
        payload = payload or {}
        impact = self.analyze_write_impact(
            table_name=payload.get("table") or payload.get("目标表"),
            field_name=payload.get("old_field"),
            new_field=payload.get("new_field"),
            old_columns=payload.get("old_columns"),
            new_columns=payload.get("new_columns"),
            change_id=payload.get("change_id"),
        )
        self._drill_seq += 1
        drill_id = int(payload.get("drill_id") or self._drill_seq)
        drill = {
            "id": drill_id,
            "drill_id": f"drill_{drill_id}",
            "status": "编制中",
            "table": impact["table"],
            "change_id": impact.get("change_id"),
            "old_schema": {
                "version": "old",
                "columns": self._schema_columns(impact["schema_versions"]["old"]),
            },
            "new_schema": {
                "version": "new",
                "columns": self._schema_columns(impact["schema_versions"]["new"]),
            },
            "rename": impact["rename"],
            "write_impact": impact,
            "write_sources": [
                {**source, "base_expression": source.get("expression", "")}
                for source in impact["write_sources"]
            ],
            "candidate_rows": copy.deepcopy(payload.get("candidate_rows") or DEFAULT_CANDIDATE_ROWS),
            "column_defaults": dict(DEFAULT_COLUMN_DEFAULTS),
            "migration_mapping": [],
            "preserved_columns": [],
            "trial": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        self._rebuild_drill(drill)
        self._drills[drill_id] = drill
        return self._compose_drill(drill)

    def _ensure_default_drill(self) -> None:
        """服务启动时准备一份演练草稿，页面打开即可看到写入影响。"""
        if self._drills:
            return
        self.create_drill({"drill_id": 1001})

    def _find_drill(self, drill_id: int | str) -> dict[str, Any] | None:
        if isinstance(drill_id, str):
            if drill_id.startswith("drill_"):
                drill_id = drill_id.split("_", 1)[1]
            try:
                drill_id = int(drill_id)
            except ValueError:
                return None
        return self._drills.get(int(drill_id))

    @staticmethod
    def _schema_columns(names: list[str]) -> list[dict[str, Any]]:
        return [{"name": name} for name in names]

    def _rebuild_drill(self, drill: dict[str, Any]) -> None:
        """按当前写入来源重建迁移映射，保留未参与改名的列。"""
        rename = drill["rename"]
        old_field, new_field = rename["old_field"], rename["new_field"]
        old_names = [column["name"] for column in drill["old_schema"]["columns"]]
        new_names = [column["name"] for column in drill["new_schema"]["columns"]]
        # 上游未核实的写入会把下游边一并卡住，需要体现在映射状态和提交门禁上。
        blocked_paths = {
            path["edge_id"]: path.get("blocked_by", [])
            for path in drill["write_impact"].get("paths", [])
            if not path.get("executable")
        }
        known_edges = {source.get("edge_id") for source in drill["write_sources"]}
        blocked_paths = {
            edge_id: reasons for edge_id, reasons in blocked_paths.items()
            if edge_id in known_edges
        }

        mapping: list[dict[str, Any]] = []
        for source in drill["write_sources"]:
            if source.get("old_field") != old_field:
                continue
            new_value = source.get("new_field")
            verified = bool(source.get("verified"))
            missing: list[str] = []
            if not new_value:
                missing.append("new_field")
            if source.get("mode") == "derived" and not source.get("conversion_basis"):
                missing.append("conversion_basis")
            if not verified:
                missing.append("verified")
            upstream_reasons = blocked_paths.get(source.get("edge_id"), [])
            upstream_ok = not upstream_reasons
            if missing:
                status = "待核实" if "verified" in missing else "缺少映射"
            elif not upstream_ok:
                status = "上游待核实"
            else:
                status = "已映射"
            mapping.append({
                "mapping_id": f"map_{source.get('edge_id')}",
                "edge_id": source.get("edge_id"),
                "old_field": old_field,
                "new_field": new_value,
                "output_field": source.get("source_field"),
                "mode": source.get("mode"),
                "write_point": source.get("write_point"),
                "pipeline": source.get("pipeline"),
                "expression": source.get("expression"),
                "target_expression": recalculate_expression(
                    source.get("base_expression") or source.get("expression", ""),
                    old_field, new_value or new_field,
                    source.get("conversion_factor"),
                ),
                "conversion_basis": source.get("conversion_basis"),
                "verified": verified,
                "status": status,
                "missing": missing,
                "upstream_ready": upstream_ok,
                "upstream_blocked_by": upstream_reasons,
                "blocking": status != "已映射",
            })

        drill["migration_mapping"] = mapping
        drill["preserved_columns"] = [
            name for name in old_names if name not in (old_field, new_field) and name in new_names
        ]
        drill["status"] = self._drill_status(mapping)
        drill["updated_at"] = datetime.now().isoformat()

    @staticmethod
    def _drill_status(mapping: list[dict[str, Any]]) -> str:
        if any(item["status"] == "缺少映射" for item in mapping):
            return "缺少映射"
        if any(item["status"] == "待核实" for item in mapping):
            return "待核实"
        if any(item["status"] == "上游待核实" for item in mapping):
            return "上游待核实"
        return "待试写"

    def update_drill_mapping(
        self, drill_id: int | str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """补登记或核实一条写入边：新字段、换算依据、表达式和核验状态。"""
        drill = self._find_drill(drill_id)
        if drill is None:
            return None, f"演练 {drill_id} 不存在"
        edge_id = payload.get("edge_id") or payload.get("mapping_id", "").replace("map_", "")
        source = next(
            (item for item in drill["write_sources"] if item.get("edge_id") == edge_id),
            None,
        )
        if source is None:
            return None, f"写入边 {edge_id} 不在本次演练范围内"
        if payload.get("new_field") is not None:
            source["new_field"] = payload["new_field"] or None
        if payload.get("conversion_basis") is not None:
            source["conversion_basis"] = payload["conversion_basis"] or None
        if payload.get("conversion_factor") is not None:
            source["conversion_factor"] = payload["conversion_factor"]
            # 补上换算依据后，试写表达式要同步重算，否则试写值仍按旧口径。
            base_expression = source.get("base_expression") or source.get("expression", "")
            source["base_expression"] = base_expression
            source["expression"] = recalculate_expression(
                base_expression,
                drill["rename"]["old_field"],
                drill["rename"]["new_field"],
                source.get("conversion_factor"),
            )
        if payload.get("expression") is not None:
            source["expression"] = payload["expression"]
            source["base_expression"] = payload["expression"]
        if payload.get("verified") is not None:
            source["verified"] = bool(payload["verified"])
        # 同步回登记表，后续新演练直接沿用补好的口径。
        for index, registered in enumerate(self._write_sources):
            if registered.edge_id == edge_id:
                self._write_sources[index] = WriteSource.from_dict(source)
                break
        drill["write_impact"] = self.analyze_write_impact(
            table_name=drill["table"],
            field_name=drill["rename"]["old_field"],
            new_field=drill["rename"]["new_field"],
            old_columns=drill["old_schema"]["columns"],
            new_columns=drill["new_schema"]["columns"],
            sources=[WriteSource.from_dict(item) for item in drill["write_sources"]],
        )
        self._rebuild_drill(drill)
        return self._compose_drill(drill), "迁移映射已更新"

    def change_drill_rename(
        self, drill_id: int | str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """改名口径变更：重算受影响的表达式与迁移映射，保留未改动列。"""
        drill = self._find_drill(drill_id)
        if drill is None:
            return None, f"演练 {drill_id} 不存在"
        old_field = payload.get("old_field") or drill["rename"]["old_field"]
        new_field = payload.get("new_field")
        if not new_field:
            return None, "改名口径必须给出 new_field"
        previous_new = drill["rename"]["new_field"]

        old_names = [column["name"] for column in drill["old_schema"]["columns"]]
        new_names = [column["name"] for column in drill["new_schema"]["columns"]]
        # 新列名只在 Schema 版本里替换，未改动的列原样保留。
        updated_new = [new_field if name == previous_new else name for name in new_names]
        updated_new = list(dict.fromkeys(updated_new + [new_field]))
        drill["new_schema"] = {"version": "new", "columns": self._schema_columns(updated_new)}
        drill["rename"] = {"old_field": old_field, "new_field": new_field,
                           "confidence": 1.0, "declared": True,
                           "reason": "改名口径手工调整"}

        for source in drill["write_sources"]:
            if source.get("old_field") != old_field:
                continue
            base_expression = source.get("base_expression") or source.get("expression", "")
            source["base_expression"] = base_expression
            source["expression"] = recalculate_expression(
                base_expression, old_field, new_field, source.get("conversion_factor"),
            )
            if source.get("new_field") in (previous_new, new_field):
                source["new_field"] = new_field
            if source.get("conversion_basis"):
                source["conversion_basis"] = substitute_field(
                    source["conversion_basis"], previous_new, new_field,
                )
        # old_field 若也被替换进旧 Schema 之外的列，需同步列存在性检查。
        if old_field not in old_names:
            old_names.append(old_field)
        drill["write_impact"] = self.analyze_write_impact(
            table_name=drill["table"],
            field_name=old_field,
            new_field=new_field,
            old_columns=drill["old_schema"]["columns"],
            new_columns=drill["new_schema"]["columns"],
            sources=[WriteSource.from_dict(item) for item in drill["write_sources"]],
        )
        self._rebuild_drill(drill)
        return self._compose_drill(drill), "改名口径已更新，相关表达式和迁移数据已重算"

    def run_trial(
        self, drill_id: int | str, payload: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """用迁移映射生成候选数据试写，返回输入就绪列与阻断结论。"""
        drill = self._find_drill(drill_id)
        if drill is None:
            return None, f"演练 {drill_id} 不存在"
        payload = payload or {}
        rows = payload.get("rows") or copy.deepcopy(drill["candidate_rows"])
        sources = [WriteSource.from_dict(item) for item in drill["write_sources"]]
        old_columns = [column["name"] for column in drill["old_schema"]["columns"]]
        new_columns = [column["name"] for column in drill["new_schema"]["columns"]]
        trial = build_trial(
            sources=sources,
            rows=rows,
            old_columns=old_columns,
            new_columns=new_columns,
            rename_old=drill["rename"]["old_field"],
            rename_new=drill["rename"]["new_field"],
            default_values=drill.get("column_defaults") or DEFAULT_COLUMN_DEFAULTS,
            upstream_blocked={
                item["edge_id"]: list(item.get("upstream_blocked_by") or [])
                for item in drill["migration_mapping"]
                if item.get("upstream_blocked_by")
            },
        )
        trial["mapping_status"] = [
            {
                "mapping_id": item["mapping_id"],
                "old_field": item["old_field"],
                "new_field": item["new_field"],
                "mode": item["mode"],
                "status": item["status"],
                "blocking": item["blocking"],
            }
            for item in drill["migration_mapping"]
        ]
        trial["executable"] = not trial["blocked"]
        drill["trial"] = trial
        if trial["blocks_settlement_task"]:
            drill["status"] = "试写阻断"
        else:
            drill["status"] = "试写通过"
        drill["updated_at"] = datetime.now().isoformat()
        message = "试写完成，结算任务被阻断" if trial["blocks_settlement_task"] else "试写完成"
        return self._compose_drill(drill), message

    def submit_drill(self, drill_id: int | str) -> tuple[dict[str, Any] | None, str]:
        """映射补齐且试写通过后才能提交迁移演练。"""
        drill = self._find_drill(drill_id)
        if drill is None:
            return None, f"演练 {drill_id} 不存在"
        blocking = [item for item in drill["migration_mapping"] if item["blocking"]]
        if blocking:
            reasons: list[str] = []
            for item in blocking:
                if item.get("upstream_blocked_by"):
                    reasons.append(f"{item['edge_id']}: 上游未核实")
                for reason in item["missing"]:
                    reasons.append(f"{item['edge_id']}: {reason}")
            return self._compose_drill(drill), (
                "仍有待核实转换边，无法提交：" + "；".join(dict.fromkeys(reasons))
            )
        drill["status"] = "已提交"
        drill["submitted_at"] = datetime.now().isoformat()
        drill["updated_at"] = drill["submitted_at"]
        return self._compose_drill(drill), "迁移演练已提交"

    def lookup_drill_paths(self, drill_id: int | str) -> dict[str, Any] | None:
        """回查本次演练关联的字段路径、Schema 版本和写入点。"""
        drill = self._find_drill(drill_id)
        if drill is None:
            return None
        impact = drill["write_impact"]
        return {
            "drill_id": drill["drill_id"],
            "table": drill["table"],
            "rename": drill["rename"],
            "schema_versions": impact["schema_versions"],
            "paths": impact["paths"],
            "write_points": impact["write_points"],
            "mapping": drill["migration_mapping"],
            "preserved_columns": drill["preserved_columns"],
            "looked_up_at": datetime.now().isoformat(),
        }

    def _drill_summary(self, drill: dict[str, Any]) -> dict[str, Any]:
        return {
            "drill_id": drill["drill_id"],
            "id": drill["id"],
            "table": drill["table"],
            "status": drill["status"],
            "rename": drill["rename"],
            "mapping_count": len(drill["migration_mapping"]),
            "blocking_count": sum(1 for item in drill["migration_mapping"] if item["blocking"]),
            "updated_at": drill["updated_at"],
        }

    def _compose_drill(self, drill: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(drill)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg
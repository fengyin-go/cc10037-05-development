"""Schema 写入影响分析引擎。

字段改名只给出「兼容/不兼容」结论时，下游真正关心的是：
- 金额列在旧/新 Schema 之间到底差了什么；
- 沿表血缘追到实际执行 INSERT 的写入点；
- 每个写入点是直接传值，还是依赖旧字段的计算表达式；
- 哪些转换边已经核实、哪些写入因此不能执行。

本模块只做纯计算（不读写 store），由 schema_evolution 领域服务组合数据后调用。
"""

from __future__ import annotations

import ast
import copy
import operator
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---- 金额列识别 ----

AMOUNT_NAME_HINTS = (
    "amount", "amt", "money", "price", "cost", "fee", "balance",
    "金额", "应收", "应付", "结算", "费用", "成本", "余额",
)
AMOUNT_TYPE_HINTS = ("decimal", "numeric", "double", "float", "money")
SETTLEMENT_NAME_HINTS = ("settle", "settlement", "receivable", "结算", "应收")
# 这些列名即使带金额词根，也不是金额值，避免把渠道类字段误判成金额列。
NON_AMOUNT_SUFFIXES = ("channel", "type", "status", "code", "name", "desc", "rate", "flag")


def is_amount_column(column: str | dict[str, Any]) -> bool:
    """判断一个列名（或列定义）是否属于金额列。

    只看列名容易漏掉 `total`、`revenue` 这类业务命名，所以列定义里
    如果类型是 decimal/float 等数值型，并且列名带金额语义，也视为金额列。
    """
    if isinstance(column, dict):
        name = str(column.get("name") or column.get("字段名") or column.get("name_en") or "")
        data_type = str(column.get("data_type") or column.get("type") or "").casefold()
    else:
        name = str(column or "")
        data_type = ""
    lowered = name.casefold()
    if lowered.endswith(NON_AMOUNT_SUFFIXES):
        return False
    if any(hint in lowered for hint in AMOUNT_NAME_HINTS):
        return True
    return bool(data_type) and data_type.startswith(AMOUNT_TYPE_HINTS) and any(
        token in lowered for token in ("_amt", "_sum", "_total", "_val")
    )


def is_settlement_column(name: str) -> bool:
    lowered = str(name or "").casefold()
    return any(hint in lowered for hint in SETTLEMENT_NAME_HINTS)


def column_name(column: str | dict[str, Any]) -> str:
    if isinstance(column, dict):
        return str(column.get("name") or column.get("字段名") or "")
    return str(column)


def _column_type(column: str | dict[str, Any]) -> str:
    if isinstance(column, dict):
        return str(column.get("data_type") or column.get("type") or "")
    return ""


def _same_family_type(old_type: str, new_type: str) -> bool:
    """粗粒度类型族比较：decimal(18,2) 与 decimal(20,4) 视为同类。"""
    def family(value: str) -> str:
        value = value.casefold()
        for prefix in ("decimal", "numeric", "double", "float", "bigint", "int", "varchar", "string", "timestamp", "date"):
            if value.startswith(prefix):
                return prefix
        return value

    return family(old_type) == family(new_type)


# ---- 字段差异 ----


@dataclass
class ColumnDiff:
    old_field: str
    new_field: str
    data_type: str
    new_type: str
    change: str            # rename / add / drop / type_change / unchanged
    is_amount: bool
    confidence: float = 1.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_field": self.old_field,
            "new_field": self.new_field,
            "data_type": self.data_type,
            "new_type": self.new_type,
            "change": self.change,
            "is_amount": self.is_amount,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def diff_columns(
    old_columns: Iterable[str | dict[str, Any]],
    new_columns: Iterable[str | dict[str, Any]],
) -> list[ColumnDiff]:
    """解析旧/新 Schema 的列差异，并尽量识别「改名」。

    改名的判定依据（不依赖数据库元数据）：
    1. 旧列消失、新列出现；
    2. 两列都是金额列（或都不是金额列）；
    3. 类型属于同一族。
    满足条件的按出现顺序两两配对，并与显式 rename 合并。
    """
    old_list = list(old_columns)
    new_list = list(new_columns)
    old_map = {column_name(c): c for c in old_list}
    new_map = {column_name(c): c for c in new_list}

    diffs: list[ColumnDiff] = []
    for name, column in old_map.items():
        if name in new_map:
            old_type = _column_type(column)
            new_type = _column_type(new_map[name])
            if old_type and new_type and old_type.casefold() != new_type.casefold():
                diffs.append(ColumnDiff(
                    old_field=name, new_field=name, data_type=old_type, new_type=new_type,
                    change="type_change", is_amount=is_amount_column(column),
                    reason=f"类型由 {old_type} 调整为 {new_type}",
                ))
            else:
                diffs.append(ColumnDiff(
                    old_field=name, new_field=name, data_type=old_type, new_type=new_type,
                    change="unchanged", is_amount=is_amount_column(column),
                    reason="列名与类型均未变化，原路径保留",
                ))

    removed = [c for c in old_list if column_name(c) not in new_map]
    added = [c for c in new_list if column_name(c) not in old_map]

    # 按金额/非金额分组配对，先配对金额列，避免 payment_channel 抢占 settle_amount。
    used_added: set[str] = set()
    for old_col in removed:
        old_name = column_name(old_col)
        candidate = None
        for new_col in added:
            new_name = column_name(new_col)
            if new_name in used_added:
                continue
            if bool(is_amount_column(old_col)) != bool(is_amount_column(new_col)):
                continue
            if _same_family_type(_column_type(old_col), _column_type(new_col)):
                candidate = new_col
                break
        if candidate is not None:
            new_name = column_name(candidate)
            used_added.add(new_name)
            diffs.append(ColumnDiff(
                old_field=old_name, new_field=new_name,
                data_type=_column_type(old_col), new_type=_column_type(candidate),
                change="rename",
                is_amount=is_amount_column(old_col) or is_amount_column(candidate),
                confidence=0.9 if is_amount_column(old_col) else 0.75,
                reason=(
                    "金额列同类型一删一增，判定为字段改名"
                    if is_amount_column(old_col)
                    else "同类型一删一增，判定为字段改名"
                ),
            ))
        else:
            diffs.append(ColumnDiff(
                old_field=old_name, new_field="", data_type=_column_type(old_col),
                new_type="", change="drop", is_amount=is_amount_column(old_col),
                reason="新 Schema 中不存在该列",
            ))

    for new_col in added:
        new_name = column_name(new_col)
        if new_name in used_added:
            continue
        diffs.append(ColumnDiff(
            old_field="", new_field=new_name, data_type="",
            new_type=_column_type(new_col), change="add",
            is_amount=is_amount_column(new_col),
            reason="新 Schema 新增列",
        ))

    return diffs


def find_rename(
    diffs: Iterable[ColumnDiff],
    old_field: str | None = None,
    new_field: str | None = None,
) -> tuple[str, str] | None:
    """从差异中确定本次改名口径。显式指定优先，其次取第一个金额列改名。"""
    if old_field and new_field:
        return old_field, new_field
    for diff in diffs:
        if diff.change == "rename" and diff.is_amount:
            return diff.old_field, diff.new_field
    for diff in diffs:
        if diff.change == "rename":
            return diff.old_field, diff.new_field
    return None


# ---- 表达式处理 ----

_FIELD_TOKEN = re.compile(r"[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*")


def substitute_field(expression: str, old_field: str, new_field: str) -> str:
    """把表达式里对旧字段的引用替换为新字段，避免误伤相似前缀。"""
    if not expression or not old_field or not new_field or old_field == new_field:
        return expression

    def replace(match: re.Match[str]) -> str:
        return new_field if match.group(0) == old_field else match.group(0)

    return _FIELD_TOKEN.sub(replace, expression)


def expression_fields(expression: str) -> list[str]:
    """提取表达式里出现的字段引用（去掉数值、True/False 等字面量）。"""
    reserved = {"true", "false", "none", "and", "or", "not", "if", "else"}
    fields: list[str] = []
    for match in _FIELD_TOKEN.findall(expression or ""):
        if match.casefold() in reserved:
            continue
        if match not in fields:
            fields.append(match)
    return fields


def recalculate_expression(
    expression: str,
    old_field: str,
    new_field: str,
    conversion_factor: float | None = None,
) -> str:
    """按新口径重算表达式：先替换字段名，再按换算系数补上换算依据。"""
    recalculated = substitute_field(expression, old_field, new_field)
    if conversion_factor is None or conversion_factor == 1:
        return recalculated
    if not recalculated:
        return recalculated
    return f"({recalculated}) * {conversion_factor:g}"


# 只允许算术/比较/布尔运算，禁止属性访问、函数调用和下标，避免表达式执行任意代码。
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}


def safe_evaluate(expression: str, variables: dict[str, Any]) -> Any:
    """安全求值：仅支持字面量、字段、四则运算和比较。"""
    if not str(expression or "").strip():
        raise ValueError("表达式为空")
    try:
        tree = ast.parse(str(expression), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"表达式语法错误: {expression}") from exc

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, bool)):
                return node.value
            raise ValueError("表达式只允许数值和布尔字面量")
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ValueError(f"缺少字段输入: {node.id}")
            return variables[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = evaluate(node.left), evaluate(node.right)
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.BoolOp):
            values = [bool(evaluate(value)) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = evaluate(comparator)
                if type(op) not in _CMP_OPS or not _CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return evaluate(node.body) if evaluate(node.test) else evaluate(node.orelse)
        raise ValueError("表达式包含不支持的语法")

    return evaluate(tree)


# ---- 写入来源与路径 ----


@dataclass
class WriteSource:
    """一个金额列写入来源。

    mode=direct 表示直接传值；mode=derived 表示依赖旧字段的计算表达式。
    new_field 为空表示改名后尚未建立映射。
    """
    edge_id: str
    target_table: str
    source_table: str
    source_field: str
    old_field: str
    new_field: str | None
    write_point: str
    pipeline: str
    mode: str = "direct"
    label: str = ""
    expression: str = ""
    conversion_basis: str | None = None
    conversion_factor: float | None = None
    verified: bool = True
    compute_node: str = ""
    affects_settlement: bool = True
    depends_on_table: str = ""
    declared_by: str = "seed"

    @property
    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.new_field:
            missing.append("new_field")
        if self.mode == "derived" and not self.conversion_basis:
            missing.append("conversion_basis")
        if not self.verified:
            missing.append("verified")
        return missing

    @property
    def ready(self) -> bool:
        return not self.missing

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WriteSource":
        fields = {
            "edge_id", "target_table", "source_table", "source_field", "old_field",
            "new_field", "write_point", "pipeline", "mode", "label", "expression",
            "conversion_basis", "conversion_factor", "verified", "compute_node",
            "affects_settlement", "depends_on_table", "declared_by",
        }
        return cls(**{key: payload[key] for key in fields if key in payload})

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "target_table": self.target_table,
            "source_table": self.source_table,
            "source_field": self.source_field,
            "old_field": self.old_field,
            "new_field": self.new_field,
            "write_point": self.write_point,
            "compute_node": self.compute_node,
            "pipeline": self.pipeline,
            "mode": self.mode,
            "label": self.label or ("直传金额" if self.mode == "direct" else "派生应收额"),
            "expression": self.expression,
            "conversion_basis": self.conversion_basis,
            "conversion_factor": self.conversion_factor,
            "verified": self.verified,
            "ready": self.ready,
            "missing": self.missing,
            "affects_settlement": self.affects_settlement,
            "depends_on_table": self.depends_on_table,
            "declared_by": self.declared_by,
        }


@dataclass
class PathStep:
    node: str
    role: str                      # source / transform / write
    field: str
    schema_version: str            # old / new
    table: str
    write_point: str = ""
    expression: str = ""
    verified: bool = True
    executable: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "role": self.role,
            "field": self.field,
            "schema_version": self.schema_version,
            "table": self.table,
            "write_point": self.write_point,
            "expression": self.expression,
            "verified": self.verified,
            "executable": self.executable,
            "reason": self.reason,
        }


@dataclass
class WritePath:
    path_id: str
    edge_id: str
    mode: str
    target_table: str
    write_point: str
    steps: list[PathStep] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        return bool(self.steps) and self.steps[-1].executable and not self.blocked_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "edge_id": self.edge_id,
            "mode": self.mode,
            "target_table": self.target_table,
            "write_point": self.write_point,
            "executable": self.executable,
            "blocked_by": list(self.blocked_by),
            "steps": [step.to_dict() for step in self.steps],
        }


def build_write_path(
    source: WriteSource,
    lineage_edges: Iterable[dict[str, Any]] | None = None,
    upstream_executable: bool = True,
    upstream_reason: str = "",
) -> WritePath:
    """沿表血缘构建字段路径，并把新旧列落到各自的 Schema 版本。

    路径固定包含：源表字段（旧版本）→ 计算节点（旧版本）→ 写入点（新版本）。
    如果写入来源本身未核实、或上游写入不可执行，末端写入步骤标记为不可执行。
    """
    rename_target = source.target_table
    lines: list[list[dict[str, Any]]] = [
        [{"source": source.source_table, "target": source.target_table}]
    ]
    if lineage_edges:
        chain = trace_lineage_chain(source.source_table, source.target_table, lineage_edges)
        if chain:
            lines = chain

    source_step_table = source.source_table
    steps: list[PathStep] = [PathStep(
        node=source_step_table,
        role="source",
        field=source.old_field,
        schema_version="old",
        table=source_step_table,
        verified=upstream_executable,
        executable=True,
        reason="" if upstream_executable else upstream_reason,
    )]

    if source.compute_node:
        steps.append(PathStep(
            node=source.compute_node,
            role="transform",
            field=source.old_field,
            schema_version="old",
            table=source_step_table,
            expression=source.expression,
            verified=source.verified,
            executable=True,
            reason="" if source.verified else "转换边待核实",
        ))

    # 中间表：仅展示，不改变字段命名口径。
    for hop in lines[0][1:-1]:
        steps.append(PathStep(
            node=str(hop.get("target") or ""),
            role="transform",
            field=source.old_field,
            schema_version="old",
            table=str(hop.get("target") or ""),
            verified=True,
            executable=True,
        ))

    mapped_field = source.new_field or source.old_field
    version = "new" if source.new_field else "old"
    write_executable = upstream_executable and source.ready and source.verified
    reasons: list[str] = []
    if not upstream_executable:
        reasons.append(upstream_reason or "上游写入未核实")
    if not source.new_field:
        reasons.append("改名后缺少新字段映射")
    if source.mode == "derived" and not source.conversion_basis:
        reasons.append("派生表达式缺少换算依据")
    if not source.verified:
        reasons.append("转换边待核实")

    steps.append(PathStep(
        node=source.write_point,
        role="write",
        field=mapped_field,
        schema_version=version,
        table=rename_target,
        write_point=source.write_point,
        expression=source.expression,
        # 只有真正能执行的写入才标记为已核实，避免未核实边的下游被当成已确认口径。
        verified=write_executable,
        executable=write_executable,
        reason="；".join(reasons),
    ))

    path = WritePath(
        path_id=f"path_{source.edge_id}",
        edge_id=source.edge_id,
        mode=source.mode,
        target_table=source.target_table,
        write_point=source.write_point,
        steps=steps,
    )
    # 阻断原因按路径顺序记录一次，避免前端重复展示同一句提示。
    for reason in reasons:
        if reason and reason not in path.blocked_by:
            path.blocked_by.append(reason)
    return path


def trace_lineage_chain(
    source_table: str,
    target_table: str,
    lineage_edges: Iterable[dict[str, Any]],
    max_hops: int = 6,
) -> list[list[dict[str, Any]]]:
    """在表血缘中查找 source_table → target_table 的最短路径。"""
    edges = list(lineage_edges)
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge.get("源节点", "")), []).append(edge)

    queue: list[tuple[str, list[dict[str, Any]]]] = [(source_table, [])]
    visited: set[str] = {source_table}
    while queue:
        node, path = queue.pop(0)
        if node == target_table and path:
            return [path]
        if len(path) >= max_hops:
            continue
        for edge in adjacency.get(node, []):
            target = str(edge.get("目标节点", ""))
            if target in visited:
                continue
            visited.add(target)
            queue.append((target, path + [{
                "source": node,
                "target": target,
                "edge": edge,
            }]))
    return []


# ---- 试写 ----


@dataclass
class TrialColumn:
    column: str
    source: str                    # direct / derived / preserved / default
    mode: str = ""
    write_point: str = ""
    input_fields: list[str] = field(default_factory=list)
    inputs_ready: bool = True
    verified: bool = True
    values: list[Any] | None = None
    missing: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "source": self.source,
            "mode": self.mode,
            "write_point": self.write_point,
            "input_fields": list(self.input_fields),
            "inputs_ready": self.inputs_ready,
            "verified": self.verified,
            "values": copy.deepcopy(self.values),
            "missing": list(self.missing),
            "reason": self.reason,
        }


def evaluate_source_values(
    source: WriteSource,
    rows: list[dict[str, Any]],
) -> tuple[list[Any] | None, list[str]]:
    """计算一个写入来源在候选行上的取值；缺输入时返回 None 和缺失原因。"""
    if not source.ready:
        return None, [f"缺少 {item}" for item in source.missing]

    expression = source.expression or source.source_field
    values: list[Any] = []
    missing: list[str] = []
    for row in rows:
        variables: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, (int, float)):
                variables[key] = value
            else:
                try:
                    variables[key] = float(value)
                except (TypeError, ValueError):
                    variables[key] = value
        # 改名后表达式可能已经用新列书写，而候选数据仍按旧列提供；
        # 映射已核实时允许从旧列取值作为新列输入。
        if source.new_field and source.new_field not in variables and source.old_field in row:
            variables[source.new_field] = variables[source.old_field]
        for field_name in expression_fields(expression):
            if field_name not in variables:
                missing.append(field_name)
        if missing:
            continue
        try:
            values.append(safe_evaluate(expression, variables))
        except ValueError as exc:
            return None, [str(exc)]
    if missing:
        return None, [f"缺少输入: {name}" for name in dict.fromkeys(missing)]
    return values, []


def build_trial(
    sources: list[WriteSource],
    rows: list[dict[str, Any]],
    old_columns: list[str],
    new_columns: list[str],
    rename_old: str,
    rename_new: str,
    default_values: dict[str, Any] | None = None,
    upstream_blocked: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """生成候选数据试写结果。

    返回每个目标列的输入就绪情况、直接/派生取值以及「缺映射是否阻断整个结算任务」。
    upstream_blocked 记录因上游写入未核实而不可执行的边。
    """
    default_values = default_values or {}
    upstream_blocked = upstream_blocked or {}
    target_sources = [s for s in sources if s.old_field == rename_old]
    columns: list[TrialColumn] = []
    comparison: list[dict[str, Any]] = []
    executed_writes: list[dict[str, Any]] = []
    blocked_writes: list[dict[str, Any]] = []
    missing_inputs: list[str] = []

    direct_values: list[Any] | None = None
    derived_values: list[Any] | None = None

    for source in target_sources:
        values, missing = evaluate_source_values(source, rows)
        inputs = expression_fields(source.expression or source.source_field)
        # 缺 new_field 的写入边按「目标仍是改名后的列」展示，便于看出是哪一列拿不到值。
        output_column = source.new_field or (
            rename_new if source.affects_settlement else rename_old
        )
        column = TrialColumn(
            column=output_column,
            source=source.mode,
            mode=source.mode,
            write_point=source.write_point,
            input_fields=inputs,
            inputs_ready=values is not None and not missing,
            verified=source.verified and source.ready,
            values=values,
            missing=missing if missing else source.missing,
            reason="" if values is not None else "缺少映射或输入，无法试写",
        )
        upstream_reasons = upstream_blocked.get(source.edge_id, [])
        if upstream_reasons:
            column.inputs_ready = False
            column.verified = False
            column.missing = list(column.missing) + upstream_reasons
            column.reason = "；".join(upstream_reasons)
        columns.append(column)
        if source.mode == "direct" and values is not None and not upstream_reasons:
            direct_values = values
        if source.mode == "derived" and values is not None and not upstream_reasons:
            derived_values = values
        if values is not None and source.verified and not upstream_reasons:
            executed_writes.append({
                "edge_id": source.edge_id,
                "write_point": source.write_point,
                "mode": source.mode,
                "column": column.column,
            })
        else:
            blocked_writes.append({
                "edge_id": source.edge_id,
                "write_point": source.write_point,
                "mode": source.mode,
                "column": column.column,
                "reason": "；".join(
                    list(missing or source.missing) + list(upstream_reasons)
                ) or "转换边未核实",
            })
        for name in missing:
            if name.startswith("缺少输入:"):
                for field_name in name.split(":", 1)[1].split("、"):
                    if field_name.strip() and field_name.strip() not in missing_inputs:
                        missing_inputs.append(field_name.strip())

    # 已核验的直传输入可对照：两侧都有值时给出差异。
    if direct_values is not None:
        for index, value in enumerate(direct_values):
            entry: dict[str, Any] = {"row_index": index, "direct": value}
            if derived_values is not None and index < len(derived_values):
                derived_value = derived_values[index]
                entry["derived"] = derived_value
                try:
                    entry["diff"] = round(float(derived_value) - float(value), 6)
                except (TypeError, ValueError):
                    entry["diff"] = None
            comparison.append(entry)

    # 未改名或未参与改名的列原路径保留。
    for name in old_columns:
        if name in (rename_old, rename_new):
            continue
        columns.append(TrialColumn(
            column=name,
            source="preserved",
            input_fields=[name],
            inputs_ready=True,
            verified=True,
            values=[row.get(name) for row in rows],
        ))
    for name in new_columns:
        if name in old_columns or name == rename_new:
            continue
        columns.append(TrialColumn(
            column=name,
            source="default",
            input_fields=[],
            inputs_ready=True,
            verified=True,
            values=[row.get(name, default_values.get(name, "unknown")) for row in rows],
            reason="新列使用默认值回填",
        ))

    settlement_source = next(
        (s for s in target_sources if s.affects_settlement or is_settlement_column(s.old_field)),
        None,
    )
    blocking_columns = [
        column.to_dict() for column in columns
        if column.mode and not column.inputs_ready
        and (column.column == rename_new or is_settlement_column(column.column))
    ]
    blocks_settlement = bool(blocking_columns) and settlement_source is not None
    complete_columns = [
        column.to_dict() for column in columns
        if column.mode and column.inputs_ready and column.verified
    ]
    complete_columns.extend(
        column.to_dict() for column in columns if column.source in ("preserved", "default")
    )

    # 候选行落列：直传值优先（已核验、口径明确），派生值只补直传拿不到的列；
    # 派生结果仍然保留在对照表里，避免两条来源互相覆盖后看不出差异。
    chosen_values: dict[str, list[Any]] = {}
    for column in columns:
        if column.source == "direct" and column.values is not None:
            chosen_values[column.column] = column.values
    for column in columns:
        if column.source == "derived" and column.values is not None:
            if column.column not in chosen_values:
                chosen_values[column.column] = column.values

    candidate_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        candidate = {name: row.get(name) for name in old_columns if name not in (rename_old, rename_new)}
        for name, values in chosen_values.items():
            if index < len(values):
                candidate[name] = values[index]
        for column in columns:
            if column.source == "default" and index < len(column.values or []):
                candidate[column.column] = column.values[index]  # type: ignore[index]
        candidate_rows.append(candidate)

    return {
        "rows": candidate_rows,
        "columns": [column.to_dict() for column in columns],
        "complete_columns": complete_columns,
        "incomplete_columns": [column.to_dict() for column in columns if column.mode and not column.inputs_ready],
        "inputs_ready": sorted({
            field_name
            for column in columns if column.inputs_ready and column.values is not None
            for field_name in column.input_fields
        }) or sorted({field_name for row in rows for field_name in row}),
        "missing_inputs": missing_inputs,
        "blocked": bool(blocking_columns),
        "blocks_settlement_task": blocks_settlement,
        "blocking_columns": blocking_columns,
        "executed_writes": executed_writes,
        "blocked_writes": blocked_writes,
        "comparison": comparison,
        "row_count": len(rows),
    }


# ---- 默认演练数据（订单结算宽表） ----

DEFAULT_OLD_COLUMNS: list[dict[str, Any]] = [
    {"name": "order_id", "data_type": "bigint", "nullable": False},
    {"name": "user_id", "data_type": "bigint", "nullable": True},
    {"name": "order_amount", "data_type": "decimal(18,2)", "nullable": False},
    {"name": "discount_rate", "data_type": "decimal(6,4)", "nullable": True},
    {"name": "receivable_amount", "data_type": "decimal(18,2)", "nullable": True},
    {"name": "order_time", "data_type": "timestamp", "nullable": True},
]

DEFAULT_NEW_COLUMNS: list[dict[str, Any]] = [
    {"name": "order_id", "data_type": "bigint", "nullable": False},
    {"name": "user_id", "data_type": "bigint", "nullable": True},
    {"name": "settle_amount", "data_type": "decimal(18,2)", "nullable": False},
    {"name": "discount_rate", "data_type": "decimal(6,4)", "nullable": True},
    {"name": "receivable_amount", "data_type": "decimal(18,2)", "nullable": True},
    {"name": "order_time", "data_type": "timestamp", "nullable": True},
    {"name": "payment_channel", "data_type": "varchar(32)", "nullable": False},
]

DEFAULT_RENAME = {"old_field": "order_amount", "new_field": "settle_amount"}

DEFAULT_TARGET_TABLE = "dw.dwd_order_wide"

DEFAULT_WRITE_SOURCES: list[WriteSource] = [
    WriteSource(
        edge_id="wi_direct_order_amount",
        target_table="dw.dwd_order_wide",
        source_table="ods.ods_order_info",
        source_field="order_amount",
        old_field="order_amount",
        new_field="settle_amount",
        write_point="订单宽表写入",
        compute_node="订单数据抽取",
        pipeline="ODS到DWD订单宽表ETL",
        mode="direct",
        label="直传金额",
        expression="order_amount",
        conversion_basis="同名金额直传，类型一致",
        verified=True,
        affects_settlement=True,
        declared_by="seed",
    ),
    WriteSource(
        edge_id="wi_derived_receivable",
        target_table="dw.dwd_order_wide",
        source_table="ods.ods_order_info",
        source_field="order_amount",
        old_field="order_amount",
        new_field=None,
        write_point="订单宽表写入",
        compute_node="订单清洗转换",
        pipeline="ODS到DWD订单宽表ETL",
        mode="derived",
        label="派生应收额",
        expression="order_amount * (1 - discount_rate)",
        conversion_basis=None,
        verified=False,
        affects_settlement=True,
        declared_by="seed",
    ),
    WriteSource(
        edge_id="wi_ads_settlement_sum",
        target_table="ads.ads_sales_dashboard",
        source_table="dw.dwd_order_wide",
        source_field="settle_amount",
        old_field="order_amount",
        new_field="settle_amount",
        write_point="销售报表写入",
        compute_node="销售汇总计算",
        pipeline="DWS到ADS销售报表",
        mode="derived",
        label="报表汇总",
        expression="settle_amount",
        conversion_basis="按日汇总 SUM(settle_amount)",
        verified=True,
        affects_settlement=True,
        depends_on_table="dw.dwd_order_wide",
        declared_by="seed",
    ),
]

DEFAULT_CANDIDATE_ROWS: list[dict[str, Any]] = [
    {"order_id": "SO1001", "user_id": "U001", "order_amount": 100.0,
     "discount_rate": 0.1, "receivable_amount": 90.0, "order_time": "2026-11-20 10:00:00"},
    {"order_id": "SO1002", "user_id": "U002", "order_amount": 250.0,
     "discount_rate": 0.2, "receivable_amount": 200.0, "order_time": "2026-11-20 11:30:00"},
    {"order_id": "SO1003", "user_id": "U003", "order_amount": 80.0,
     "discount_rate": 0.0, "receivable_amount": 80.0, "order_time": "2026-11-20 12:15:00"},
]

DEFAULT_COLUMN_DEFAULTS = {"payment_channel": "unknown"}

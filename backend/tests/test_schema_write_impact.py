"""Schema 写入影响分析与迁移演练的回归用例。

运行：cd backend && .venv/bin/python -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

from app.engine.schema_write_impact import (
    diff_columns,
    expression_fields,
    find_rename,
    is_amount_column,
    recalculate_expression,
    safe_evaluate,
    substitute_field,
)
from app.services.schema_evolution import SchemaEvolutionService


class AmountColumnTest(unittest.TestCase):
    def test_amount_columns(self):
        self.assertTrue(is_amount_column("order_amount"))
        self.assertTrue(is_amount_column({"name": "settle_amount", "data_type": "decimal(18,2)"}))
        self.assertTrue(is_amount_column("应收金额"))
        self.assertFalse(is_amount_column("payment_channel"))
        self.assertFalse(is_amount_column("discount_rate"))

    def test_rename_detection_skips_non_amount_added_column(self):
        diffs = diff_columns(
            ["order_id", "order_amount", "receivable_amount"],
            ["order_id", "settle_amount", "receivable_amount", "payment_channel"],
        )
        self.assertEqual(find_rename(diffs), ("order_amount", "settle_amount"))
        changes = {d.old_field or d.new_field: d.change for d in diffs}
        self.assertEqual(changes["payment_channel"], "add")
        self.assertEqual(changes["receivable_amount"], "unchanged")


class ExpressionTest(unittest.TestCase):
    def test_substitute_only_exact_token(self):
        self.assertEqual(
            substitute_field("order_amount * (1 - order_amount_rate)", "order_amount", "settle_amount"),
            "settle_amount * (1 - order_amount_rate)",
        )

    def test_recalculate_applies_factor(self):
        self.assertEqual(
            recalculate_expression("order_amount * (1 - discount_rate)", "order_amount", "settle_amount", 0.93),
            "(settle_amount * (1 - discount_rate)) * 0.93",
        )

    def test_safe_evaluate_blocks_code(self):
        with self.assertRaises(ValueError):
            safe_evaluate("__import__('os').system('true')", {})
        with self.assertRaises(ValueError):
            safe_evaluate("missing_field + 1", {})
        self.assertEqual(safe_evaluate("a * 2", {"a": 3}), 6)
        self.assertEqual(expression_fields("order_amount * (1 - discount_rate)"), ["order_amount", "discount_rate"])


class DrillFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SchemaEvolutionService()
        self.drill_id = 1001

    def test_default_drill_exposes_both_write_sources(self):
        impact = self.service.get_drill(self.drill_id)["write_impact"]
        self.assertEqual(impact["summary"]["direct_count"], 1)
        self.assertEqual(impact["summary"]["derived_count"], 1)
        self.assertEqual(impact["summary"]["missing_mapping"], ["wi_derived_receivable"])
        self.assertEqual([p["write_point"] for p in impact["write_points"]], ["订单宽表写入"])
        # 直传路径末端写新列，派生路径因缺映射仍停在旧列。
        by_edge = {p["edge_id"]: p for p in impact["paths"]}
        self.assertEqual(by_edge["wi_direct_order_amount"]["steps"][-1]["field"], "settle_amount")
        self.assertEqual(by_edge["wi_direct_order_amount"]["steps"][-1]["schema_version"], "new")
        self.assertFalse(by_edge["wi_derived_receivable"]["executable"])

    def test_trial_blocks_settlement_until_mapping_completed(self):
        drill, message = self.service.run_trial(self.drill_id, {})
        self.assertTrue(drill["trial"]["blocks_settlement_task"])
        self.assertIn("结算任务被阻断", message)
        self.assertIn("order_amount", drill["trial"]["inputs_ready"])

        drill, message = self.service.submit_drill(self.drill_id)
        self.assertIn("无法提交", message)

        drill, _ = self.service.update_drill_mapping(self.drill_id, {
            "edge_id": "wi_derived_receivable",
            "new_field": "settle_amount",
            "conversion_basis": "应收额 = 结算金额 * (1 - 折扣率)",
            "verified": True,
        })
        self.assertTrue(all(p["executable"] for p in drill["write_impact"]["paths"]))

        drill, message = self.service.run_trial(self.drill_id, {})
        self.assertFalse(drill["trial"]["blocks_settlement_task"])
        self.assertEqual(
            drill["trial"]["comparison"][0],
            {"row_index": 0, "direct": 100.0, "derived": 90.0, "diff": -10.0},
        )

        drill, message = self.service.submit_drill(self.drill_id)
        self.assertEqual(message, "迁移演练已提交")
        self.assertEqual(drill["status"], "已提交")

    def test_rename_change_keeps_unrelated_columns(self):
        # 先补齐派生边的映射，改名口径变更只重算已建立的映射。
        self.service.update_drill_mapping(self.drill_id, {
            "edge_id": "wi_derived_receivable",
            "new_field": "settle_amount",
            "conversion_basis": "应收额 = 结算金额 * (1 - 折扣率)",
            "verified": True,
        })
        drill, message = self.service.change_drill_rename(self.drill_id, {"new_field": "final_amount"})
        self.assertIn("重算", message)
        self.assertEqual(
            [c["name"] for c in drill["new_schema"]["columns"]],
            ["order_id", "user_id", "final_amount", "discount_rate",
             "receivable_amount", "order_time", "payment_channel"],
        )
        self.assertEqual(
            drill["preserved_columns"],
            ["order_id", "user_id", "discount_rate", "receivable_amount", "order_time"],
        )
        for item in drill["migration_mapping"]:
            self.assertEqual(item["new_field"], "final_amount")
            self.assertTrue(item["target_expression"].startswith("final_amount"))
        for path in drill["write_impact"]["paths"]:
            self.assertEqual(path["steps"][-1]["schema_version"], "new")
            self.assertTrue(all(step["schema_version"] == "old" for step in path["steps"][:-1]))

        lookup = self.service.lookup_drill_paths(self.drill_id)
        self.assertEqual(lookup["rename"]["new_field"], "final_amount")
        self.assertEqual(
            lookup["schema_versions"]["old"],
            ["order_id", "user_id", "order_amount", "discount_rate", "receivable_amount", "order_time"],
        )

    def test_downstream_write_blocked_by_unverified_upstream(self):
        drill = self.service.create_drill({"table": "ads.ads_sales_dashboard"})
        item = drill["migration_mapping"][0]
        self.assertEqual(item["status"], "上游待核实")
        self.assertTrue(item["upstream_blocked_by"])
        drill, message = self.service.submit_drill(drill["id"])
        self.assertIn("无法提交", message)


if __name__ == "__main__":
    unittest.main()

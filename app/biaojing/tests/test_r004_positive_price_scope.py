# -*- coding: utf-8 -*-
"""标镜 R004 非正单价口径回归（C 报告 P3-12：与"报价非正数"口径对齐）。

口径：清单行单价为负数或零时，不进可比组（与 R005 的正报价口径一致），
并在 r004_stats.excluded_rows_detail 里如实披露"单价非正数"——负价本身
就是需要人工核查的异常，不得悄悄参与等差/等比模式计算。

运行：
    cd app && python3 -W error::ResourceWarning -m unittest \\
        biaojing.tests.test_r004_positive_price_scope -v
"""

from __future__ import annotations

import unittest

from biaojing.rules import SCHEMA, FactBase, screen_all


def ev(eid: str) -> dict:
    return {"evidence_id": eid, "file_sha256": "a" * 64,
            "source_type": "project_scan", "role": "bidder",
            "quote": "合成原文",
            "locator": {"kind": "xlsx_cell", "sheet": "S", "cell": "A1"}}


def line(price, item_code: str = "A1") -> dict:
    return {"item_code": item_code, "item_name": "电线", "spec": "BV",
            "unit": "台", "qty": 1, "unit_price": price,
            "currency": "CNY", "tax_included": True,
            "evidence": f"E-p-{price}"}


def bid(bidder_id: str, prices) -> dict:
    return {"bidder_id": bidder_id, "bidder_name": f"主体{bidder_id}",
            "role": "bidder", "evidence": f"E-b-{bidder_id}",
            "outcome": {"status": "unknown"},
            "price_lines": [line(p) for p in prices]}


def screen(prices_by_bidder: dict) -> dict:
    bids = [bid(b, ps) for b, ps in sorted(prices_by_bidder.items())]
    data = {"schema": SCHEMA,
            "events": [{"event_id": "EV-1", "project_id": "P-1",
                        "event_name": "合成事件",
                        "lots": [{"lot_id": "L1", "lot_name": "一标段",
                                  "bids": bids}]}],
            "evidence": [ev(f"E-b-{b}") for b in prices_by_bidder]
            + [ev(f"E-p-{p}") for ps in prices_by_bidder.values() for p in ps]}
    return screen_all(FactBase.from_dict(data))


class R004PositivePriceScopeTests(unittest.TestCase):

    def test_negative_price_excluded_and_disclosed(self):
        # 丙的 -120 不得参与等差计算；排除原因如实披露"单价非正数"
        out = screen({"B1": [100], "B2": [110], "B3": [-120]})
        stats = out.get("r004_stats") or {}
        detail = stats.get("excluded_rows_detail", [])
        reasons = [d.get("reason", "") for d in detail
                   if d.get("bidder_id") == "B3"]
        self.assertTrue(any("非正" in r for r in reasons),
                        f"负单价未披露排除原因：{detail}")
        for d in detail:
            if d.get("bidder_id") == "B3":
                self.assertEqual(d.get("unit_price"), -120)

    def test_zero_price_excluded_too(self):
        out = screen({"B1": [100], "B2": [0], "B3": [120]})
        detail = (out.get("r004_stats") or {}).get("excluded_rows_detail", [])
        self.assertTrue(any(d.get("bidder_id") == "B2" and "非正" in d.get("reason", "")
                            for d in detail), f"零单价未排除：{detail}")

    def test_positive_pattern_still_detected(self):
        # 全正价三家等差：模式照常检出（既有行为不回归）
        out = screen({"B1": [100], "B2": [110], "B3": [120]})
        stats = out.get("r004_stats") or {}
        self.assertGreaterEqual(stats.get("comparable_rows_matched", 0), 1)
        findings = [f for f in out["findings"] if f["rule_id"] == "R004"
                    and f["signal"] not in ("不计算",)]
        self.assertTrue(findings, "三家等差应产出 R004 线索")

    def test_none_price_line_excluded_not_fatal(self):
        # 复核八轮：公式无缓存等场景产出 unit_price=None 的行——边界校验
        # 不得让整次筛查失败；该行如实列入排除（单价缺失），其余规则照常
        out = screen({"B1": [100], "B2": [110], "B3": [None]})
        stats = out.get("r004_stats") or {}
        detail = stats.get("excluded_rows_detail", [])
        self.assertTrue(any(d.get("bidder_id") == "B3"
                            and "单价缺失" in d.get("reason", "")
                            for d in detail), f"None 单价行未排除：{detail}")
        self.assertIn("R008", out.get("rule_statuses", {}),
                      "其他规则状态缺失")
        self.assertNotIn("run_status", ("failed",),
                         "筛查不应以失败告终")

    def test_corrupt_price_still_rejected_at_boundary(self):
        # 安全校验保留：非有限坏值（'abc'、布尔）在边界照旧抛出
        for bad in ("abc", True):
            with self.assertRaises(ValueError):
                screen({"B1": [100], "B2": [110], "B3": [bad]})

    def test_literal_unknown_string_rejected_not_none(self):
        # 复核九轮：allow_unknown=True 会连字面 'unknown' 一起放行，
        # 但 R004 只把 None 当「单价缺失」——'unknown' 字符串必须在
        # 边界被拒（结构化 ValueError），不得穿透到 _decimal 崩溃
        with self.assertRaises(ValueError):
            screen({"B1": [100], "B2": [110], "B3": ["unknown"]})


if __name__ == "__main__":
    unittest.main()

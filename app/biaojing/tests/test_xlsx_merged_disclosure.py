# -*- coding: utf-8 -*-
"""标镜 XLSX 合并单元格披露回归（C 线审查 P2-8 解析层半边）。

口径：
  - 工作簿存在合并区域时，解析层逐区域输出 kind=xlsx_merged_range 的
    披露证据（locator 为 `表!区域`，quote 说明"仅左上角持有值，其余为空，
    候选层不自动填充，须人工确认"）；
  - 该披露不计入 cells_total，不改变 success/partial/failed 状态口径；
  - counts.merged_ranges_total 如实统计，并设硬上限防畸形文件；
  - 无合并区域时不产出该类证据（既有行为零变化）。
  合并区域到候选行/列的自动填充属于 candidates 层（另册处理），本层只
  负责把结构如实暴露给人工与下游。

运行：
    cd app && python3 -W error::ResourceWarning -m unittest \\
        biaojing.tests.test_xlsx_merged_disclosure -v
"""

from __future__ import annotations

import io
import unittest

import openpyxl

from biaojing import xlsx_parser


def _wb_with_merges(merges: list[str], anchor_values: dict | None = None,
                    title: str = "报价") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    ws.append(["投标人", "综合单价(元)", "清单名称"])
    ws.append(["甲公司", 100, "电线"])
    for ref in merges:
        ws.merge_cells(ref)
    for coord, value in (anchor_values or {}).items():
        ws[coord] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class XlsxMergedDisclosureTests(unittest.TestCase):

    def test_vertical_merge_is_disclosed(self):
        data = _wb_with_merges(["A3:A5"], {"A3": "甲公司"})
        rec = xlsx_parser.parse(data)
        merged = [e for e in rec["evidence"]
                  if e["kind"] == "xlsx_merged_range"]
        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item["sheet"], "报价")
        self.assertEqual(item["range"], "A3:A5")
        self.assertEqual(item["locator"], "报价!A3:A5")
        self.assertIn("左上角", item["quote"])
        self.assertIn("人工", item["quote"])
        self.assertEqual(rec["counts"]["merged_ranges_total"], 1)
        self.assertEqual(rec["status"], "success")

    def test_no_merge_no_disclosure(self):
        data = _wb_with_merges([])
        rec = xlsx_parser.parse(data)
        self.assertFalse(any(e["kind"] == "xlsx_merged_range"
                             for e in rec["evidence"]))
        self.assertEqual(rec["counts"]["merged_ranges_total"], 0)
        self.assertEqual(rec["status"], "success")

    def test_multiple_ranges_all_disclosed(self):
        data = _wb_with_merges(["A3:A4", "B3:B4"], {"A3": "甲公司"})
        rec = xlsx_parser.parse(data)
        ranges = sorted(e["range"] for e in rec["evidence"]
                        if e["kind"] == "xlsx_merged_range")
        self.assertEqual(ranges, ["A3:A4", "B3:B4"])
        self.assertEqual(rec["counts"]["merged_ranges_total"], 2)

    def test_disclosure_does_not_count_as_cells(self):
        with_merge = xlsx_parser.parse(_wb_with_merges(["A3:A4"]))
        without = xlsx_parser.parse(_wb_with_merges([]))
        self.assertEqual(with_merge["counts"]["cells_total"],
                         without["counts"]["cells_total"])

    def test_merge_cap_is_disclosed_not_silent(self):
        # 畸形超量合并区域：触顶必须留痕（note），不得静默截断
        merges = [f"C{r}:D{r}" for r in range(3, 3 + 1001)]
        data = _wb_with_merges(merges)
        rec = xlsx_parser.parse(data)
        merged = [e for e in rec["evidence"]
                  if e["kind"] == "xlsx_merged_range"]
        self.assertLessEqual(len(merged), 1000)
        self.assertTrue(any("合并区域" in n and ("上限" in n or "截断" in n)
                            for n in rec["notes"]),
                        f"触顶未留痕：{rec['notes']}")


if __name__ == "__main__":
    unittest.main()

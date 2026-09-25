# -*- coding: utf-8 -*-
"""A 线通宵修复回归：candidates 语义钉死（全部合成数据，无任何真实资料）。

覆盖四类缺陷回归：
  1. _outcome_value 中标结果语义：候选推荐≠最终中标、否决/废标≠普通落标、
     无效标独立分类、识别不了落 unknown（不许猜）。
  2. _xlsx_price_line_candidates 公式缓存值：cached_value 必须真正进入
     报价解析；无缓存/错误公式（#REF! 等）不得把公式串当数字。
  3. 纳税人识别号标签适配：冒号全半角/空格/无冒号变体；保留原文与
     工作表/行列证据位置；识别不了不猜；相似主体不合并。
  4. _xlsx_header_suggestions 作用域：重复表头、"下一表"分节、表尾合计行、
     多个采购包边界——旧表头不得套到后续所有行。

运行：
    cd app && /opt/homebrew/bin/python3 -m unittest \\
        biaojing.tests.test_candidate_regressions -v
"""

from __future__ import annotations

import unittest

from biaojing import candidates

SHA = "ab" * 32


def xcell(sheet, ref, value, cached=None, eid=None):
    """构造一条 xlsx_cell 合成证据（字段与 xlsx_parser.parse 输出一致）。"""
    item = {"kind": "xlsx_cell", "sheet": sheet, "cell": ref,
            "value": value, "locator": f"{sheet}!{ref}",
            "evidence_id": eid or f"E-{sheet}-{ref}"}
    if cached is not None or (isinstance(value, str)
                              and value.startswith("=")):
        item["cached_value"] = cached
    return item


def suggestion_cells(suggestions):
    """建议 → {(field, sheet!cell): value}，便于断言作用域。"""
    return {(item["field"], item["locator_display"]): item["value"]
            for item in suggestions}


# --------------------------------------------------------------- 中标结果语义

class OutcomeValueTests(unittest.TestCase):
    """任务 2：_outcome_value 六类语义钉死。"""

    def test_candidate_recommendation_is_not_final_win(self):
        for text in ("中标候选", "中标候选人", "第一中标候选人",
                     "候选推荐", "推荐中标候选人", "拟中标", "预中标"):
            self.assertEqual(candidates._outcome_value(text), "candidate",
                             text)

    def test_final_win_stays_won(self):
        for text in ("最终中标", "中标", "中标人", "中标单位", "已中标", "是"):
            self.assertEqual(candidates._outcome_value(text), "won", text)

    def test_rejection_is_not_ordinary_lost(self):
        for text in ("否决", "否决投标", "被否决", "废标"):
            result = candidates._outcome_value(text)
            self.assertEqual(result, "rejected", text)
            self.assertNotEqual(result, "lost", text)

    def test_invalid_bid_is_distinct(self):
        for text in ("无效", "无效标", "无效投标", "响应无效"):
            self.assertEqual(candidates._outcome_value(text), "invalid", text)

    def test_ordinary_lost_kept(self):
        for text in ("未中标", "未中", "落标", "未获得", "否"):
            self.assertEqual(candidates._outcome_value(text), "lost", text)

    def test_unrecognized_falls_to_unknown_not_guess(self):
        for text in ("评审中", "待定", "合格", "—", "", "见通知"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_outcome_semantics_via_header_suggestions(self):
        # 公开路径：中标结果列的候选推荐/否决不得被判成 won/lost
        evidence = [
            xcell("结果", "A1", "投标人名称"),
            xcell("结果", "B1", "中标结果"),
            xcell("结果", "A2", "甲公司"),
            xcell("结果", "B2", "中标候选人"),
            xcell("结果", "A3", "乙公司"),
            xcell("结果", "B3", "否决"),
            xcell("结果", "A4", "丙公司"),
            xcell("结果", "B4", "评审中"),
        ]
        found = suggestion_cells(candidates._xlsx_header_suggestions(evidence))
        self.assertEqual(found[("outcome_result", "结果!B2")], "candidate")
        self.assertEqual(found[("outcome_result", "结果!B3")], "rejected")
        self.assertEqual(found[("outcome_result", "结果!B4")], "unknown")


# ----------------------------------------------------------- 公式缓存值→报价

class PriceLineCachedValueTests(unittest.TestCase):
    """任务 3：cached_value 必须真正进入报价解析；坐标/单位/税口径带出。"""

    @staticmethod
    def build(rows):
        """rows: [(ref, value, cached)]；表头固定 A1 清单编码/B1 清单名称/
        C1 综合单价(元)/D1 投标人名称，数据在第 2 行起按 rows 给出。"""
        evidence = [xcell("报价", "A1", "清单编码"),
                    xcell("报价", "B1", "清单名称"),
                    xcell("报价", "C1", "综合单价(元)"),
                    xcell("报价", "D1", "投标人名称")]
        for ref, value, cached in rows:
            col = ref[0]
            row = ref[1:]
            label = {"A": "清单编码", "B": "清单名称",
                     "C": "综合单价(元)", "D": "投标人名称"}[col]
            evidence.append(xcell("报价", f"{col}1", label))
            evidence.append(xcell("报价", ref, value, cached=cached))
        # 去重表头（多行共用）
        uniq = {(e["cell"]): e for e in evidence}
        return list(uniq.values())

    def price_lines(self, evidence):
        return [c for c in candidates._xlsx_price_line_candidates(evidence)]

    def test_cached_formula_value_reaches_price_parsing(self):
        evidence = self.build([("A2", "X1", None), ("B2", "设备甲", None),
                               ("C2", "=C2*2", 617.5), ("D2", "甲公司", None)])
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1, groups)
        lines = groups[0]["value"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["unit_price"], "617.5")
        # 来源坐标正确带出：证据指向公式单元格本身
        self.assertEqual(lines[0]["evidence"], "E-报价-C2")
        self.assertEqual(lines[0]["field_evidence"]["unit_price"], "E-报价-C2")
        self.assertEqual(groups[0]["locator"],
                         {"kind": "xlsx_cell", "sheet": "报价", "cell": "C2"})
        self.assertEqual(groups[0]["locator_display"], "报价!C2")

    def test_formula_without_cache_is_never_parsed_as_number(self):
        evidence = self.build([("A2", "X1", None), ("B2", "设备甲", None),
                               ("C2", "=SUM(1234)", "unknown"),
                               ("D2", "甲公司", None)])
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1, "行应保留供人工核对")
        self.assertIsNone(groups[0]["value"][0]["unit_price"])

    def test_error_formula_cache_yields_no_price(self):
        evidence = self.build([
            ("A2", "X1", None), ("B2", "设备甲", None),
            ("C2", "=N(1234)", "#REF!"), ("D2", "甲公司", None),
            ("A3", "X2", None), ("B3", "设备乙", None),
            ("C3", "=A1&B1", "#VALUE!"), ("D3", "甲公司", None)])
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1)
        lines = groups[0]["value"]
        self.assertEqual(len(lines), 2)
        self.assertIsNone(lines[0]["unit_price"], "#REF! 不得解析出数字")
        self.assertIsNone(lines[1]["unit_price"], "#VALUE! 不得解析出数字")

    def test_formula_text_fields_use_cached_value(self):
        evidence = self.build([
            ("A2", "X1", None), ("B2", '=CONCAT("设备","乙")', "设备乙"),
            ("C2", 100, None), ("D2", "甲公司", None),
            ("A3", "X2", None), ("B3", "=B1", "#VALUE!"),
            ("C3", 110, None), ("D3", "甲公司", None)])
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1)
        lines = {line["item_name"]: line for line in groups[0]["value"]}
        self.assertEqual(lines["设备乙"]["item_name"], "设备乙")
        broken = lines["unknown"]
        self.assertEqual(broken["item_name"], "unknown")
        self.assertNotIn("item_name", broken["field_evidence"],
                         "错误缓存字段不得伪造证据映射")

    def test_formula_bidder_cell_uses_cache_and_unknown_is_skipped(self):
        evidence = self.build([
            ("A2", "X1", None), ("B2", "设备甲", None), ("C2", 100, None),
            ("D2", "=D1", "甲公司"),
            ("A3", "X2", None), ("B3", "设备乙", None), ("C3", 110, None),
            ("D3", "=D1", "unknown")])
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(groups[0]["locator_display"], "报价!C2")
        self.assertEqual([line["item_code"] for line in groups[0]["value"]],
                         ["X1"], "无缓存的主体公式行不得归属")

    def test_yuan_and_ten_thousand_yuan_unit_hints(self):
        yuan = self.build([("A2", "X1", None), ("B2", "设备甲", None),
                           ("C2", 1234.5, None), ("D2", "甲公司", None)])
        groups = self.price_lines(yuan)
        self.assertEqual(groups[0]["value"][0]["unit_price"], "1234.5")

        evidence = [xcell("报价", "A1", "清单编码"),
                    xcell("报价", "B1", "清单名称"),
                    xcell("报价", "C1", "综合单价(万元)"),
                    xcell("报价", "D1", "投标人名称"),
                    xcell("报价", "A2", "X1"), xcell("报价", "B2", "设备甲"),
                    xcell("报价", "C2", 1.5), xcell("报价", "D2", "甲公司")]
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1, "综合单价(万元) 列应可成组")
        self.assertEqual(groups[0]["value"][0]["unit_price"], "15000")

    def test_tax_included_flags_and_formula_cache(self):
        evidence = [xcell("报价", "A1", "清单编码"),
                    xcell("报价", "B1", "清单名称"),
                    xcell("报价", "C1", "综合单价(元)"),
                    xcell("报价", "D1", "投标人名称"),
                    xcell("报价", "E1", "是否含税")]
        rows = [("A2", "X1", None), ("B2", "设备甲", None), ("C2", 10, None),
                ("D2", "甲公司", None), ("E2", "含税", None),
                ("A3", "X2", None), ("B3", "设备乙", None), ("C3", 20, None),
                ("D3", "甲公司", None), ("E3", "不含税", None),
                ("A4", "X3", None), ("B4", "设备丙", None), ("C4", 30, None),
                ("D4", "甲公司", None), ("E4", "是", None),
                ("A5", "X4", None), ("B5", "设备丁", None), ("C5", 40, None),
                ("D5", "甲公司", None), ("E5", "否", None),
                ("A6", "X5", None), ("B6", "设备戊", None), ("C6", 50, None),
                ("D6", "甲公司", None),
                ("E6", '=IF(1,"是","否")', "是")]
        for ref, value, cached in rows:
            evidence.append(xcell("报价", ref, value, cached=cached))
        groups = self.price_lines(evidence)
        self.assertEqual(len(groups), 1)
        flags = {line["item_code"]: line["tax_included"]
                 for line in groups[0]["value"]}
        self.assertEqual(flags, {"X1": True, "X2": False, "X3": True,
                                 "X4": False, "X5": True})


# --------------------------------------------------------------- 标签适配

class TaxpayerIdTests(unittest.TestCase):
    """任务 4：纳税人识别号变体；原文与证据位置保留；不猜、不合并。"""

    def test_colon_and_spacing_variants_in_cells(self):
        evidence = [
            xcell("资料", "B2", "纳税人识别号：91110108660604228X"),
            xcell("资料", "B3", "纳税人识别号: 91110108660604228X"),
            xcell("资料", "B4", "纳税人识别号 ：91110108660604228X"),
            xcell("资料", "B5", "纳税人识别号91110108660604228X"),
        ]
        result = candidates.extract_candidates(SHA, "xlsx", evidence)
        found = [c for c in result if c["field"] == "taxpayer_id"]
        self.assertEqual(len(found), 4, found)
        for cand in found:
            # 原文保留：号码逐字符一致，不做任何改写或补全
            self.assertEqual(cand["value"], "91110108660604228X")
            self.assertEqual(cand["locator"]["kind"], "xlsx_cell")
            self.assertEqual(cand["locator"]["sheet"], "资料")
        cells = {c["locator"]["cell"] for c in found}
        self.assertEqual(cells, {"B2", "B3", "B4", "B5"})
        displays = {c["locator_display"] for c in found}
        self.assertEqual(displays,
                         {f"资料!B{i}" for i in (2, 3, 4, 5)})

    def test_no_colon_variant_in_paragraph_text(self):
        evidence = [{"kind": "docx_paragraph", "locator": "paragraph 3",
                     "paragraph_index": 3,
                     "text": "纳税人识别号 91110108660604228X，电话13800000001"}]
        result = candidates.extract_candidates(SHA, "docx", evidence)
        found = [c for c in result if c["field"] == "taxpayer_id"]
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["value"], "91110108660604228X")
        self.assertEqual(found[0]["locator"],
                         {"kind": "docx_paragraph", "paragraph_index": 3})
        others = {c["field"] for c in result if c["field"] != "taxpayer_id"}
        self.assertNotIn("uscc", others, "不得把识别号猜成其他字段")

    def test_unrecognizable_value_is_not_guessed(self):
        evidence = [xcell("资料", "B2", "纳税人识别号 见附件"),
                    xcell("资料", "B3", "纳税人识别号 110108123456789")]
        result = candidates.extract_candidates(SHA, "xlsx", evidence)
        self.assertEqual([c for c in result if c["field"] == "taxpayer_id"],
                         [], "识别不了必须留给人工，不许猜")

    def test_header_column_adaptation_keeps_sheet_and_cell(self):
        # 表头建议需要同行 ≥2 个已知标签，补一列投标人名称
        evidence = [xcell("资料", "A1", "纳税人识别号"),
                    xcell("资料", "B1", "投标人名称"),
                    xcell("资料", "A2", "91110108660604228X"),
                    xcell("资料", "B2", "甲公司"),
                    xcell("资料", "A3", "911101086606042297"),
                    xcell("资料", "B3", "乙公司")]
        result = candidates.extract_candidates(SHA, "xlsx", evidence)
        found = [c for c in result if c["field"] == "taxpayer_id"]
        self.assertEqual(len(found), 2, found)
        self.assertEqual({c["locator_display"] for c in found},
                         {"资料!A2", "资料!A3"})
        self.assertTrue(all("表头" in c["note"] for c in found))
        # 每条建议的定位必须是 typed xlsx_cell（工作表/单元格可回指）
        self.assertTrue(all(c["locator"] == {
            "kind": "xlsx_cell", "sheet": "资料",
            "cell": c["locator_display"].split("!")[1]} for c in found))

    def test_similar_bidders_are_not_merged(self):
        evidence = [xcell("报价", "A1", "清单编码"),
                    xcell("报价", "B1", "清单名称"),
                    xcell("报价", "C1", "综合单价(元)"),
                    xcell("报价", "D1", "投标人名称"),
                    xcell("报价", "A2", "X1"), xcell("报价", "B2", "设备甲"),
                    xcell("报价", "C2", 100),
                    xcell("报价", "D2", "华东科技"),
                    xcell("报价", "A3", "X2"), xcell("报价", "B3", "设备乙"),
                    xcell("报价", "C3", 110),
                    xcell("报价", "D3", "华东科技有限公司")]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 2, "相似主体必须各自成组")
        prices = sorted(int(group["value"][0]["unit_price"])
                        for group in groups)
        self.assertEqual(prices, [100, 110])


# ----------------------------------------------------------- 表头建议作用域

class HeaderSuggestionScopeTests(unittest.TestCase):
    """任务 5：重复表头/下一表/合计行/采购包边界——旧表头不套后续所有行。"""

    def test_repeated_header_rows_rebind_following_data(self):
        evidence = [
            xcell("明细", "A1", "清单编码"), xcell("明细", "B1", "清单名称"),
            xcell("明细", "C1", "综合单价(元)"),
            xcell("明细", "A2", "X1"), xcell("明细", "B2", "设备甲"),
            xcell("明细", "C2", 100),
            xcell("明细", "A3", "X2"), xcell("明细", "B3", "设备乙"),
            xcell("明细", "C3", 110),
            # 第二张表：同列位置语义变了——C 列变成总报价
            xcell("明细", "A5", "清单编码"), xcell("明细", "B5", "清单名称"),
            xcell("明细", "C5", "总报价(元)"),
            xcell("明细", "A6", "Y1"), xcell("明细", "B6", "服务甲"),
            xcell("明细", "C6", 999),
        ]
        found = suggestion_cells(
            candidates._xlsx_header_suggestions(evidence))
        self.assertEqual(found[("unit_price", "明细!C2")], 100)
        self.assertEqual(found[("unit_price", "明细!C3")], 110)
        self.assertNotIn(("unit_price", "明细!C6"), found,
                         "旧表头不得把第二张表的总报价当综合单价")
        self.assertIn(("total_price", "明细!C6"), found)
        total = found[("total_price", "明细!C6")]
        self.assertEqual(total["amount_yuan"], "999")

    def test_next_table_marker_stops_old_header(self):
        evidence = [
            xcell("分节", "A1", "清单名称"),
            xcell("分节", "B1", "综合单价(元)"),
            xcell("分节", "A2", "设备甲"), xcell("分节", "B2", 100),
            xcell("分节", "A3", "设备乙"), xcell("分节", "B3", 110),
            xcell("分节", "A4", "下一表"),
            xcell("分节", "A5", "服务甲"), xcell("分节", "B5", 55),
            xcell("分节", "A6", "服务乙"), xcell("分节", "B6", 66),
        ]
        found = suggestion_cells(
            candidates._xlsx_header_suggestions(evidence))
        self.assertEqual(found[("unit_price", "分节!B2")], 100)
        self.assertEqual(found[("unit_price", "分节!B3")], 110)
        self.assertNotIn(("unit_price", "分节!B5"), found,
                         "「下一表」之后无新表头，旧表头不得继续套用")
        self.assertNotIn(("unit_price", "分节!B6"), found)

    def test_footer_total_rows_get_no_suggestions(self):
        evidence = [
            xcell("明细", "A1", "清单编码"), xcell("明细", "B1", "清单名称"),
            xcell("明细", "C1", "综合单价(元)"),
            xcell("明细", "A2", "X1"), xcell("明细", "B2", "设备甲"),
            xcell("明细", "C2", 10),
            xcell("明细", "A3", "X2"), xcell("明细", "B3", "设备乙"),
            xcell("明细", "C3", 20),
            xcell("明细", "A4", "合计"), xcell("明细", "C4", 30),
            xcell("明细", "A5", "本页小计"), xcell("明细", "C5", 30),
        ]
        found = suggestion_cells(
            candidates._xlsx_header_suggestions(evidence))
        self.assertEqual(found[("unit_price", "明细!C2")], 10)
        self.assertEqual(found[("unit_price", "明细!C3")], 20)
        self.assertNotIn(("unit_price", "明细!C4"), found,
                         "合计行不得被当作数据行给建议")
        self.assertNotIn(("unit_price", "明细!C5"), found,
                         "小计行不得被当作数据行给建议")

    def test_procurement_package_boundary_and_no_cross_merge(self):
        evidence = [
            xcell("报价", "A1", "采购包一：设备采购"),
            xcell("报价", "A2", "投标人名称"),
            xcell("报价", "B2", "综合单价(元)"),
            xcell("报价", "A3", "甲公司"), xcell("报价", "B3", 100),
            xcell("报价", "A4", "乙公司"), xcell("报价", "B4", 120),
            xcell("报价", "A5", "采购包二：服务采购"),
            xcell("报价", "A6", "投标人名称"),
            xcell("报价", "B6", "总报价(元)"),
            xcell("报价", "A7", "丙公司"), xcell("报价", "B7", 999),
            xcell("报价", "A8", "丁公司"), xcell("报价", "B8", 888),
        ]
        suggestions = candidates._xlsx_header_suggestions(evidence)
        found = suggestion_cells(suggestions)
        self.assertEqual(found[("bidder_name", "报价!A3")], "甲公司")
        self.assertEqual(found[("unit_price", "报价!B3")], 100)
        self.assertEqual(found[("unit_price", "报价!B4")], 120)
        # 采购包二：只有自己的表头生效，包一的“综合单价”不得越界
        self.assertEqual(found[("bidder_name", "报价!A7")], "丙公司")
        self.assertIn(("total_price", "报价!B7"), found)
        self.assertNotIn(("unit_price", "报价!B7"), found,
                         "包二数据不得套用包一表头")
        self.assertNotIn(("unit_price", "报价!B8"), found)
        # 每个单元格的建议唯一，不因双表头重复产出（对原始列表查重）
        bidder_cells = [(s["field"], s["locator_display"])
                        for s in suggestions if s["field"] == "bidder_name"]
        self.assertEqual(len(bidder_cells), len(set(bidder_cells)))

    def test_price_lines_split_across_packages_despite_same_bidder(self):
        # 同一工作表、同名投标人在两个采购包：不得仅凭表名/名称合并
        evidence = [
            xcell("报价", "A1", "采购包一：设备采购"),
            xcell("报价", "A2", "清单编码"), xcell("报价", "B2", "清单名称"),
            xcell("报价", "C2", "综合单价(元)"),
            xcell("报价", "D2", "投标人名称"),
            xcell("报价", "A3", "S1"), xcell("报价", "B3", "设备甲"),
            xcell("报价", "C3", 100), xcell("报价", "D3", "华东科技"),
            xcell("报价", "A4", "S2"), xcell("报价", "B4", "设备乙"),
            xcell("报价", "C4", 110), xcell("报价", "D4", "华东科技"),
            xcell("报价", "A5", "采购包二：服务采购"),
            xcell("报价", "A6", "清单编码"), xcell("报价", "B6", "清单名称"),
            xcell("报价", "C6", "综合单价(元)"),
            xcell("报价", "D6", "投标人名称"),
            xcell("报价", "A7", "F1"), xcell("报价", "B7", "服务甲"),
            xcell("报价", "C7", 200), xcell("报价", "D7", "华东科技"),
            xcell("报价", "A8", "F2"), xcell("报价", "B8", "服务乙"),
            xcell("报价", "C8", 220), xcell("报价", "D8", "华东科技"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 2,
                         "不同采购包即使同表同名也不得合并成一组")
        prices = sorted(int(line["unit_price"])
                        for group in groups for line in group["value"])
        self.assertEqual(prices, [100, 110, 200, 220])
        # 组证据不得互相串：每组恰好 2 行，主定位指向本组首行单价
        for group, first_cell in zip(sorted(
                groups, key=lambda g: g["locator_display"]),
                ("报价!C3", "报价!C7")):
            self.assertEqual(len(group["value"]), 2)
            self.assertEqual(group["locator_display"], first_cell)


if __name__ == "__main__":
    unittest.main()

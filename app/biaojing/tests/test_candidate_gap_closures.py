# -*- coding: utf-8 -*-
"""F 线缺口封堵回归（全部合成数据，无任何真实资料）。

逐项钉死独立复核确认的开放缺口：
  1. _outcome_value 否向/疑问语义：否向线索（不/未/非/无/没/从未/并非/不是）
     或疑问线索（是否/吗/？/?/能否）出现时绝不映射 candidate/won；正反信号
     并存（候选标记+否定的中标）→unknown；否定的「中标」若非显性落标词
     （未中标/未中/落标/未获得）→unknown 而非 lost；rejected 只认明确词
     （否决/废标/被否决），裸「否」是落标不是否决；既有正确行为（未中标→
     lost、否决→rejected、无效→invalid、中标→won、候选→candidate、
     空/乱→unknown）全部保持。
  2. price_lines 组证据键：(sheet, section, bidder) 三元组——清单字段
     （item_code/item_name/spec）的证据 ID 必须进入顶层 evidence_ids，
     不得滞留 line.field_evidence（原二元组键写丢）。

运行：
    cd app && /opt/homebrew/bin/python3 -m unittest \\
        biaojing.tests.test_candidate_gap_closures -v
"""
from __future__ import annotations

import json
import unittest

from biaojing import candidates


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


# ------------------------------------------------- 缺口 1：outcome 否向/疑问

class OutcomeNegationQuestionTests(unittest.TestCase):
    """否向/疑问线索绝不映射 candidate/won；落标只认显性词；正反并存→unknown。"""

    def test_negated_win_is_never_won(self):
        for text in ("不是中标人", "未被认定为中标", "非中标人", "没中标",
                     "并非中标"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_candidate_marker_with_negation_is_unknown(self):
        # 正反信号并存（候选标记 + 否定的中标）→ unknown，绝不 candidate
        for text in ("候选但从未中标", "中标候选人但未定", "拟中标，未最终确定"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_question_forms_never_win_or_lose(self):
        for text in ("是否中标？", "中标吗", "能否中标", "中标?"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_rejected_requires_explicit_word_bare_fou_is_lost(self):
        for text in ("否决", "否决投标", "被否决", "废标"):
            self.assertEqual(candidates._outcome_value(text), "rejected", text)
        # 裸「否」= 未中标（既有行为），但绝不是否决
        self.assertEqual(candidates._outcome_value("否"), "lost")
        self.assertNotEqual(candidates._outcome_value("否"), "rejected")

    def test_explicit_lost_and_existing_pins_unchanged(self):
        for text in ("未中标", "未中", "落标", "未获得"):
            self.assertEqual(candidates._outcome_value(text), "lost", text)
        for text in ("最终中标", "中标", "中标人", "已中标", "是"):
            self.assertEqual(candidates._outcome_value(text), "won", text)
        for text in ("无效", "无效投标"):
            self.assertEqual(candidates._outcome_value(text), "invalid", text)
        for text in ("中标候选", "第一中标候选人", "拟中标", "预中标",
                     "推荐中标"):
            self.assertEqual(candidates._outcome_value(text), "candidate",
                             text)
        for text in ("", "评审中", "待定", "—", "见通知"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_negation_semantics_via_header_suggestions(self):
        # 公开路径：结果列的疑问/否向值不得给出 candidate/won/lost 候选
        evidence = [
            xcell("结果", "A1", "投标人名称"),
            xcell("结果", "B1", "中标结果"),
            xcell("结果", "A2", "甲公司"),
            xcell("结果", "B2", "是否中标？"),
            xcell("结果", "A3", "乙公司"),
            xcell("结果", "B3", "不是中标人"),
            xcell("结果", "A4", "丙公司"),
            xcell("结果", "B4", "候选但从未中标"),
            xcell("结果", "A5", "丁公司"),
            xcell("结果", "B5", "否决"),
        ]
        found = suggestion_cells(candidates._xlsx_header_suggestions(evidence))
        self.assertEqual(found[("outcome_result", "结果!B2")], "unknown")
        self.assertEqual(found[("outcome_result", "结果!B3")], "unknown")
        self.assertEqual(found[("outcome_result", "结果!B4")], "unknown")
        self.assertEqual(found[("outcome_result", "结果!B5")], "rejected")


# --------------------------------------------- 缺口 2：组证据三元组键

class GroupEvidenceKeyTests(unittest.TestCase):
    """清单字段（item_code/item_name/spec）证据必须进入顶层 evidence_ids。"""

    def test_item_field_evidence_ids_reach_top_level(self):
        evidence = [
            xcell("清单", "A1", "清单编码"), xcell("清单", "B1", "清单名称"),
            xcell("清单", "C1", "综合单价(元)"), xcell("清单", "D1", "投标人名称"),
            xcell("清单", "A2", "X1"), xcell("清单", "B2", "设备甲"),
            xcell("清单", "C2", 100), xcell("清单", "D2", "甲公司"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        ids = set(groups[0]["evidence_ids"])
        # 修复前：item 字段证据被写进 (sheet, bidder) 二元组键，输出端读
        # (sheet, section, bidder) 三元组键 → 清单证据从未进顶层
        self.assertIn("E-清单-A2", ids, "清单编码列证据必须进顶层 evidence_ids")
        self.assertIn("E-清单-B2", ids, "清单名称列证据必须进顶层 evidence_ids")
        self.assertIn("E-清单-C2", ids)
        self.assertIn("E-清单-D2", ids)
        # 行内 field_evidence 同样保留（双重定位不回退）
        self.assertEqual(groups[0]["value"][0]["field_evidence"]["item_code"],
                         "E-清单-A2")
        self.assertEqual(groups[0]["value"][0]["field_evidence"]["item_name"],
                         "E-清单-B2")

    def test_item_evidence_scoped_per_section_group(self):
        # 分节边界后同名单列的证据不得串组（三元组键的分节维度）
        evidence = [
            xcell("分包", "A1", "采购包一"),
            xcell("分包", "A2", "清单编码"), xcell("分包", "B2", "清单名称"),
            xcell("分包", "C2", "综合单价(元)"), xcell("分包", "D2", "投标人名称"),
            xcell("分包", "A3", "X1"), xcell("分包", "B3", "设备甲"),
            xcell("分包", "C3", 100), xcell("分包", "D3", "甲公司"),
            xcell("分包", "A5", "采购包二"),
            xcell("分包", "A6", "清单编码"), xcell("分包", "B6", "清单名称"),
            xcell("分包", "C6", "综合单价(元)"), xcell("分包", "D6", "投标人名称"),
            xcell("分包", "A7", "Y1"), xcell("分包", "B7", "服务甲"),
            xcell("分包", "C7", 200), xcell("分包", "D7", "甲公司"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 2, "不同采购包同投标人不得合并")
        by_first_price = {group["locator_display"]: group for group in groups}
        ids_low = set(by_first_price["分包!C3"]["evidence_ids"])
        ids_high = set(by_first_price["分包!C7"]["evidence_ids"])
        self.assertIn("E-分包-A3", ids_low)
        self.assertIn("E-分包-B3", ids_low)
        self.assertNotIn("E-分包-A7", ids_low, "包二证据不得串进包一组")
        self.assertIn("E-分包-A7", ids_high)
        self.assertIn("E-分包-B7", ids_high)
        self.assertNotIn("E-分包-A3", ids_high, "包一证据不得串进包二组")


class PriceLineUnitDisclosureTests(unittest.TestCase):
    def test_formula_without_cache_is_visible_in_group_note(self):
        evidence = [
            xcell("公式待核", "A1", "投标人"),
            xcell("公式待核", "B1", "综合单价(元)"),
            xcell("公式待核", "C1", "清单名称"),
            xcell("公式待核", "D1", "规格型号"),
            xcell("公式待核", "A2", "甲公司"),
            xcell("公式待核", "B2", "=Z9"),
            xcell("公式待核", "C2", "电线"),
            xcell("公式待核", "D2", "BV"),
        ]
        group = candidates._xlsx_price_line_candidates(evidence)[0]
        self.assertIsNone(group["value"][0]["unit_price"])
        self.assertIn("公式待核!B2", group["note"])
        self.assertIn("缓存值缺失或无效", group["note"])

    def test_formula_cache_is_used_with_recalculation_warning(self):
        evidence = [
            xcell("公式缓存待核", "A1", "投标人"),
            xcell("公式缓存待核", "B1", "综合单价(元)"),
            xcell("公式缓存待核", "C1", "清单名称"),
            xcell("公式缓存待核", "D1", "规格型号"),
            xcell("公式缓存待核", "A2", "甲公司"),
            xcell("公式缓存待核", "B2", "=100+20", cached=120),
            xcell("公式缓存待核", "C2", "电线"),
            xcell("公式缓存待核", "D2", "BV"),
        ]
        group = candidates._xlsx_price_line_candidates(evidence)[0]
        self.assertEqual(group["value"][0]["unit_price"], "120")
        self.assertIn("公式缓存待核!B2", group["note"])
        self.assertIn("采用公式缓存值 120", group["note"])
        self.assertIn("可能尚未重新计算", group["note"])

    def test_missing_unit_is_visible_in_group_note(self):
        evidence = [
            xcell("单位待核", "A1", "投标人"),
            xcell("单位待核", "B1", "综合单价"),
            xcell("单位待核", "C1", "清单名称"),
            xcell("单位待核", "D1", "规格型号"),
            xcell("单位待核", "A2", "甲公司"),
            xcell("单位待核", "B2", 53.8),
            xcell("单位待核", "C2", "电线"),
            xcell("单位待核", "D2", "BV"),
        ]
        group = candidates._xlsx_price_line_candidates(evidence)[0]
        self.assertIsNone(group["value"][0]["unit_price"])
        self.assertIn("单位未知", group["note"])
        self.assertIn("单位待核!B2", group["note"])
        self.assertIn("不进入 R004", group["note"])

    def test_yuan_header_currency_hint_matches_total_price(self):
        evidence = [
            xcell("币种口径", "A1", "投标人"),
            xcell("币种口径", "B1", "综合单价(元)"),
            xcell("币种口径", "C1", "清单名称"),
            xcell("币种口径", "D1", "规格型号"),
            xcell("币种口径", "E1", "总报价(元)"),
            xcell("币种口径", "A2", "甲公司"),
            xcell("币种口径", "B2", 100),
            xcell("币种口径", "C2", "电线"),
            xcell("币种口径", "D2", "BV"),
            xcell("币种口径", "E2", 200),
        ]
        group = candidates._xlsx_price_line_candidates(evidence)[0]
        line = group["value"][0]
        self.assertEqual(line["currency"], "CNY")
        self.assertEqual(line["field_evidence"]["currency"],
                         "E-币种口径-B1")
        self.assertIn("由表头", group["note"])
        total = next(item for item in candidates._xlsx_header_suggestions(evidence)
                     if item["field"] == "total_price")
        self.assertEqual(total["currency_hint"], line["currency"])


# --------------------------------------------- 缺口 3：币种归一白名单

def currency_sheet(values):
    """同一投标人多行、币种列取值各异的合成表（A-E 列）。"""
    evidence = [
        xcell("币种", "A1", "投标人"), xcell("币种", "B1", "综合单价(元)"),
        xcell("币种", "C1", "清单名称"), xcell("币种", "D1", "规格型号"),
        xcell("币种", "E1", "币种"),
    ]
    for idx, cur in enumerate(values, start=2):
        evidence += [
            xcell("币种", f"A{idx}", "甲公司"),
            xcell("币种", f"B{idx}", 100 + idx),
            xcell("币种", f"C{idx}", "电缆"),
            xcell("币种", f"D{idx}", "YJV"),
            xcell("币种", f"E{idx}", cur),
        ]
    return evidence


class CurrencyNormalizationTests(unittest.TestCase):
    """P1-6：常见人民币写法→CNY；标准外币代码大写保留；乱写→unknown。"""

    def test_rmb_variants_all_normalize_to_cny_same_group(self):
        evidence = currency_sheet(("RMB", "rmb", "人民币", "人民币(元)",
                                   "人民币（元）", "￥", "¥", "元", "CNY"))
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        currencies = [line["currency"] for line in groups[0]["value"]]
        self.assertEqual(currencies, ["CNY"] * 9,
                         "全部人民币写法必须归一 CNY，不得按原串分裂可比组")

    def test_standard_currency_codes_preserved_uppercase(self):
        evidence = currency_sheet(("usd", "EUR", "jpy", "HKD", "gbp"))
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1)
        currencies = [line["currency"] for line in groups[0]["value"]]
        self.assertEqual(currencies, ["USD", "EUR", "JPY", "HKD", "GBP"])

    def test_unrecognized_currency_becomes_unknown_never_raw(self):
        evidence = currency_sheet(("美金块", "RMB-", "＄"))
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1)
        currencies = {line["currency"] for line in groups[0]["value"]}
        self.assertEqual(currencies, {"unknown"},
                         "不识别币种一律 unknown，绝不留自定义字符串进 comparable_key")
        dumped = json.dumps(groups[0], ensure_ascii=False)
        self.assertNotIn("美金块", dumped)

    def test_field_evidence_kept_for_unknown_currency(self):
        evidence = currency_sheet(("美金块",))
        groups = candidates._xlsx_price_line_candidates(evidence)
        line = groups[0]["value"][0]
        self.assertEqual(line["currency"], "unknown")
        self.assertEqual(line["field_evidence"]["currency"], "E-币种-E2",
                         "归一为 unknown 也要保留原单元格证据便于人工核对")


# --------------------------------- 缺口 4：同表头行重复同名字段

class DuplicateHeaderColumnTests(unittest.TestCase):
    """P2-7：同一逻辑表头行内同字段多列，不得静默取末列。"""

    def test_duplicate_price_columns_never_autopick(self):
        # B/D 两列都叫「综合单价(元)」，真价 100（B）、干扰 999（D）
        evidence = [
            xcell("重复列", "A1", "投标人"), xcell("重复列", "B1", "综合单价(元)"),
            xcell("重复列", "C1", "清单名称"), xcell("重复列", "D1", "综合单价(元)"),
            xcell("重复列", "E1", "规格型号"),
            xcell("重复列", "A2", "甲公司"), xcell("重复列", "B2", 100),
            xcell("重复列", "C2", "电线"), xcell("重复列", "D2", 999),
            xcell("重复列", "E2", "BV-2.5"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        lines = groups[0]["value"]
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0]["unit_price"],
                          "重复单价列不得自动取任一列（原实现静默取末列 999）")
        self.assertNotIn("unit_price", lines[0]["field_evidence"])
        dumped = json.dumps(groups[0], ensure_ascii=False)
        self.assertNotIn("999", dumped, "干扰值不得以任何形态进入候选")
        note = groups[0]["note"]
        self.assertIn("表头重复", note)
        self.assertIn("unit_price", note)
        self.assertIn("B、D", note)
        self.assertIn("人工", note)

    def test_duplicate_text_columns_take_first_with_disclosure(self):
        evidence = [
            xcell("重文名", "A1", "投标人"), xcell("重文名", "B1", "综合单价(元)"),
            xcell("重文名", "C1", "清单名称"), xcell("重文名", "D1", "清单名称"),
            xcell("重文名", "E1", "规格型号"),
            xcell("重文名", "A2", "甲公司"), xcell("重文名", "B2", 100),
            xcell("重文名", "C2", "电线"), xcell("重文名", "D2", "电缆"),
            xcell("重文名", "E2", "BV-2.5"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        line = groups[0]["value"][0]
        self.assertEqual(line["item_name"], "电线",
                         "文本重复列取第一列，绝不静默取末列")
        self.assertEqual(line["field_evidence"]["item_name"], "E-重文名-C2")
        note = groups[0]["note"]
        self.assertIn("表头重复", note)
        self.assertIn("item_name", note)
        self.assertIn("C、D", note)


# ------------------------------------- 缺口 5：双层表头 + 全空行边界

class TwoTierHeaderTests(unittest.TestCase):
    """P2-9a：相邻标签行合并为逻辑表头；无法安全合并时必须披露。"""

    def test_two_tier_header_rows_merge_into_one_logical_header(self):
        # repro_b Sheet3 场景：r1 投标人/报价明细/清单名称/规格型号，
        # r2 仅 B 列「综合单价(元)」，r3 起为数据行
        evidence = [
            xcell("双层", "A1", "投标人"), xcell("双层", "B1", "报价明细"),
            xcell("双层", "C1", "清单名称"), xcell("双层", "D1", "规格型号"),
            xcell("双层", "B2", "综合单价(元)"),
            xcell("双层", "A3", "甲公司"), xcell("双层", "B3", 100),
            xcell("双层", "C3", "电线"), xcell("双层", "D3", "BV-2.5"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1,
                         "双层表头必须合并后成组，不得静默归零")
        line = groups[0]["value"][0]
        self.assertEqual(line["unit_price"], "100")
        self.assertEqual(line["item_name"], "电线")
        found = suggestion_cells(candidates._xlsx_header_suggestions(evidence))
        self.assertEqual(found[("unit_price", "双层!B3")], 100)
        self.assertEqual(found[("bidder_name", "双层!A3")], "甲公司")
        self.assertNotIn(("unit_price", "双层!B2"), found,
                         "第二层表头行自身不得再当数据行给建议")

    def test_unmergeable_two_tier_rows_disclose_not_silent_zero(self):
        # r1/r2 相邻标签行字段重叠（两行都有综合单价）→ 无法安全合并，
        # 也不得静默归零，必须输出「疑似双层表头，未自动成组」披露
        evidence = [
            xcell("疑双层", "A1", "投标人"), xcell("疑双层", "B1", "综合单价(元)"),
            xcell("疑双层", "B2", "综合单价(元)"), xcell("疑双层", "C2", "清单名称"),
            xcell("疑双层", "A3", "甲公司"), xcell("疑双层", "B3", 100),
            xcell("疑双层", "C3", "电线"),
        ]
        results = candidates._xlsx_price_line_candidates(evidence)
        groups = [c for c in results if c["field"] == "price_lines"]
        self.assertEqual(groups, [], "字段重叠的两行不得自动成组")
        disclosures = [c for c in results if c["field"] == "header_structure"]
        self.assertTrue(disclosures, "疑似双层表头必须披露，不许静默归零")
        note = disclosures[0]["note"]
        self.assertIn("疑似双层表头", note)
        self.assertIn("未自动成组", note)
        self.assertEqual(disclosures[0]["value"]["sheet"], "疑双层")
        self.assertEqual(disclosures[0]["value"]["rows"], [1, 2])

    def test_data_row_below_header_never_merges(self):
        # 数据行恰好含一个表头标签样文本时：护栏（下行非空值须全为表头标签）
        # 拒绝合并，且不得把数据行当第二层表头吃掉
        evidence = [
            xcell("护栏", "A1", "投标人"), xcell("护栏", "B1", "综合单价(元)"),
            xcell("护栏", "C1", "清单名称"), xcell("护栏", "D1", "规格型号"),
            xcell("护栏", "A2", "甲公司"), xcell("护栏", "B2", 100),
            xcell("护栏", "C2", "单位"), xcell("护栏", "D2", "BV"),
            xcell("护栏", "A3", "甲公司"), xcell("护栏", "B3", 120),
            xcell("护栏", "C3", "电缆"), xcell("护栏", "D3", "YJV"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(len(groups[0]["value"]), 2, "两行数据都必须保留")
        prices = sorted(int(line["unit_price"]) for line in groups[0]["value"])
        self.assertEqual(prices, [100, 120])


class BlankRowScopeTests(unittest.TestCase):
    """P2-9b：表头作用域在全空分隔行处截断；表内偶发空单元格不误伤。"""

    def test_blank_separator_row_truncates_header_scope(self):
        evidence = [
            xcell("空行界", "A1", "投标人"), xcell("空行界", "B1", "综合单价(元)"),
            xcell("空行界", "C1", "清单名称"), xcell("空行界", "D1", "规格型号"),
            xcell("空行界", "A2", "甲公司"), xcell("空行界", "B2", 100),
            xcell("空行界", "C2", "电线"), xcell("空行界", "D2", "BV"),
            # 第 3 行整行全空（无任何证据单元格）
            xcell("空行界", "A4", "甲公司"), xcell("空行界", "B4", 55),
            xcell("空行界", "C4", "服务甲"), xcell("空行界", "D4", "SCB"),
        ]
        found = suggestion_cells(candidates._xlsx_header_suggestions(evidence))
        self.assertIn(("unit_price", "空行界!B2"), found)
        self.assertNotIn(("unit_price", "空行界!B4"), found,
                         "全空分隔行之后旧表头不得继续套用")
        self.assertNotIn(("bidder_name", "空行界!A4"), found)
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(len(groups[0]["value"]), 1,
                         "空行后的行不得再归入旧表头的组")
        self.assertEqual(groups[0]["value"][0]["item_name"], "电线")

    def test_sparse_rows_inside_table_do_not_truncate(self):
        # 行内个别列空（非整行空）不得截断作用域
        evidence = [
            xcell("稀疏", "A1", "投标人"), xcell("稀疏", "B1", "综合单价(元)"),
            xcell("稀疏", "C1", "清单名称"), xcell("稀疏", "D1", "规格型号"),
            xcell("稀疏", "A2", "甲公司"), xcell("稀疏", "B2", 100),
            xcell("稀疏", "C2", "电线"),  # D2 空
            xcell("稀疏", "A3", "甲公司"), xcell("稀疏", "B3", 200),
            xcell("稀疏", "C3", "开关"), xcell("稀疏", "D3", "10A"),
        ]
        groups = candidates._xlsx_price_line_candidates(evidence)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(len(groups[0]["value"]), 2,
                         "偶发空单元格行不得被误当作分隔行")
        prices = sorted(int(line["unit_price"]) for line in groups[0]["value"])
        self.assertEqual(prices, [100, 200])


class OutcomeNegatedPolarityTests(unittest.TestCase):
    """复核四轮补充：否决/无效分支同样受否向与疑问守卫约束。

    否定的「否决/无效」与对它们的提问一律 unknown——rejected/invalid
    只能由无否向、无疑问的明确表述产生；显性落标词（未中标/落标/未获得）
    的既有映射不受影响。
    """

    def test_negated_rejected_is_unknown(self):
        for text in ("未被否决", "没否决", "并非否决", "不是否决"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_question_about_rejection_is_unknown(self):
        for text in ("是否被否决？", "是否否决", "被否决吗", "能否否决"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_negated_invalid_is_unknown(self):
        for text in ("并非无效", "不是无效", "未无效", "无无效"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)

    def test_clear_rejected_and_invalid_unchanged(self):
        for text in ("否决", "被否决", "废标", "否决投标"):
            self.assertEqual(candidates._outcome_value(text), "rejected", text)
        self.assertEqual(candidates._outcome_value("无效"), "invalid")
        self.assertEqual(candidates._outcome_value("无效投标"), "invalid")

    def test_lexicalized_lost_terms_keep_mapping(self):
        for text in ("未中标", "未中", "落标", "未获得"):
            self.assertEqual(candidates._outcome_value(text), "lost", text)

    def test_asked_or_negated_lost_terms_are_unknown(self):
        for text in ("是否未中标？", "没有未中标", "并非未中标", "落标吗？"):
            self.assertEqual(candidates._outcome_value(text), "unknown", text)


if __name__ == "__main__":
    unittest.main()

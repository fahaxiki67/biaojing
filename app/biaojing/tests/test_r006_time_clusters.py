# -*- coding: utf-8 -*-
"""标镜 R006 时间聚集回归：创建时间相近但不完全相同、同日聚集。

钉死口径（通宵任务 B 线）：
  - 相近（≤窗口秒数）但不完全相同的创建时间可成簇，信号一律"待核信号"，
    不得升为"线索"，更不得写成结论；
  - 同日聚集为更弱信号，同样待核；仅有日期（无时间部分）不得声称秒级相近；
  - 输出保留 Creator 与 Producer、原始时间、时区标注、规范化时间与窗口理由；
  - 披露替代解释（平台转码/另存为、批量导出、统一模板/扫描平台、同版软件）；
  - 不得由相同软件型号/版本推断同一台物理设备；
  - 时间一律是文档内元数据创建时间，不得把文件系统导入/复制时间当原件创建时间；
  - naive 与 aware 混用时不得跨类比较（保守不成簇）；
  - 单一投标人的多个文件时间再近也不触发；creation_date unknown 不参与；
  - 旧的完全相等分组行为保持兼容，且同一批文件不因聚簇重复上报。

运行：
    cd app && python3 -m unittest biaojing.tests.test_r006_time_clusters -v
"""

from __future__ import annotations

import unittest

from biaojing.rules import SCHEMA, FactBase, screen_all

SHA = "a" * 64


def ev(eid: str) -> dict:
    return {"evidence_id": eid, "file_sha256": SHA, "source_type": "project_scan",
            "role": "bidder", "quote": "合成原文",
            "locator": {"kind": "xlsx_cell", "sheet": "S", "cell": "A1"}}


def bid(bidder_id: str, **kw) -> dict:
    b = {"bidder_id": bidder_id, "bidder_name": f"主体{bidder_id}",
         "role": "bidder", "evidence": f"E-bid-{bidder_id}",
         "outcome": {"status": "unknown"}}
    b.update(kw)
    return b


def screen(data: dict, thresholds: dict | None = None) -> dict:
    return screen_all(FactBase.from_dict(data), thresholds)


def by_rule(result: dict, rule_id: str) -> list[dict]:
    return [f for f in result["findings"] if f["rule_id"] == rule_id]


def cluster_result(file_specs: list[dict], thresholds: dict | None = None) -> dict:
    """file_specs: [{metadata, source_type?}]，逐项生成 B1..Bn 的单文件投标。"""
    bids, evidence = [], []
    for i, spec in enumerate(file_specs, start=1):
        bidder = f"B{i}"
        bids.append(bid(bidder, files=[{
            "file_ref": f"{bidder.lower()}.pdf", "sha256": SHA[:62] + f"{i:02d}",
            "source_type": spec.get("source_type", "project_scan"),
            "context": "bid_document",
            "metadata": spec["metadata"], "evidence": f"E-extra-{i}"}]))
        evidence += [ev(f"E-bid-{bidder}"), ev(f"E-extra-{i}")]
    data = {"schema": SCHEMA,
            "events": [{"event_id": "EV-1", "project_id": "P-1",
                        "event_name": "合成事件",
                        "lots": [{"lot_id": "L1", "lot_name": "一标段",
                                  "bids": bids}]}],
            "evidence": evidence}
    return screen(data, thresholds)


class R006TimeClusterTests(unittest.TestCase):

    def test_near_but_not_equal_times_cluster(self):
        # 相差 70 秒（≤120 秒窗口）、producer 不同：应有一条时间相近聚簇
        out = cluster_result([
            {"metadata": {"producer": "软件甲", "creator": "编制一",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件乙", "creator": "编制二",
                          "creation_date": "2026-03-01T09:01:10"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        f = fs[0]
        self.assertEqual(f["signal"], "待核信号")  # 不完全相同不得升为线索
        self.assertEqual(f["params"]["cluster_mode"], "tight_window")
        self.assertTrue(f["params"]["window_rationale"])
        self.assertEqual(sorted(f["scope"]["bidder_ids"]), ["B1", "B2"])
        self.assertIn("70", f["trigger_reason"])

    def test_same_day_cluster_is_weak_signal(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T08:00:00"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "2026-03-01T15:30:00"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["params"]["cluster_mode"], "same_day")
        self.assertEqual(fs[0]["signal"], "待核信号")
        self.assertIn("同日", fs[0]["trigger_reason"])

    def test_creator_producer_raw_time_timezone_normalized_all_kept(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲", "creator": "编制一",
                          "creation_date": "2026-03-01T09:00:00+08:00"}},
            {"metadata": {"producer": "软件乙", "creator": "编制二",
                          "creation_date": "2026-03-01T01:00:30Z"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)  # UTC 归一后相差 30 秒 → 相近
        files = fs[0]["inputs"]["files"]
        by_ref = {x["file_ref"]: x for x in files}
        for ref, (creator, producer, raw) in (
                ("b1.pdf", ("编制一", "软件甲", "2026-03-01T09:00:00+08:00")),
                ("b2.pdf", ("编制二", "软件乙", "2026-03-01T01:00:30Z"))):
            rec = by_ref[ref]
            self.assertEqual(rec["creator"], creator)
            self.assertEqual(rec["producer"], producer)
            self.assertEqual(rec["creation_date"], raw)  # 原始时间原文保留
            self.assertTrue(rec.get("timezone"))
            self.assertTrue(rec.get("normalized_time"))
        # 时区如实分别标注：+08:00 与 UTC 不同
        self.assertNotEqual(by_ref["b1.pdf"]["timezone"],
                            by_ref["b2.pdf"]["timezone"])

    def test_mixed_aware_naive_never_compared(self):
        # 一个带时区、一个不带：不得跨类比较，保守不成簇
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00+08:00"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "2026-03-01T09:00:00"}},
        ])
        self.assertEqual(by_rule(out, "R006"), [])

    def test_single_bidder_never_clusters(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:10"}},
        ][:1])  # 只有 B1 一家
        self.assertEqual(by_rule(out, "R006"), [])
        # 同一投标人两份文件时间再近也不触发
        bidder = bid("B1", files=[
            {"file_ref": "a.pdf", "sha256": SHA, "source_type": "project_scan",
             "context": "bid_document",
             "metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"},
             "evidence": "E-extra-1"},
            {"file_ref": "b.pdf", "sha256": "b" * 64,
             "source_type": "project_scan", "context": "bid_document",
             "metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:10"},
             "evidence": "E-extra-2"}])
        data = {"schema": SCHEMA,
                "events": [{"event_id": "EV-1", "project_id": "P-1",
                            "event_name": "合成事件",
                            "lots": [{"lot_id": "L1", "lot_name": "一标段",
                                      "bids": [bidder]}]}],
                "evidence": [ev("E-bid-B1"), ev("E-extra-1"), ev("E-extra-2")]}
        self.assertEqual(by_rule(screen(data), "R006"), [])

    def test_unknown_time_excluded_from_clusters(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件乙", "creation_date": "unknown"}},
        ])
        self.assertEqual(by_rule(out, "R006"), [])

    def test_device_and_import_time_disclosure(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:40"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        limitations = " ".join(fs[0]["limitations"])
        self.assertIn("物理设备", limitations)   # 不得推断同型号=同一台设备
        self.assertIn("导入", limitations)        # 导入/复制时间≠原件创建时间
        alternatives = " ".join(fs[0]["alternative_explanations"])
        self.assertTrue(("转码" in alternatives or "另存" in alternatives),
                        f"替代解释缺平台转码/另存为：{alternatives}")
        self.assertIn("document_internal", fs[0]["params"]["time_source"])

    def test_exact_group_not_duplicated_by_cluster(self):
        # 完全相等的 (producer, creation_date) 走旧分组，同一批文件
        # 不得再额外产出聚簇 finding
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertNotIn("cluster_mode", fs[0]["params"])

    def test_date_only_same_day_no_seconds_claim(self):
        # 只有日期没有时间：可同日聚集，但不得声称秒级相近
        out = cluster_result([
            {"metadata": {"producer": "软件甲", "creation_date": "2026-03-01"}},
            {"metadata": {"producer": "软件乙", "creation_date": "2026-03-01"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["params"]["cluster_mode"], "same_day")
        self.assertNotIn("秒", fs[0]["trigger_reason"])

    def test_custom_window_threshold_respected(self):
        th = {"R006": {"near_window_seconds": 30}}
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "2026-03-01T09:01:10"}},
        ], thresholds=th)
        fs = by_rule(out, "R006")
        # 70 秒超出 30 秒窗口：不得判 tight，只保留同日弱信号
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["params"]["cluster_mode"], "same_day")

    def test_exact_group_still_works(self):
        # 旧接口兼容：非通用 producer + 完全相同元数据仍是"线索"
        out = cluster_result([
            {"metadata": {"producer": "凌云扫描仪 3.2",
                          "creation_date": "2026-01-01T00:00:00"}},
            {"metadata": {"producer": "凌云扫描仪 3.2",
                          "creation_date": "2026-01-01T00:00:00"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "线索")


if __name__ == "__main__":
    unittest.main()


class R006PdfDateAndSpanTests(unittest.TestCase):
    """独立复核补充：PDF 日期格式与窗口跨度口径。"""

    def test_pdf_date_format_nearby_times_cluster(self):
        # 真实 PDF 解析器暴露 D:YYYYMMDDHHmmSS+HH'mm' 格式，必须可解析
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "D:20260301090000+08'00'"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "D:20260301090110+08'00'"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        f = fs[0]
        self.assertEqual(f["params"]["cluster_mode"], "tight_window")
        self.assertIn("70", f["trigger_reason"])
        files = {x["file_ref"]: x for x in f["inputs"]["files"]}
        # 原始文本逐字保留；时区标注保留 +08:00；规范化时间为 UTC
        self.assertEqual(files["b1.pdf"]["creation_date"],
                         "D:20260301090000+08'00'")
        self.assertIn("+08", files["b1.pdf"]["timezone"])
        self.assertTrue(files["b1.pdf"]["normalized_time"]
                        .startswith("2026-03-01T01:00:00"))

    def test_pdf_date_z_and_offset_compared_in_utc(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "D:20260301010000Z"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "D:20260301090110+08'00'"}},
        ])
        fs = by_rule(out, "R006")
        # Z = UTC 01:00:00；+08'00' = UTC 01:01:10 → 相差 70 秒
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["params"]["cluster_mode"], "tight_window")
        self.assertIn("70", fs[0]["trigger_reason"])

    def test_pdf_date_only_same_day_no_seconds_claim(self):
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "D:20260301"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "D:20260301"}},
        ])
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["params"]["cluster_mode"], "same_day")
        self.assertNotIn("秒", fs[0]["trigger_reason"])

    def test_chained_cluster_span_must_fit_window(self):
        # 链式相邻（110s+110s）不等于整簇相近：全簇跨度 220s 不得声称
        # 处于 120 秒窗口；切分后前两台 tight，第三台只进同日弱信号
        out = cluster_result([
            {"metadata": {"producer": "软件甲",
                          "creation_date": "2026-03-01T09:00:00"}},
            {"metadata": {"producer": "软件乙",
                          "creation_date": "2026-03-01T09:01:50"}},
            {"metadata": {"producer": "软件丙",
                          "creation_date": "2026-03-01T09:03:40"}},
        ])
        fs = by_rule(out, "R006")
        tights = [f for f in fs if f["params"]["cluster_mode"] == "tight_window"]
        self.assertEqual(len(tights), 1)
        self.assertEqual(sorted(tights[0]["scope"]["bidder_ids"]), ["B1", "B2"])
        self.assertIn("110", tights[0]["trigger_reason"])
        self.assertNotIn("220", tights[0]["trigger_reason"])
        sames = [f for f in fs if f["params"]["cluster_mode"] == "same_day"]
        self.assertEqual(len(sames), 1)
        self.assertEqual(sorted(sames[0]["scope"]["bidder_ids"]),
                         ["B1", "B2", "B3"])

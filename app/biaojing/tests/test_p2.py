# -*- coding: utf-8 -*-
"""标镜 P2 测试：R001—R008 触发例/反例/unknown 边界例、SQLite 幂等与冲突、
证据引用可解析、筛查 CLI。测试库一律位于临时目录。

运行：
    cd app && python3 -m unittest biaojing.tests.test_p2 -v
"""

from __future__ import annotations

import io
import copy
import json
import os
import statistics
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from biaojing import rules, store
from biaojing.rules import FactBase, SCHEMA, screen_all

SHA = "a" * 64


# ---------------------------------------------------------------- builders

def ev(eid: str, quote: str = "合成原文", sha: str = SHA, source: str = "unknown",
       role: str = "bidder", locator: dict | None = None) -> dict:
    return {"evidence_id": eid, "file_sha256": sha, "source_type": source,
            "role": role, "quote": quote,
            "locator": locator or {"kind": "xlsx_cell", "sheet": "S", "cell": "A1"}}


def bid(bidder_id: str, **kw) -> dict:
    b = {"bidder_id": bidder_id, "bidder_name": f"主体{bidder_id}",
         "role": kw.pop("role", "bidder"),
         "evidence": kw.pop("evidence", f"E-bid-{bidder_id}"),
         "outcome": kw.pop("outcome", {"status": "unknown"})}
    b.update(kw)
    return b


def lot(lot_id: str, bids: list[dict], lot_name: str = "一标段") -> dict:
    return {"lot_id": lot_id, "lot_name": lot_name, "bids": bids}


def event(event_id: str, lots: list[dict], project_id: str = "P-1",
          event_name: str = "") -> dict:
    return {"event_id": event_id, "project_id": project_id,
            "event_name": event_name or f"事件{event_id}", "lots": lots}


def facts(*events, evidence: list[dict] | None = None) -> dict:
    return {"schema": SCHEMA, "events": list(events),
            "evidence": evidence if evidence is not None else []}


def screen(data: dict) -> dict:
    return screen_all(FactBase.from_dict(data))


def by_rule(result: dict, rule_id: str) -> list[dict]:
    return [f for f in result["findings"] if f["rule_id"] == rule_id]


EVIDENCE_IDS_DEFAULT = [f"E-bid-{x}" for x in ("B1", "B2", "B3")]


def default_evidence() -> list[dict]:
    return [ev(e) for e in ["E-bid-B1", "E-bid-B2", "E-bid-B3"]] + [
        ev(f"E-extra-{i}") for i in range(20)]


# ---------------------------------------------------------------- R001

class R001Tests(unittest.TestCase):
    def base(self, file_extra: dict, bidder_extra: dict | None = None) -> dict:
        b1 = bid("B1", uscc="91500100MA1", uscc_evidence="E-uscc-B1",
                 files=[{"file_ref": "a.pdf", "sha256": SHA,
                         "source_type": "unknown", "context": "bid_document",
                         "evidence": "E-file-1", **file_extra}])
        if bidder_extra:
            b1.update(bidder_extra)
        return facts(
            event("EV-1", [lot("L1", [b1, bid("B2")])]),
            evidence=default_evidence() + [ev("E-file-1"), ev("E-uscc-B1")])

    def test_trigger_owner_conflict(self):
        out = screen(self.base({"declared_owner_id": "B2"}))
        fs = by_rule(out, "R001")
        self.assertEqual(len(fs), 1)
        self.assertIn("B2", fs[0]["trigger_reason"])
        self.assertIn("E-file-1", fs[0]["evidence_ids"])

    def test_trigger_uscc_conflict(self):
        out = screen(self.base({"declared_uscc": "91999900XX"}, {"uscc": "91500100MA1"}))
        self.assertEqual(len(by_rule(out, "R001")), 1)

    def test_negative_consistent(self):
        out = screen(self.base({"declared_owner_id": "B1"}))
        self.assertEqual(by_rule(out, "R001"), [])

    def test_excluded_contexts(self):
        for ctx in ("legal_performance", "joint_venture_reference",
                    "tenderer_document"):
            out = screen(self.base({"declared_owner_id": "B2", "context": ctx}))
            self.assertEqual(by_rule(out, "R001"), [], ctx)

    def test_unknown_owner_not_judged(self):
        out = screen(self.base({"declared_owner_id": None}))
        self.assertEqual(by_rule(out, "R001"), [])

    def test_uscc_conflict_requires_both_evidence(self):
        # 二次复核第 2 条：USCC 冲突须双方证据均可解析。主体侧
        # uscc_evidence 缺失时不得触发，须记入 excluded_facts
        data = self.base({"declared_uscc": "91999900XX"},
                         {"uscc": "91500100MA1", "uscc_evidence": None})
        out = screen(data)
        self.assertEqual(by_rule(out, "R001"), [])
        self.assertTrue(any(f["reason"].startswith("uscc_conflict_missing")
                            for f in out["excluded_facts"]))

    def test_uscc_conflict_invalid_subject_evidence(self):
        # 主体侧证据引用不在注册表 → 同样不触发
        data = self.base({"declared_uscc": "91999900XX"},
                         {"uscc": "91500100MA1",
                          "uscc_evidence": "E-ghost-uscc"})
        out = screen(data)
        self.assertEqual(by_rule(out, "R001"), [])


# ---------------------------------------------------------------- R002

class R002Tests(unittest.TestCase):
    @staticmethod
    def contact(value="13800000001", source_role="bidder", evidence="E-x") -> dict:
        return {"kind": "phone", "value": value,
                "source_role": source_role, "evidence": evidence}

    def two_bidders(self, c1: dict, c2: dict, role2: str = "bidder") -> dict:
        return facts(
            event("EV-1", [lot("L1", [
                bid("B1", contacts=[c1]),
                bid("B2", role=role2, contacts=[c2])])]),
            evidence=default_evidence())

    def test_trigger_cross(self):
        out = screen(self.two_bidders(self.contact(evidence="E-extra-1"),
                                      self.contact(evidence="E-extra-2")))
        fs = by_rule(out, "R002")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["scope"]["bidder_ids"], ["B1", "B2"])
        self.assertTrue({"E-extra-1", "E-extra-2"} <= set(fs[0]["evidence_ids"]))

    def test_negative_distinct(self):
        out = screen(self.two_bidders(
            self.contact("13800000001", evidence="E-extra-1"),
            self.contact("13900000002", evidence="E-extra-2")))
        self.assertEqual(by_rule(out, "R002"), [])

    def test_role_exclusion(self):
        out = screen(self.two_bidders(self.contact(),
                                      self.contact(), role2="agency"))
        self.assertEqual(by_rule(out, "R002"), [])

    def test_source_role_platform_excluded(self):
        # 投标文件里抄录的平台电话：主体是 bidder，但来源角色是 platform，
        # 不得仅因 bid.role=bidder 就触发（Codex 复核现象 6）
        out = screen(self.two_bidders(self.contact(),
                                      self.contact(source_role="platform")))
        self.assertEqual(by_rule(out, "R002"), [])
        self.assertTrue(any(f["reason"].startswith("contact_source_role")
                            for f in out["excluded_facts"]))

    def test_source_role_unknown_excluded(self):
        out = screen(self.two_bidders(self.contact(),
                                      self.contact(source_role="unknown")))
        self.assertEqual(by_rule(out, "R002"), [])

    def test_unknown_value_skipped(self):
        out = screen(self.two_bidders(
            self.contact(value="unknown", evidence="E-extra-1"),
            self.contact(value="unknown", evidence="E-extra-2")))
        self.assertEqual(by_rule(out, "R002"), [])

    def test_invalid_occurrence_not_cited(self):
        # 二次复核第 3 条：B1 同号码先 ghost 后有效证据——finding 只能
        # 引用通过校验的 occurrence（E-c1/E-c2），不得漏 E-c1、引 E-ghost
        data = facts(
            event("EV-1", [lot("L1", [
                bid("B1", contacts=[
                    {"kind": "phone", "value": "138", "source_role": "bidder",
                     "evidence": "E-ghost"},
                    {"kind": "phone", "value": "138", "source_role": "bidder",
                     "evidence": "E-c1"}]),
                bid("B2", contacts=[
                    {"kind": "phone", "value": "138", "source_role": "bidder",
                     "evidence": "E-c2"}])])]),
            evidence=default_evidence() + [ev("E-c1"), ev("E-c2")])
        out = screen(data)
        fs = by_rule(out, "R002")
        self.assertEqual(len(fs), 1)
        self.assertNotIn("E-ghost", fs[0]["evidence_ids"])
        self.assertTrue({"E-c1", "E-c2"} <= set(fs[0]["evidence_ids"]))
        self.assertTrue(any(f["evidence_id"] == "E-ghost"
                            for f in out["excluded_facts"]))


# ---------------------------------------------------------------- R003

class R003Tests(unittest.TestCase):
    def two_bidders(self, p1: dict, p2: dict) -> dict:
        return facts(
            event("EV-1", [lot("L1", [
                bid("B1", persons=[p1]), bid("B2", persons=[p2])])]),
            evidence=default_evidence())

    def test_trigger_same_person_id(self):
        p1 = {"person_id": "PID-1", "name": "张三", "id_number": "510...001",
              "role": "项目经理", "evidence": "E-extra-1"}
        out = screen(self.two_bidders(p1, dict(p1, evidence="E-extra-2")))
        fs = [f for f in by_rule(out, "R003") if f["signal"] == "线索"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["scope"]["bidder_ids"], ["B1", "B2"])

    def test_same_name_diff_id_only_hint(self):
        p1 = {"person_id": "PID-1", "name": "张三", "id_number": "510...001",
              "role": "项目经理", "evidence": "E-extra-1"}
        p2 = {"person_id": "PID-2", "name": "张三", "id_number": "510...002",
              "role": "项目经理", "evidence": "E-extra-2"}
        out = screen(self.two_bidders(p1, p2))
        fs = by_rule(out, "R003")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "待核信号")
        self.assertIn("不判定同一人", fs[0]["trigger_reason"])

    def test_negative_distinct_people(self):
        out = screen(self.two_bidders(
            {"person_id": "PID-1", "name": "张三", "id_number": "510...001",
             "role": "项目经理", "evidence": "E-extra-1"},
            {"person_id": "PID-2", "name": "李四", "id_number": "510...002",
             "role": "项目经理", "evidence": "E-extra-2"}))
        self.assertEqual(by_rule(out, "R003"), [])

    def test_unknown_pid_skipped(self):
        out = screen(self.two_bidders(
            {"person_id": None, "name": "张三", "evidence": "E-extra-1"},
            {"person_id": None, "name": "张三", "evidence": "E-extra-2"}))
        self.assertEqual(by_rule(out, "R003"), [])

    def test_same_person_id_survives_id_gap(self):
        # Codex 复核现象 5：同 person_id，一个证件 unknown、一个有值——
        # 必须按稳定 person_id 关联为人员交叉，不得被证件号拆开，
        # 也不得误报"同名不同身份"
        p1 = {"person_id": "PID-9", "name": "王五", "id_number": "unknown",
              "role": "项目经理", "evidence": "E-extra-1"}
        p2 = {"person_id": "PID-9", "name": "王五", "id_number": "510...9",
              "role": "项目经理", "evidence": "E-extra-2"}
        out = screen(self.two_bidders(p1, p2))
        fs = by_rule(out, "R003")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "线索")
        self.assertEqual(fs[0]["params"]["id_number_variants"],
                         ["510...9", "unknown"])
        self.assertTrue(fs[0]["params"]["id_number_conflict"])


# ---------------------------------------------------------------- R004

class R004Tests(unittest.TestCase):
    def lines(self, prices: dict[str, list[float]]) -> list[dict]:
        """每家 bids 的 price_lines：prices = {bidder_id: [行1价, 行2价, ...]}"""
        out = {}
        for bidder_id, row_prices in prices.items():
            out[bidder_id] = [
                {"item_code": f"X-{i+1}", "item_name": "项", "spec": "同",
                 "unit": "项", "qty": 1, "unit_price": p,
                 "currency": "CNY", "tax_included": True,
                 "evidence": f"E-line-{bidder_id}-{i+1}"}
                for i, p in enumerate(row_prices)]
        return out

    def three_bidder_event(self, prices: dict[str, list[float]]) -> dict:
        bids = [bid(bidder_id, price_lines=ls)
                for bidder_id, ls in self.lines(prices).items()]
        evs = []
        for bidder_id, ls in self.lines(prices).items():
            for i, l in enumerate(ls):
                evs.append(ev(l["evidence"]))
        return facts(event("EV-1", [lot("L1", bids)]), evidence=default_evidence() + evs)

    def test_trigger_strong_three_rows(self):
        out = screen(self.three_bidder_event(
            {"B1": [100, 200, 300], "B2": [110, 210, 310], "B3": [120, 220, 320]}))
        fs = by_rule(out, "R004")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "线索")
        self.assertEqual(fs[0]["inputs"]["comparable_rows_matched"], 3)
        self.assertEqual(fs[0]["inputs"]["comparable_rows_unmatched_no_pattern"], 0)
        # 固定排序：所有行同一 bidder 顺序（对应关系保持）
        orders = {tuple(r["bidders_in_fixed_order"])
                  for r in fs[0]["inputs"]["rows_detail"]}
        self.assertEqual(orders, {("B1", "B2", "B3")})

    def test_signal_is_invariant_to_bidder_id_renaming(self):
        original = screen(self.three_bidder_event({
            "B1": [100, 200, 300], "B2": [110, 210, 310],
            "B3": [120, 220, 320]}))
        renamed = screen(self.three_bidder_event({
            "Z": [100, 200, 300], "A": [110, 210, 310],
            "M": [120, 220, 320]}))
        a, b = by_rule(original, "R004"), by_rule(renamed, "R004")
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(a[0]["signal"], b[0]["signal"])
        self.assertEqual(a[0]["scope"]["strong_lot_groups"][0]["rows"],
                         b[0]["scope"]["strong_lot_groups"][0]["rows"])
        self.assertEqual(
            [row["values"] for row in a[0]["inputs"]["rows_detail"]],
            [row["values"] for row in b[0]["inputs"]["rows_detail"]])

    def test_identical_quotes_are_reported_separately_from_price_pattern(self):
        out = screen(self.three_bidder_event({
            "B1": [100, 200], "B2": [100, 200], "B3": [100, 200]}))
        finding = by_rule(out, "R004")[0]
        self.assertEqual(finding["signal"], "弱线索")
        self.assertEqual({row["mode"] for row in finding["inputs"]["rows_detail"]},
                         {"完全相同"})

    def test_weak_single_row(self):
        out = screen(self.three_bidder_event(
            {"B1": [100], "B2": [110], "B3": [120]}))
        fs = by_rule(out, "R004")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "弱线索")

    def test_negative_no_pattern(self):
        out = screen(self.three_bidder_event(
            {"B1": [100, 200], "B2": [150, 260], "B3": [190, 240]}))
        self.assertEqual(by_rule(out, "R004"), [])

    def test_insufficient_bidders_unmatched(self):
        l1 = {"item_code": "X-1", "item_name": "项", "spec": "同", "unit": "项",
              "qty": 1, "unit_price": 100.0, "currency": "CNY",
              "tax_included": True, "evidence": "E-line-B1"}
        l2 = {**l1, "unit_price": 110.0, "evidence": "E-line-B2"}
        data = facts(
            event("EV-1", [lot("L1", [
                bid("B1", price_lines=[l1]),
                bid("B2", price_lines=[l2])])]),
            evidence=default_evidence() + [ev("E-line-B1"), ev("E-line-B2")])
        out = screen(data)
        self.assertEqual(by_rule(out, "R004"), [])
        self.assertEqual(
            out["r004_stats"]["comparable_groups_below_threshold"], 1)

    def test_no_cross_event_grouping(self):
        # Codex 复核现象 1：三个不同 event、每事件内 B1/B2/B3 报
        # 100/200/300。修复前全局拼组产出 1 个伪"可比行"；修复后每组按
        # (event, lot) 隔离，各自等差但分属不同事件 → 仅弱线索，
        # rows_detail 明确分属 3 个 event，绝不拼成同一竞价组
        evs = []
        line_evs = []
        for i in (1, 2, 3):
            bids = []
            for bidder_id, price in (("B1", 100.0), ("B2", 200.0), ("B3", 300.0)):
                line = {"item_code": "X-1", "item_name": "项", "spec": "同",
                        "unit": "项", "qty": 1, "unit_price": price,
                        "currency": "CNY", "tax_included": True,
                        "evidence": f"E-l-{i}-{bidder_id}"}
                bids.append(bid(bidder_id, price_lines=[line]))
                line_evs.append(ev(line["evidence"]))
            evs.append(event(f"EV-{i}", [lot("L1", bids)]))
        out = screen(facts(*evs, evidence=default_evidence() + line_evs))
        fs = by_rule(out, "R004")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "弱线索")
        row_events = {r["event_id"] for r in fs[0]["inputs"]["rows_detail"]}
        self.assertEqual(row_events, {"EV-1", "EV-2", "EV-3"})

    def test_geometric_positive(self):
        # Codex 复核现象 2：100/110/121 相邻比 1.1，等比为独立可触发模式
        out = screen(self.three_bidder_event(
            {"B1": [100], "B2": [110], "B3": [121]}))
        fs = by_rule(out, "R004")
        self.assertEqual(len(fs), 1)
        self.assertIn("等比", fs[0]["params"]["modes"])

    def test_unknown_item_identity_and_units_excluded(self):
        # 未知字段相同不构成可比清单行，排除项仍可回指事件、主体和证据。
        bids, evids = [], []
        for bidder_id, price in (("B1", 100), ("B2", 110), ("B3", 120)):
            eid = f"E-unknown-{bidder_id}"
            bids.append(bid(bidder_id, price_lines=[{
                "unit_price": price, "evidence": eid}]))
            evids.append(ev(eid))
        out = screen(facts(event("EV-1", [lot("L1", bids)]),
                           evidence=evids))
        self.assertEqual(by_rule(out, "R004"), [])
        stats = out["r004_stats"]
        self.assertEqual(stats["comparable_rows_matched"], 0)
        self.assertEqual(stats["comparable_rows_unmatched_no_pattern"], 0)
        self.assertEqual(stats["comparable_groups_total"], 0)
        self.assertEqual(stats["comparable_rows_excluded"], 3)
        self.assertEqual({r["bidder_id"] for r in stats["excluded_rows_detail"]},
                         {"B1", "B2", "B3"})
        self.assertTrue(all(r["event_id"] == "EV-1"
                            and r["lot_id"] == "L1" and r["evidence_id"]
                            for r in stats["excluded_rows_detail"]))
        self.assertTrue(all("未知" in r["reason"]
                            for r in stats["excluded_rows_detail"]))

    def test_tax_scale_three_state_not_collapsed(self):
        # 税口径三态：None(unknown) 不得折成 False 而与"不含税"混比
        from biaojing.rules import PriceLine

        k_unknown = PriceLine({"item_code": "A", "unit_price": 1,
                               "tax_included": None}).comparable_key()
        k_excl = PriceLine({"item_code": "A", "unit_price": 1,
                            "tax_included": False}).comparable_key()
        k_incl = PriceLine({"item_code": "A", "unit_price": 1,
                            "tax_included": True}).comparable_key()
        self.assertEqual(len({k_unknown, k_excl, k_incl}), 3)
        self.assertIsNone(k_unknown[-1])

    def test_matched_ratio_counts_no_pattern_groups(self):
        # 二次复核第 1 条：A 行 10/20/30 等差、B 行 10/25/50 无模式，
        # 两组均达 3 家门槛 → matched=1、no_pattern=1、ratio=0.5
        rows = {"A": {"B1": 10.0, "B2": 20.0, "B3": 30.0},
                "B": {"B1": 10.0, "B2": 25.0, "B3": 50.0}}
        bids, evs = [], []
        for bidder_id in ("B1", "B2", "B3"):
            pls = []
            for code, p in rows.items():
                pl = {"item_code": code, "item_name": "项", "spec": "同",
                      "unit": "项", "qty": 1, "unit_price": p[bidder_id],
                      "currency": "CNY", "tax_included": True,
                      "evidence": f"E-{code}-{bidder_id}"}
                pls.append(pl)
                evs.append(ev(pl["evidence"]))
            bids.append(bid(bidder_id, price_lines=pls))
        out = screen(facts(event("EV-1", [lot("L1", bids)]),
                           evidence=default_evidence() + evs))
        fs = by_rule(out, "R004")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["inputs"]["comparable_rows_matched"], 1)
        self.assertEqual(
            fs[0]["inputs"]["comparable_rows_unmatched_no_pattern"], 1)
        self.assertAlmostEqual(fs[0]["inputs"]["matched_ratio"], 0.5)


# ---------------------------------------------------------------- R005

class R005Tests(unittest.TestCase):
    def one_event(self, prices: list, name_suffix: str = "",
                  currency: str | None = "CNY",
                  tax: bool | None = True) -> dict:
        bids, evs = [], []
        for i, p in enumerate(prices, start=1):
            bids.append(bid(f"B{i}", total_price=p,
                            currency=currency, tax_included=tax,
                            total_price_evidence=f"E-p{i}",
                            outcome={"status": "unknown"}))
            evs.append(ev(f"E-p{i}"))
        return facts(event(f"EV-1{name_suffix}", [lot("L1", bids)]), evidence=evs)

    def test_trigger_and_recompute(self):
        out = screen(self.one_event([100.0, 101.0, 102.0]))
        fs = by_rule(out, "R005")
        self.assertEqual(len(fs), 1)
        prices = [100.0, 101.0, 102.0]
        mean = statistics.fmean(prices)
        rr = (max(prices) - min(prices)) / mean
        cv = statistics.pstdev(prices) / mean
        self.assertAlmostEqual(fs[0]["params"]["range_ratio"], rr, places=4)
        self.assertAlmostEqual(fs[0]["params"]["cv"], cv, places=4)
        self.assertEqual(fs[0]["inputs"]["n_positive"], 3)
        from decimal import Decimal
        self.assertEqual(Decimal(fs[0]["inputs"]["mean"]),
                         Decimal(str(round(mean, 4))))

    def test_negative_dispersed(self):
        out = screen(self.one_event([80.0, 100.0, 130.0]))
        self.assertEqual([f for f in by_rule(out, "R005")
                          if f["signal"] != "不计算"], [])

    def test_insufficient_explicit_skip(self):
        out = screen(self.one_event([100.0]))
        fs = by_rule(out, "R005")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "不计算")
        self.assertIn("不补零", fs[0]["trigger_reason"])

    def test_nonpositive_excluded(self):
        out = screen(self.one_event([100.0, 0, -5]))
        fs = by_rule(out, "R005")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "不计算")
        self.assertEqual(len(fs[0]["inputs"]["excluded"]), 2)

    def test_unknown_price(self):
        out = screen(self.one_event(["unknown", 100.0]))
        fs = by_rule(out, "R005")
        self.assertTrue(all(f["signal"] == "不计算" for f in fs))


    def test_direct_factbase_rejects_invalid_prices_and_tax(self):
        invalid_bids = [
            {"total_price": True}, {"total_price": False},
            {"total_price": float("nan")}, {"total_price": float("inf")},
            {"total_price": float("-inf")}, {"tax_included": "false"},
            {"price_lines": [{"unit_price": True}]},
            {"price_lines": [{"unit_price": float("nan")}]},
            {"price_lines": [{"unit_price": float("inf")}]},
            {"price_lines": [{"unit_price": 1, "tax_included": "false"}]},
        ]
        for i, override in enumerate(invalid_bids):
            with self.subTest(override=override):
                raw = bid("B1")
                raw.update(override)
                data = facts(event("EV-1", [lot("L1", [raw])]))
                with self.assertRaisesRegex(ValueError, "报价|单价|tax_included"):
                    FactBase.from_dict(data)

    def test_all_rule_entry_points_revalidate_mutated_facts(self):
        data = facts(event("EV-1", [lot("L1", [bid("B1", total_price=10)])]))
        for run in (screen_all,
                    lambda fb: rules.screen_r004(fb, rules.DEFAULT_THRESHOLDS),
                    lambda fb: rules.screen_r005(fb, rules.DEFAULT_THRESHOLDS),
                    lambda fb: rules.screen_r007(fb, rules.DEFAULT_THRESHOLDS)):
            fb = FactBase.from_dict(copy.deepcopy(data))
            fb.bids[0].total_price = True
            with self.assertRaisesRegex(ValueError, "总报价"):
                run(fb)

    def test_cross_currency_and_tax_not_mixed(self):
        # 二次复核第 7 条：CNY 含税 100 与 USD 不含税 101 不得进同一分母
        bids = [
            bid("B1", total_price=100.0, currency="CNY", tax_included=True,
                total_price_evidence="E-p1", outcome={"status": "unknown"}),
            bid("B2", total_price=101.0, currency="USD", tax_included=False,
                total_price_evidence="E-p2", outcome={"status": "unknown"})]
        out = screen(facts(event("EV-1", [lot("L1", bids)]),
                           evidence=[ev("E-p1"), ev("E-p2")]))
        self.assertEqual([f for f in by_rule(out, "R005")
                          if f["signal"] not in ("不计算",)], [])

    def test_missing_currency_isolated_not_cny(self):
        # 二次复核第 7 条：币种缺失保持 unknown，不得偷换成 CNY；
        # unknown 币种报价被隔离并披露
        bids = [bid("B1", total_price=100.0,
                    total_price_evidence="E-p1",
                    outcome={"status": "unknown"})]  # 无 currency 字段
        out = screen(facts(event("EV-1", [lot("L1", bids)]),
                           evidence=[ev("E-p1")]))
        fs = by_rule(out, "R005")
        self.assertTrue(all(f["signal"] == "不计算" for f in fs))
        self.assertTrue(any("币种 unknown" in e["reason"]
                            for f in fs for e in f["inputs"]["excluded"]))

    def test_cross_tax_scale_isolated(self):
        # 同币种但税口径不同（含税 100 / 不含税 101）互不比较
        bids = [
            bid("B1", total_price=100.0, currency="CNY", tax_included=True,
                total_price_evidence="E-p1", outcome={"status": "unknown"}),
            bid("B2", total_price=101.0, currency="CNY", tax_included=False,
                total_price_evidence="E-p2", outcome={"status": "unknown"})]
        out = screen(facts(event("EV-1", [lot("L1", bids)]),
                           evidence=[ev("E-p1"), ev("E-p2")]))
        self.assertEqual([f for f in by_rule(out, "R005")
                          if f["signal"] not in ("不计算",)], [])

    def test_no_cross_lot_denominator(self):
        # Codex 复核现象 3：同事件不同标段各一家 100/101——
        # 分母按 (event_id, lot_id) 划分，不得拼出"集中"
        bids_l1 = [bid("B1", total_price=100.0,
                       total_price_evidence="E-p1",
                       outcome={"status": "unknown"})]
        bids_l2 = [bid("B2", total_price=101.0,
                       total_price_evidence="E-p2",
                       outcome={"status": "unknown"})]
        out = screen(facts(event("EV-1", [lot("L1", bids_l1), lot("L2", bids_l2)]),
                           evidence=[ev("E-p1"), ev("E-p2")]))
        self.assertEqual([f for f in by_rule(out, "R005")
                          if f["signal"] not in ("不计算",)], [])


# ---------------------------------------------------------------- R006

class R006Tests(unittest.TestCase):
    def two_files(self, m1: dict, s1: str = "unknown", m2: dict | None = None,
                  s2: str = "project_scan") -> dict:
        m2 = m2 or m1
        return facts(
            event("EV-1", [lot("L1", [
                bid("B1", files=[{"file_ref": "a.pdf", "sha256": SHA,
                                  "source_type": s1, "context": "bid_document",
                                  "metadata": m1, "evidence": "E-extra-1"}]),
                bid("B2", files=[{"file_ref": "b.pdf", "sha256": "b" * 64,
                                  "source_type": s2, "context": "bid_document",
                                  "metadata": m2, "evidence": "E-extra-2"}])])]),
            evidence=default_evidence())

    def test_trigger_distinct_producer(self):
        m = {"producer": "凌云扫描仪 3.2", "creation_date": "2026-01-01T00:00:00"}
        out = screen(self.two_files(m, s1="project_scan", s2="project_scan"))
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "线索")

    def test_degraded_generic_producer(self):
        m = {"producer": "Microsoft Word 16.0", "creation_date": "2026-01-01"}
        out = screen(self.two_files(m))
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "待核信号")
        self.assertTrue(fs[0]["params"]["degraded"])

    def test_degraded_unknown_source(self):
        m = {"producer": "凌云扫描仪 3.2", "creation_date": "2026-01-01"}
        out = screen(self.two_files(m, s1="unknown"))
        self.assertEqual(by_rule(out, "R006")[0]["signal"], "待核信号")

    def test_negative_different_metadata(self):
        out = screen(self.two_files(
            {"producer": "甲软件", "creation_date": "2026-01-01"},
            m2={"producer": "乙软件", "creation_date": "2026-02-01"}))
        self.assertEqual(by_rule(out, "R006"), [])

    def test_unknown_creation_date_not_treated_equal(self):
        # 二次复核第 8 条：非通用 producer 相同、但双方都缺 creation_date——
        # 不得声称创建时间一致；最多待核信号并说明时间缺失
        m = {"producer": "Local Contoso Renderer"}  # 无 creation_date
        out = screen(self.two_files(m, s1="project_scan", s2="project_scan"))
        fs = by_rule(out, "R006")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["signal"], "待核信号")
        self.assertNotIn("creation_date=unknown", fs[0]["trigger_reason"])
        self.assertTrue(any("时间缺失" in r
                            for r in fs[0]["params"]["degrade_reasons"]))


# ---------------------------------------------------------------- R007

class R007Tests(unittest.TestCase):
    def multi_event(self, outcomes: list[dict], with_price_evidence: bool = False) -> dict:
        """outcomes: 每个 dict 描述一个事件中 B1 与 B2 的投标与结果。"""
        evs, evids = [], []
        for i, spec in enumerate(outcomes, start=1):
            b1 = bid("B1", total_price=spec.get("b1_price"),
                     currency=spec.get("b1_currency", "CNY"),
                     tax_included=spec.get("b1_tax", True),
                     total_price_evidence=(
                         f"E-p-{i}-B1" if with_price_evidence else None),
                     outcome=spec.get("b1_outcome", {"status": "unknown"}))
            b2 = bid("B2", total_price=spec.get("b2_price"),
                     currency=spec.get("b2_currency", "CNY"),
                     tax_included=spec.get("b2_tax", True),
                     total_price_evidence=(
                         f"E-p-{i}-B2" if with_price_evidence else None),
                     outcome=spec.get("b2_outcome", {"status": "unknown"}))
            evs.append(event(f"EV-{i}", [lot("L1", [b1, b2])]))
            if with_price_evidence:
                evids += [ev(f"E-p-{i}-B1"), ev(f"E-p-{i}-B2")]
        return facts(*evs, evidence=default_evidence() + evids)

    def test_trigger_known_lost(self):
        lost = {"status": "known", "result": "lost"}
        out = screen(self.multi_event([
            {"b1_outcome": lost}, {"b1_outcome": lost}, {"b1_outcome": lost}]))
        fs = by_rule(out, "R007")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["inputs"]["n_known_lost_events"], 3)
        self.assertEqual(fs[0]["inputs"]["n_events_covered"], 3)

    def test_lost_dedup_within_event(self):
        # Codex 复核：同一事件多个标段均 lost 只算 1 个事件，不按行累加
        lost = {"status": "known", "result": "lost"}
        ev = event("EV-1", [
            lot("L1", [bid("B1", outcome=lost), bid("B2")]),
            lot("L2", [bid("B1", outcome=lost), bid("B3")]),
            lot("L3", [bid("B1", outcome=lost), bid("B4")])])
        out = screen(facts(ev, evidence=default_evidence()))
        self.assertEqual(by_rule(out, "R007"), [])  # 仅 1 个事件，远低于阈值

    def test_no_cross_lot_price_compare(self):
        # Codex 复核：跨标段价格不可比——B1 在 L1 投 100（lost），
        # B2 在 L2 中标价 90：不同标段不得构成"高于中标价"
        lost = {"status": "known", "result": "lost"}
        won = {"status": "known", "result": "won"}
        e1 = event("EV-1", [
            lot("L1", [bid("B1", total_price=100.0,
                           total_price_evidence="E-p1", outcome=lost)]),
            lot("L2", [bid("B2", total_price=90.0,
                           total_price_evidence="E-p2", outcome=won)])])
        out = screen(facts(e1, evidence=[ev("E-p1"), ev("E-p2")]))
        self.assertEqual(by_rule(out, "R007"), [])

    def test_trigger_above_winning(self):
        lost = {"status": "known", "result": "lost"}
        won = {"status": "known", "result": "won"}
        out = screen(self.multi_event([
            {"b1_price": 100.0, "b1_outcome": lost,
             "b2_price": 90.0, "b2_outcome": won},
            {"b1_price": 100.0, "b1_outcome": lost,
             "b2_price": 91.0, "b2_outcome": won},
            {"b1_price": 100.0, "b1_outcome": lost,
             "b2_price": 92.0, "b2_outcome": won}], with_price_evidence=True))
        fs = by_rule(out, "R007")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["inputs"]["n_above_winning_price"], 3)
        # 正例须引用比较双方的价格证据
        price_evs = {f"E-p-{i}-{b}" for i in (1, 2, 3) for b in ("B1", "B2")}
        self.assertTrue(price_evs <= set(fs[0]["evidence_ids"]))

    def test_incomparable_currency_and_tax_do_not_count_as_above(self):
        lost = {"status": "known", "result": "lost"}
        won = {"status": "known", "result": "won"}
        out = screen(self.multi_event([
            {"b1_price": 100, "b1_currency": "CNY", "b1_tax": True,
             "b1_outcome": lost, "b2_price": 90, "b2_currency": "USD",
             "b2_tax": False, "b2_outcome": won},
            {"b1_price": 100, "b1_currency": "CNY", "b1_tax": True,
             "b1_outcome": lost, "b2_price": 90, "b2_currency": "USD",
             "b2_tax": False, "b2_outcome": won},
            {"b1_price": 100, "b1_currency": "CNY", "b1_tax": True,
             "b1_outcome": lost, "b2_price": 90, "b2_currency": "USD",
             "b2_tax": False, "b2_outcome": won}], with_price_evidence=True))
        fs = by_rule(out, "R007")
        self.assertEqual(len(fs), 1)  # 已知未中标事件独立达到阈值
        inputs = fs[0]["inputs"]
        self.assertEqual(inputs["n_known_lost_events"], 3)
        self.assertEqual(inputs["n_above_winning_price"], 0)
        self.assertEqual(inputs["n_price_comparisons_excluded"], 3)
        self.assertTrue(all("币种不一致" in x["reason"]
                            and "税口径不一致" in x["reason"]
                            for x in inputs["price_comparison_excluded"]))

    def test_unknown_currency_or_tax_excludes_comparison(self):
        won = {"status": "known", "result": "won"}
        unknown = {"status": "unknown"}
        cases = [
            ({"b1_currency": "unknown", "b1_tax": True,
              "b2_currency": "CNY", "b2_tax": True}, "币种未知"),
            ({"b1_currency": "CNY", "b1_tax": None,
              "b2_currency": "CNY", "b2_tax": True}, "税口径未知"),
            ({"b1_currency": "CNY", "b1_tax": False,
              "b2_currency": "CNY", "b2_tax": True}, "税口径不一致"),
        ]
        for i, (fields, expected_reason) in enumerate(cases, start=1):
            spec = {"b1_price": 120, "b1_outcome": unknown,
                    "b2_price": 100, "b2_outcome": won, **fields}
            out = screen(self.multi_event([spec]))
            self.assertEqual(by_rule(out, "R007"), [], expected_reason)
            self.assertTrue(any(expected_reason in x["reason"]
                                for x in out["excluded_facts"]),
                            f"case {i}: {expected_reason}")

    def test_above_requires_both_price_evidence(self):
        # 二次复核第 6 条：双方 total_price_evidence 缺失时不得计入
        # above_winning 比较（bid 级参与证据有效也不行）。
        # 构造：B1 出价 100 已知 won；B2 出价 120 结果 unknown——
        # 若无双方价格证据，B2 高于中标价不得计入，R007 不触发
        won = {"status": "known", "result": "won"}
        unknown = {"status": "unknown"}
        out = screen(self.multi_event([
            {"b1_price": 100.0, "b1_outcome": won,
             "b2_price": 120.0, "b2_outcome": unknown},
            {"b1_price": 100.0, "b1_outcome": won,
             "b2_price": 120.0, "b2_outcome": unknown},
            {"b1_price": 100.0, "b1_outcome": won,
             "b2_price": 120.0, "b2_outcome": unknown}]))
        self.assertEqual(by_rule(out, "R007"), [])
        # 正例：补上双方价格证据后，above=3 触发且引用双方价格证据
        out2 = screen(self.multi_event([
            {"b1_price": 100.0, "b1_outcome": won,
             "b2_price": 120.0, "b2_outcome": unknown},
            {"b1_price": 100.0, "b1_outcome": won,
             "b2_price": 120.0, "b2_outcome": unknown},
            {"b1_price": 100.0, "b1_outcome": won,
             "b2_price": 120.0, "b2_outcome": unknown}], with_price_evidence=True))
        fs = by_rule(out2, "R007")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["inputs"]["n_above_winning_price"], 3)
        price_evs = {f"E-p-{i}-{b}" for i in (1, 2, 3) for b in ("B1", "B2")}
        self.assertTrue(price_evs <= set(fs[0]["evidence_ids"]))
    def test_negative_unknown_not_lost(self):
        lost = {"status": "known", "result": "lost"}
        out = screen(self.multi_event([
            {"b1_outcome": lost}, {"b1_outcome": {"status": "unknown"}},
            {"b1_outcome": {"status": "unknown"}}]))
        self.assertEqual(by_rule(out, "R007"), [])

    def test_boundary_all_unknown(self):
        out = screen(self.multi_event([{} for _ in range(5)]))
        self.assertEqual(by_rule(out, "R007"), [])


# ---------------------------------------------------------------- R008

class R008Tests(unittest.TestCase):
    def joint_event(self, event_id: str, lots_n: int = 1) -> dict:
        lots = [lot(f"{event_id}-L{i+1}",
                    [bid("B1", outcome={"status": "unknown"}),
                     bid("B2", outcome={"status": "unknown"})])
                for i in range(lots_n)]
        return event(event_id, lots)

    def test_trigger_three_events(self):
        out = screen(facts(self.joint_event("EV-1"), self.joint_event("EV-2"),
                           self.joint_event("EV-3"), evidence=default_evidence()))
        fs = by_rule(out, "R008")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["inputs"]["n_common_events"], 3)
        self.assertEqual(fs[0]["inputs"]["common_event_ids"], ["EV-1", "EV-2", "EV-3"])

    def test_same_event_lots_dedup(self):
        # 同一事件 3 个标段都有 B1/B2：按事件去重后只有 1，不触发
        out = screen(facts(self.joint_event("EV-1", lots_n=3),
                           evidence=default_evidence()))
        self.assertEqual(by_rule(out, "R008"), [])

    def test_outcome_distribution_field(self):
        lost, won = {"status": "known", "result": "lost"}, {"status": "known", "result": "won"}
        e1 = event("EV-1", [lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)])])
        e2 = event("EV-2", [lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)])])
        e3 = event("EV-3", [lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)])])
        out = screen(facts(e1, e2, e3, evidence=default_evidence()))
        fs = by_rule(out, "R008")
        self.assertEqual(fs[0]["inputs"]["outcome_distribution_known"],
                         {"a=won|b=lost": 3})

    def test_same_event_diff_lots_not_joint(self):
        # Codex 复核现象 4：3 个事件里 B1 始终投 L1、B2 始终投 L2，
        # 从未在同一标段同场投标——不得算共同参投组合
        evs = []
        for i in (1, 2, 3):
            evs.append(event(f"EV-{i}", [
                lot("L1", [bid("B1", outcome={"status": "unknown"})]),
                lot("L2", [bid("B2", outcome={"status": "unknown"})])]))
        out = screen(facts(*evs, evidence=default_evidence()))
        self.assertEqual(by_rule(out, "R008"), [])

    def test_evidence_only_same_lot_usable(self):
        # 二次复核第 4 条：B1 在各事件另有 L2 记录且证据无效——
        # finding 证据不得混入其他标段的 E-ghost
        evs, evids = [], []
        for i in (1, 2, 3):
            b1_main = bid("B1", outcome={"status": "unknown"},
                          evidence=f"E-joint-B1-{i}")
            b2_main = bid("B2", outcome={"status": "unknown"},
                          evidence=f"E-joint-B2-{i}")
            b1_side = bid("B1", outcome={"status": "unknown"},
                          evidence=f"E-ghost-{i}")  # 无效证据 + 其他标段
            evs.append(event(f"EV-{i}", [
                lot("L1", [b1_main, b2_main]),
                lot("L2", [b1_side])]))
            evids += [ev(f"E-joint-B1-{i}"), ev(f"E-joint-B2-{i}")]
        out = screen(facts(*evs, evidence=default_evidence() + evids))
        fs = by_rule(out, "R008")
        self.assertEqual(len(fs), 1)
        self.assertFalse(any("ghost" in e for e in fs[0]["evidence_ids"]))
        self.assertTrue({f"E-joint-B1-{i}" for i in (1, 2, 3)}
                        <= set(fs[0]["evidence_ids"]))

    def test_outcome_dist_uses_common_lot_only(self):
        # 二次复核第 5 条：胜负分布只统计共同标段，其他标段结果不污染
        won = {"status": "known", "result": "won"}
        lost = {"status": "known", "result": "lost"}
        e1 = event("EV-1", [
            lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)]),
            lot("L2", [bid("B1", outcome=won)])])  # B2 不同场，不进分布
        e2 = event("EV-2", [
            lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)]),
            lot("L2", [bid("B1", outcome=lost)])])
        e3 = event("EV-3", [
            lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)])])
        out = screen(facts(e1, e2, e3, evidence=default_evidence()))
        fs = by_rule(out, "R008")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["inputs"]["outcome_distribution_known"],
                         {"a=won|b=lost": 3})

    def test_outcome_dist_two_lots_opposite(self):
        # 两个共同标段结果相反 → 分布各计 1 次，不合并
        won = {"status": "known", "result": "won"}
        lost = {"status": "known", "result": "lost"}
        evs = []
        for i in (1, 2, 3):
            evs.append(event(f"EV-{i}", [
                lot("L1", [bid("B1", outcome=won), bid("B2", outcome=lost)]),
                lot("L2", [bid("B1", outcome=lost), bid("B2", outcome=won)])]))
        out = screen(facts(*evs, evidence=default_evidence()))
        fs = by_rule(out, "R008")
        self.assertEqual(fs[0]["inputs"]["outcome_distribution_known"],
                         {"a=won|b=lost": 3, "a=lost|b=won": 3})


# ---------------------------------------------------------------- store 幂等/冲突

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="biaojing_p2_")
        self.db = os.path.join(self.tmp, "store.sqlite3")

    def sample(self, event_id="EV-1", price=100.0) -> dict:
        return facts(event(event_id, [lot("L1", [
            bid("B1", total_price=price, total_price_evidence="E-p1",
                contacts=[{"kind": "phone", "value": "138", "evidence": "E-c1"}],
                files=[{"file_ref": "a.pdf", "sha256": SHA,
                        "source_type": "unknown", "context": "bid_document",
                        "declared_owner_id": "B1", "evidence": "E-f1"}])])]),
            evidence=[ev("E-p1"), ev("E-c1"), ev("E-f1")])

    def test_import_idempotent_event_count(self):
        conn = store.connect(self.db)
        try:
            r1 = store.import_batch(self.sample(), conn)
            r2 = store.import_batch(self.sample(), conn)
            self.assertEqual(r1.events_inserted, 1)
            self.assertEqual(r2.events_inserted, 0)
            self.assertEqual(r2.events_skipped_idempotent, 1)
            self.assertEqual(store.event_count(conn), 1)
            self.assertEqual(r2.total_conflicts, 0)
        finally:
            conn.close()

    def test_conflict_same_id_diff_content(self):
        conn = store.connect(self.db)
        try:
            r1 = store.import_batch(self.sample(price=100.0), conn)
            r2 = store.import_batch(self.sample(price=999.0), conn)
            self.assertEqual(r1.events_inserted, 1)
            self.assertEqual(r2.events_inserted, 0)
            self.assertEqual(len(r2.conflicts), 1)
            self.assertEqual(r2.conflicts[0]["kind"], "event_id_conflict")
            self.assertEqual(store.event_count(conn), 1)  # 保留既有，不覆盖
            self.assertIn("拒绝覆盖", store.conflicts(conn)[0]["detail"])
        finally:
            conn.close()

    def test_repeat_file_keeps_source_refs(self):
        data1 = self.sample("EV-1")
        data2 = self.sample("EV-2")
        conn = store.connect(self.db)
        try:
            store.import_batch(data1, conn)
            store.import_batch(data2, conn)
            rows = conn.execute(
                "SELECT lot_id, bidder_id, file_sha256, file_ref FROM bid_files"
                " WHERE file_sha256=?", (SHA,)).fetchall()
            self.assertEqual(len(rows), 2)  # 同字节文件在两事件各留来源引用
            self.assertEqual(store.event_count(conn), 2)
        finally:
            conn.close()

    def test_bid_content_conflict(self):
        d1 = facts(event("EV-1", [lot("L1", [bid("B1", total_price=100.0)])]),
                   evidence=[])
        d2 = facts(event("EV-1", [lot("L1", [bid("B1", total_price=200.0)])]),
                   evidence=[])
        conn = store.connect(self.db)
        try:
            r1 = store.import_batch(d1, conn)
            self.assertEqual(r1.events_inserted, 1)
            # d2 事件 hash 不同 → 事件级冲突先拦截
            r2 = store.import_batch(d2, conn)
            self.assertTrue(any(c["kind"] == "event_id_conflict"
                                for c in r2.conflicts))
        finally:
            conn.close()

    def test_evidence_id_conflict(self):
        d1 = facts(event("EV-1", [lot("L1", [bid("B1")])]),
                   evidence=[ev("E-x", quote="甲")])
        d2 = facts(event("EV-2", [lot("L1", [bid("B1")])]),
                   evidence=[ev("E-x", quote="乙")])
        conn = store.connect(self.db)
        try:
            store.import_batch(d1, conn)
            r2 = store.import_batch(d2, conn)
            self.assertTrue(any(c["kind"] == "evidence_id_conflict"
                                for c in r2.conflicts))
        finally:
            conn.close()

    def test_evidence_conflict_rejects_event(self):
        # Codex 复核第 8 条：同 evidence_id 内容冲突时，引用它的新事件必须
        # 被整体拒绝——事件不插入、子行不残留、旧证据不覆盖、冲突有记录
        d1 = facts(event("EV-1", [lot("L1", [bid("B1")])]),
                   evidence=[ev("E-x", quote="甲")])
        d2 = facts(event("EV-2", [lot("L1", [
            bid("B1", evidence="E-x")])]),
            evidence=[ev("E-x", quote="乙")])  # 引用冲突证据的事件
        conn = store.connect(self.db)
        try:
            store.import_batch(d1, conn)
            r2 = store.import_batch(d2, conn)
            self.assertEqual(store.event_count(conn), 1)  # EV-2 未插入
            self.assertEqual(r2.events_inserted, 0)
            kept = conn.execute(
                "SELECT quote FROM evidence WHERE evidence_id='E-x'"
            ).fetchone()[0]
            self.assertEqual(kept, "甲")  # 旧证据未被覆盖
            kids = conn.execute(
                "SELECT COUNT(*) FROM bids WHERE event_id='EV-2'").fetchone()[0]
            self.assertEqual(kids, 0)  # 被拒事件无子行残留
            kinds = {c["kind"] for c in store.conflicts(conn)}
            self.assertIn("evidence_id_conflict", kinds)
            self.assertIn("event_rejected_stale_evidence", kinds)
        finally:
            conn.close()

    def test_event_atomic_rollback_on_bad_child(self):
        # Codex 复核第 8 条：中途无效子记录 → 整事件回滚，
        # 不留下 event 与部分子行
        bad = {"schema": SCHEMA, "evidence": [ev("E-z")], "events": [{
            "event_id": "EV-BAD", "project_id": "P-1", "event_name": "坏事件",
            "lots": [{"lot_id": "L1", "lot_name": "一标段", "bids": [{
                "bidder_id": "B1", "bidder_name": "甲", "role": "bidder",
                "evidence": "E-z",
                "outcome": {"status": "unknown"},
                "price_lines": [
                    {"item_code": "A", "unit_price": 10.0, "evidence": "E-z"},
                    {"item_code": "B", "unit_price": "abc", "evidence": "E-z"}],
            }]}]}]}
        conn = store.connect(self.db)
        try:
            r = store.import_batch(bad, conn)
            self.assertEqual(r.events_inserted, 0)
            self.assertEqual(store.event_count(conn), 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM bids WHERE event_id='EV-BAD'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM bid_price_lines WHERE event_id='EV-BAD'"
            ).fetchone()[0], 0)
            self.assertTrue(any(c["kind"] == "event_import_error"
                                for c in store.conflicts(conn)))
        finally:
            conn.close()

    def test_invalid_amounts_and_tax_reject_event_atomically(self):
        conn = store.connect(self.db)
        try:
            store.import_batch(self.sample("EV-GOOD", price=100.0), conn)
            invalid = [
                ("total_true", lambda b: b.update(total_price=True)),
                ("total_false", lambda b: b.update(total_price=False)),
                ("total_nan", lambda b: b.update(total_price=float("nan"))),
                ("total_inf", lambda b: b.update(total_price=float("inf"))),
                ("total_ninf", lambda b: b.update(total_price=float("-inf"))),
                ("bid_tax_string", lambda b: b.update(tax_included="false")),
                ("line_true", lambda b: b.update(price_lines=[{"unit_price": True}])),
                ("line_nan", lambda b: b.update(price_lines=[{"unit_price": float("nan")}])),
                ("line_inf", lambda b: b.update(price_lines=[{"unit_price": float("inf")}])),
                ("line_tax_string", lambda b: b.update(price_lines=[{
                    "unit_price": 1, "tax_included": "false"}])),
            ]
            for i, (name, mutate) in enumerate(invalid, start=1):
                data = self.sample(f"EV-BAD-{i}")
                bidder = data["events"][0]["lots"][0]["bids"][0]
                mutate(bidder)
                result = store.import_batch(data, conn)
                self.assertEqual(result.events_inserted, 0, name)
                self.assertTrue(any(c["kind"] == "event_import_error"
                                    and c["key"] == f"EV-BAD-{i}"
                                    for c in result.conflicts), name)
                self.assertEqual(store.event_count(conn), 1, name)
            kept = conn.execute(
                "SELECT total_price FROM bids WHERE event_id='EV-GOOD'"
            ).fetchone()[0]
            self.assertEqual(kept, 100.0)
        finally:
            conn.close()

    def test_true_false_none_tax_and_unknown_price_roundtrip(self):
        bids, line_evidence = [], []
        for i, (tax, total) in enumerate(((True, 100.0),
                                          (False, 110.0),
                                          (None, "unknown")), start=1):
            eid = f"E-line-{i}"
            bids.append(bid(
                f"B{i}", total_price=total, currency="CNY", tax_included=tax,
                total_price_evidence=eid,
                price_lines=[{"item_code": f"I{i}", "unit_price": i,
                              "currency": "CNY", "tax_included": tax,
                              "evidence": eid}]))
            line_evidence.append(ev(eid))
        data = facts(event("EV-ROUNDTRIP", [lot("L1", bids)]),
                     evidence=line_evidence)
        conn = store.connect(self.db)
        try:
            result = store.import_batch(data, conn)
            self.assertEqual(result.events_inserted, 1)
            roundtrip = store.load_factbase(conn)
        finally:
            conn.close()
        actual = {b.bidder_id: (b.total_price, b.tax_included,
                                b.price_lines[0].tax_included)
                  for b in roundtrip.bids}
        self.assertEqual(actual, {"B1": (100.0, True, True),
                                  "B2": (110.0, False, False),
                                  "B3": ("unknown", None, None)})

    def test_load_factbase_roundtrip_consistent(self):
        """经 SQLite 读回的事实做筛查，与直接从 JSON 构造的结果一致。"""
        data = facts(
            event("EV-1", [lot("L1", [
                bid("B1", total_price=100.0, total_price_evidence="E-p1",
                    outcome={"status": "known", "result": "won"}),
                bid("B2", total_price=101.0, total_price_evidence="E-p2"),
                bid("B3", total_price=102.0, total_price_evidence="E-p3")])]),
            evidence=[ev("E-p1"), ev("E-p2"), ev("E-p3")])
        direct = screen_all(FactBase.from_dict(data))
        conn = store.connect(self.db)
        try:
            store.import_batch(data, conn)
            roundtrip = screen_all(store.load_factbase(conn))
        finally:
            conn.close()
        self.assertEqual(direct["summary"], roundtrip["summary"])
        self.assertEqual(len(direct["findings"]), len(roundtrip["findings"]))

    def test_roundtrip_keeps_source_role_and_occurrences(self):
        """验收补充要求：roundtrip 必须保留联系人的 source_role，以及
        同 value/person_id 下不同 evidence 的多个 occurrence——
        不得被主键吞掉（先写测试证明，再修 store）。"""
        data = facts(event("EV-1", [lot("L1", [
            bid("B1",
                contacts=[
                    {"kind": "phone", "value": "138", "source_role": "bidder",
                     "evidence": "E-c1"},
                    {"kind": "phone", "value": "138", "source_role": "platform",
                     "evidence": "E-c2"}],
                persons=[
                    {"person_id": "PID-1", "name": "张三", "id_number": "51001",
                     "role": "项目经理", "evidence": "E-per-1"},
                    {"person_id": "PID-1", "name": "张三", "id_number": "51001",
                     "role": "技术负责人", "evidence": "E-per-2"}])])]),
            evidence=[ev("E-c1"), ev("E-c2"), ev("E-per-1"), ev("E-per-2")])
        conn = store.connect(self.db)
        try:
            store.import_batch(data, conn)
            fb = store.load_factbase(conn)
        finally:
            conn.close()
        b1 = [b for b in fb.bids if b.bidder_id == "B1"][0]
        phones = [c for c in b1.contacts if c.kind == "phone"]
        roles = sorted(c.source_role for c in phones)
        evids = sorted(c.evidence for c in phones)
        self.assertEqual(roles, ["bidder", "platform"])
        self.assertEqual(evids, ["E-c1", "E-c2"])
        persons = sorted(p.evidence for p in b1.persons
                         if p.person_id == "PID-1")
        self.assertEqual(persons, ["E-per-1", "E-per-2"])

    def test_r001_uscc_conflict_survives_roundtrip(self):
        # 第三次复核：uscc/uscc_evidence 必须随 bid occurrence 存取——
        # USCC 冲突经 SQLite roundtrip 后 R001 仍在且引用 E-file/E-uscc
        data = facts(
            event("EV-1", [lot("L1", [
                bid("B1", uscc="U1", uscc_evidence="E-uscc",
                    files=[{"file_ref": "a.pdf", "sha256": SHA,
                            "source_type": "unknown", "context": "bid_document",
                            "declared_uscc": "U2", "evidence": "E-file"}])])]),
            evidence=[ev("E-uscc"), ev("E-file")])
        direct = screen_all(FactBase.from_dict(data))
        self.assertEqual(len(by_rule(direct, "R001")), 1)  # 直筛先确认触发
        conn = store.connect(self.db)
        try:
            store.import_batch(data, conn)
            roundtrip = screen_all(store.load_factbase(conn))
        finally:
            conn.close()
        fs = by_rule(roundtrip, "R001")
        self.assertEqual(len(fs), 1)  # roundtrip 后 R001 不消失
        self.assertTrue({"E-file", "E-uscc"} <= set(fs[0]["evidence_ids"]))
        # 不得出现"主体 USCC 证据缺失"的误排除（uscc_evidence 已按
        # bid occurrence 存取）
        self.assertFalse(any("uscc_conflict_missing" in f["reason"]
                             for f in roundtrip["excluded_facts"]))

    def test_same_bidder_diff_uscc_across_events(self):
        # 第三次复核：同 bidder_id 跨事件 USCC 不同——各 bid 的事实与证据
        # 按事件读回，不得串值或被 INSERT OR IGNORE 静默覆盖
        data = facts(
            event("EV-1", [lot("L1", [
                bid("B1", uscc="U1", uscc_evidence="E-uscc-1",
                    files=[{"file_ref": "a.pdf", "sha256": SHA,
                            "source_type": "unknown", "context": "bid_document",
                            "declared_uscc": "U9", "evidence": "E-file-1"}])])]),
            event("EV-2", [lot("L1", [
                bid("B1", uscc="U2", uscc_evidence="E-uscc-2",
                    files=[{"file_ref": "b.pdf", "sha256": "b" * 64,
                            "source_type": "unknown", "context": "bid_document",
                            "declared_uscc": "U8", "evidence": "E-file-2"}])])]),
            evidence=[ev("E-uscc-1"), ev("E-file-1"),
                      ev("E-uscc-2"), ev("E-file-2")])
        conn = store.connect(self.db)
        try:
            store.import_batch(data, conn)
            fb = store.load_factbase(conn)
        finally:
            conn.close()
        by_event = {b.event_id: b for b in fb.bids if b.bidder_id == "B1"}
        self.assertEqual(by_event["EV-1"].uscc, "U1")
        self.assertEqual(by_event["EV-1"].uscc_evidence, "E-uscc-1")
        self.assertEqual(by_event["EV-2"].uscc, "U2")
        self.assertEqual(by_event["EV-2"].uscc_evidence, "E-uscc-2")
        # 两个事件的 USCC 冲突独立成立
        out = screen_all(fb)
        self.assertEqual(len(by_rule(out, "R001")), 2)

    def test_bidder_masterdata_name_conflict_visible(self):
        # 名称仅作显示主数据：同 bidder_id 不同显示名 → 记冲突可见，
        # 既有值保留，不静默覆盖
        d1 = facts(event("EV-1", [lot("L1", [bid("B1", bidder_name="旧名称")])]),
                   evidence=[])
        d2 = facts(event("EV-2", [lot("L1", [bid("B1", bidder_name="新名称")])]),
                   evidence=[])
        conn = store.connect(self.db)
        try:
            store.import_batch(d1, conn)
            r2 = store.import_batch(d2, conn)
            name = conn.execute(
                "SELECT display_name FROM bidders WHERE bidder_id='B1'"
            ).fetchone()[0]
            self.assertEqual(name, "旧名称")  # 首值保留，不覆盖
            self.assertTrue(any(c["kind"] == "bidder_masterdata_conflict"
                                for c in store.conflicts(conn)))
            self.assertEqual(r2.events_inserted, 1)  # 事件本身照常导入
        finally:
            conn.close()


# ---------------------------------------------------------------- 证据强制

class EvidenceEnforcementTests(unittest.TestCase):
    """Codex 复核第 7 条：触发字段证据缺失/无效 → 事实不触发并记录排除。"""

    def cross_phone(self, evidence: dict | None, locator: dict | None = None) -> dict:
        """两家同电话的冲突事实，电话证据由参数控制。"""
        c1 = {"kind": "phone", "value": "138", "source_role": "bidder",
              "evidence": evidence}
        return facts(
            event("EV-1", [lot("L1", [
                bid("B1", contacts=[c1]), bid("B2", contacts=[dict(c1)])])]),
            evidence=default_evidence())

    def test_missing_evidence_id_excludes_trigger(self):
        out = screen(self.cross_phone(evidence=None))
        self.assertEqual(by_rule(out, "R002"), [])
        self.assertTrue(any(f["reason"] == "evidence_absent"
                            for f in out["excluded_facts"]))

    def test_invalid_evidence_id_excludes_trigger(self):
        # E-ghost 不在证据注册表
        out = screen(self.cross_phone(evidence="E-ghost"))
        self.assertEqual(by_rule(out, "R002"), [])
        self.assertTrue(any(f["reason"] == "evidence_missing_or_invalid"
                            and f["evidence_id"] == "E-ghost"
                            for f in out["excluded_facts"]))

    def test_locator_without_kind_is_invalid(self):
        # 证据存在但 locator 缺 kind → 视为无效，事实不触发
        data = self.cross_phone(evidence="E-bad")
        data["evidence"].append(
            ev("E-bad", locator={"sheet": "S", "cell": "A1"}))
        out = screen(data)
        self.assertEqual(by_rule(out, "R002"), [])


# ---------------------------------------------------------------- 证据可解析 / 形状

class FindingContractTests(unittest.TestCase):
    REQUIRED = ["rule_id", "rule_version", "scope", "inputs", "params",
                "trigger_reason", "evidence_ids", "limitations",
                "alternative_explanations", "review_status", "signal"]

    def sample_with_all_rules(self) -> dict:
        line = {"item_code": "X-1", "item_name": "项", "spec": "同", "unit": "项",
                "qty": 1, "unit_price": 100.0, "currency": "CNY",
                "tax_included": True, "evidence": "E-line-B1"}
        line2 = {**line, "item_code": "X-2", "evidence": "E-line2-B1"}
        f1 = {"file_ref": "a.pdf", "sha256": SHA, "source_type": "unknown",
              "context": "bid_document", "declared_owner_id": "B2",
              "metadata": {"producer": "凌云扫描仪", "creation_date": "2026-01-01"},
              "evidence": "E-file-B1"}
        f2 = {"file_ref": "b.pdf", "sha256": "b" * 64, "source_type": "unknown",
              "context": "bid_document",
              "metadata": {"producer": "凌云扫描仪", "creation_date": "2026-01-01"},
              "evidence": "E-file-B2"}
        person1 = {"person_id": "PID-1", "name": "张三", "id_number": "51001",
                   "role": "项目经理", "evidence": "E-per-1"}
        person2 = {**person1, "evidence": "E-per-2"}
        b1 = bid("B1", uscc="U1", total_price=100.0,
                 total_price_evidence="E-p1",
                 contacts=[{"kind": "phone", "value": "138",
                            "evidence": "E-c1"}],
                 persons=[person1], price_lines=[line, line2], files=[f1],
                 outcome={"status": "unknown"})
        b2 = bid("B2", total_price=101.0, total_price_evidence="E-p2",
                 contacts=[{"kind": "phone", "value": "138",
                            "evidence": "E-c2"}],
                 persons=[person2],
                 price_lines=[{**line, "unit_price": 110.0,
                               "evidence": "E-line-B2"},
                              {**line2, "unit_price": 110.0,
                               "evidence": "E-line2-B2"}],
                 files=[f2], outcome={"status": "unknown"})
        e1 = event("EV-1", [lot("L1", [b1, b2])])
        evids = []
        for eid in ["E-bid-B1", "E-bid-B2", "E-p1", "E-p2", "E-c1", "E-c2",
                    "E-per-1", "E-per-2", "E-file-B1", "E-file-B2",
                    "E-line-B1", "E-line-B2", "E-line2-B1", "E-line2-B2"]:
            evids.append(ev(eid))
        return facts(e1, evidence=evids)

    def test_all_findings_shape_and_evidence_resolvable(self):
        data = self.sample_with_all_rules()
        fb = FactBase.from_dict(data)
        out = screen_all(fb)
        self.assertGreater(out["summary"]["total"], 0)
        for f in out["findings"]:
            for key in self.REQUIRED:
                self.assertIn(key, f, f"{f.get('rule_id')} 缺 {key}")
            for eid in f["evidence_ids"]:
                self.assertIn(eid, fb.evidence,
                              f"{f['rule_id']} 引用不可解析的 {eid}")
        self.assertEqual(out["unresolved_evidence"], [])

    def test_usable_rejects_bad_locator_kinds(self):
        # 二次复核第 9 条：kind 空串/None/未知 kind 的 locator 一律无效
        from biaojing.rules import FactBase

        fb = FactBase.from_dict(facts(evidence=[
            ev("E-empty", locator={"kind": "", "page": 1}),
            ev("E-none", locator={"page": 1}),
            ev("E-unknown-kind", locator={"kind": "unknown_kind", "page": 1}),
            ev("E-pdf-bad", locator={"kind": "pdf_page", "page": 0}),
            ev("E-xlsx-ok", locator={"kind": "xlsx_cell",
                                     "sheet": "S", "cell": "A1"}),
        ]))
        self.assertFalse(fb.usable("E-empty"))
        self.assertFalse(fb.usable("E-none"))
        self.assertFalse(fb.usable("E-unknown-kind"))
        self.assertFalse(fb.usable("E-pdf-bad"))
        self.assertTrue(fb.usable("E-xlsx-ok"))


# ---------------------------------------------------------------- CLI

class ScreenCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="biaojing_p2_cli_")
        self.db = os.path.join(self.tmp, "p2.sqlite3")
        self.facts_path = os.path.join(self.tmp, "facts.json")
        data = facts(
            event("EV-1", [lot("L1", [
                bid("B1", total_price=100.0, currency="CNY",
                    tax_included=True, total_price_evidence="E-p1",
                    outcome={"status": "unknown"}),
                bid("B2", total_price=101.0, currency="CNY",
                    tax_included=True, total_price_evidence="E-p2",
                    outcome={"status": "unknown"}),
                bid("B3", total_price=102.0, currency="CNY",
                    tax_included=True, total_price_evidence="E-p3",
                    outcome={"status": "unknown"})])]),
            evidence=[ev("E-p1"), ev("E-p2"), ev("E-p3")])
        with open(self.facts_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def run_cli(self, *extra) -> tuple[int, str, str]:
        from biaojing import screen_cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = screen_cli.run([self.facts_path, "--db", self.db, *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_help_mentions_product(self):
        app_dir = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))  # .../app
        proc = subprocess.run(
            [sys.executable, "-m", "biaojing.screen", "--help"],
            capture_output=True, text=True, cwd=app_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("标镜", proc.stdout + proc.stderr)

    def test_cli_json_parse_and_product(self):
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertNotIn("Exception ignored", err)
        payload = json.loads(out)
        self.assertEqual(payload["product_name"], "标镜")
        self.assertEqual(payload["module"], "biaojing.screen")
        self.assertEqual(payload["import"]["events_inserted"], 1)
        self.assertGreater(payload["summary"]["total"], 0)

    def test_cli_idempotent_rerun(self):
        rc1, out1, _ = self.run_cli()
        rc2, out2, _ = self.run_cli()
        p1, p2 = json.loads(out1), json.loads(out2)
        self.assertEqual(p2["import"]["events_inserted"], 0)
        self.assertEqual(p2["import"]["events_skipped_idempotent"], 1)
        self.assertEqual(p2["import"]["event_count_after"], 1)
        # 重复导入前后：事件数与 R008 findings 均不变
        self.assertEqual(p1["summary"]["by_rule"].get("R008"),
                         p2["summary"]["by_rule"].get("R008"))
        self.assertEqual(p1["findings"], p2["findings"])

    def test_cli_stderr_clean_and_output_file(self):
        out_path = os.path.join(self.tmp, "out.json")
        rc, _, err = self.run_cli("-o", out_path)
        self.assertEqual(rc, 0)
        self.assertNotIn("Exception ignored", err)
        with open(out_path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["product_name"], "标镜")


if __name__ == "__main__":
    unittest.main()

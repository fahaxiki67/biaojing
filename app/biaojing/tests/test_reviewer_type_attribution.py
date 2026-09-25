# -*- coding: utf-8 -*-
"""标镜确认操作者归属回归（D 报告 D-03，任务口径明确要求）。

口径：/api/confirm 接受 reviewer_type=agent/test 并落入
confirmation_history.actor（区分机器代理与人工复核写入）；缺省为
「本机用户」；未知 reviewer_type 返回结构化失败（静默当人工会误记
归属）。不改变任何既有确认语义。

运行：
    cd app && python3 -W error::ResourceWarning -m unittest \\
        biaojing.tests.test_reviewer_type_attribution -v
"""

from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

import openpyxl

from biaojing import webapp, workspace


class ReviewerTypeAttributionTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="biaojing_reviewer_")
        self.wb = workspace.Workbench(self.temp.name)
        self.srv = webapp.WorkbenchServer(("127.0.0.1", 0), self.wb)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)
        self.thread.start()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["投标人名称", "综合单价(元)", "清单名称", "规格型号"])
        ws.append(["甲公司", 100, "电线", "BV"])
        buf = io.BytesIO()
        wb.save(buf)
        req = urllib.request.Request(
            self.base + "/api/upload?name=rect.xlsx", data=buf.getvalue(),
            method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Origin": self.base})
        with urllib.request.urlopen(req, timeout=15) as resp:
            self.assertTrue(json.load(resp)["ok"])
        state = self._get_json("/api/state")
        self.pl = next(c for c in state["candidates"]
                       if c["field"] == "price_lines")
        self.lines = self.pl["value"] if isinstance(self.pl["value"], list) \
            else json.loads(self.pl["value"])

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.wb.close()
        self.temp.cleanup()

    def _post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Origin": self.base})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)

    def _get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.load(resp)

    def _confirm(self, reviewer_type=None) -> dict:
        payload = {"event_id": "EV-R", "lot_id": "LOT-R",
                   "bidder_id": "SYN-BIDDER-01", "field": "price_lines",
                   "value": self.lines, "evidence_id": self.pl["evidence_id"],
                   "action": "confirm"}
        if reviewer_type is not None:
            payload["reviewer_type"] = reviewer_type
        return self._post_json("/api/confirm", payload)

    def _actors(self):
        state = self._get_json("/api/state")
        return [h.get("actor") for h in state.get("confirmation_history", [])]

    def test_reviewer_type_test_recorded(self):
        res = self._confirm("test")
        self.assertTrue(res.get("ok"), res)
        self.assertIn("test", self._actors())

    def test_reviewer_type_agent_recorded(self):
        res = self._confirm("agent")
        self.assertTrue(res.get("ok"), res)
        self.assertIn("agent", self._actors())

    def test_default_actor_is_local_user(self):
        res = self._confirm(None)
        self.assertTrue(res.get("ok"), res)
        self.assertIn("本机用户", self._actors())

    def test_unknown_reviewer_type_rejected(self):
        res = self._confirm("superuser")
        self.assertFalse(res.get("ok"), "未知 reviewer_type 被静默当人工")
        self.assertIn("reviewer_type", res.get("error", ""))

    def test_contact_person_amount_paths_record_actor(self):
        # 复核十轮：actor 贯穿普通字段/联系人/人员/原子金额全部写入路径
        state = self._get_json("/api/state")
        cand = next(c for c in state["candidates"]
                    if c["field"] == "bidder_name")
        ev_id = cand["evidence_id"]
        for field, extra, expect in (
                ("contact_phone", {"value": "0571-88880000",
                                   "source_role": "bidder"}, "test"),
                ("person_manager", {"value": "合成经理",
                                    "person_id": "SYN-P-1"}, "agent")):
            payload = {"event_id": "EV-R", "lot_id": "LOT-R",
                       "bidder_id": "SYN-BIDDER-01", "field": field,
                       "evidence_id": ev_id, "action": "confirm",
                       "reviewer_type": expect}
            payload.update(extra)
            res = self._post_json("/api/confirm", payload)
            self.assertTrue(res.get("ok"), (field, res))
        amount = self._post_json("/api/confirm_amount", {
            "event_id": "EV-R", "lot_id": "LOT-R",
            "bidder_id": "SYN-BIDDER-01", "raw_value": "100",
            "unit": "元", "currency": "CNY", "tax_included": True,
            "evidence_id": self.lines[0]["evidence"],
            "action": "confirm", "reviewer_type": "agent"})
        self.assertTrue(amount.get("ok"), amount)
        actors = {h.get("field"): h.get("actor")
                  for h in self._get_json("/api/state")
                  .get("confirmation_history", [])}
        self.assertEqual(actors.get("contact_phone"), "test")
        self.assertEqual(actors.get("person_manager"), "agent")
        self.assertEqual(actors.get("total_price"), "agent")

    def test_amount_unknown_reviewer_rejected_without_history(self):
        before = len(self._get_json("/api/state")
                     .get("confirmation_history", []))
        amount = self._post_json("/api/confirm_amount", {
            "event_id": "EV-R", "lot_id": "LOT-R",
            "bidder_id": "SYN-BIDDER-01", "raw_value": "100",
            "unit": "元", "currency": "CNY", "tax_included": True,
            "evidence_id": self.lines[0]["evidence"],
            "action": "confirm", "reviewer_type": "superuser"})
        self.assertFalse(amount.get("ok"), "未知 reviewer_type 被静默接受")
        after = len(self._get_json("/api/state")
                    .get("confirmation_history", []))
        self.assertEqual(before, after, "失败确认留下部分历史")


if __name__ == "__main__":
    unittest.main()

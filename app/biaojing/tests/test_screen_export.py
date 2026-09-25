# -*- coding: utf-8 -*-
"""标镜筛查结果导出回归（D-04 闭环）。

口径：
  - GET /api/screen/last 返回最近一次 screen_runs 完整导出：id/run_at/
    status/error/rules_version/facts_sha256/facts_json/outcome_json；
    facts_json/outcome_json 为库内原始字符串（逐字节），便于对
    facts_sha256 现场重算复核；
  - Content-Disposition: attachment；沿用本机 Host 校验（do_GET 统一）；
  - 尚无运行记录 → 404 + 结构化中文错误，不 500；
  - outcome_json 内含真实 finding 与 evidence 引用。

运行：
    cd app && python3 -W error::ResourceWarning -m unittest \\
        biaojing.tests.test_screen_export -v
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import openpyxl

from biaojing import webapp, workspace


class ScreenExportTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="biaojing_exp_")
        self.wb = workspace.Workbench(self.temp.name)
        self.srv = webapp.WorkbenchServer(("127.0.0.1", 0), self.wb)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)
        self.thread.start()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["投标人名称", "清单名称", "规格型号", "单位",
                   "是否含税", "综合单价(元)"])
        for row in (["甲公司", "电线", "BV", "台", "是", 100],
                    ["乙公司", "电线", "BV", "台", "是", 110],
                    ["丙公司", "电线", "BV", "台", "是", 120],
                    ["甲公司", "开关", "10A", "台", "是", 200],
                    ["乙公司", "开关", "10A", "台", "是", 210],
                    ["丙公司", "开关", "10A", "台", "是", 220]):
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        req = urllib.request.Request(
            self.base + "/api/upload?name=rect.xlsx", data=buf.getvalue(),
            method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Origin": self.base})
        with urllib.request.urlopen(req, timeout=15) as resp:
            self.assertTrue(json.load(resp)["ok"])

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.wb.close()
        self.temp.cleanup()

    def _get(self, path: str, timeout: int = 10):
        req = urllib.request.Request(self.base + path,
                                     headers={"Origin": self.base})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), json.load(resp)
        except urllib.error.HTTPError as exc:
            with exc:
                body = json.load(exc)
            return exc.code, dict(exc.headers), body

    def _get_json(self, path: str) -> dict:
        _, _, body = self._get(path)
        return body

    def _post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Origin": self.base})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)

    def _confirm_all_and_screen(self):
        state = self._get_json("/api/state")
        bidder_for = {"100": "SYN-BIDDER-01", "110": "SYN-BIDDER-02",
                      "120": "SYN-BIDDER-03"}
        for cand in state["candidates"]:
            if cand["field"] != "price_lines":
                continue
            lines = cand["value"] if isinstance(cand["value"], list) \
                else json.loads(cand["value"])
            bidder = bidder_for[str(lines[0]["unit_price"])]
            res = self._post_json("/api/confirm", {
                "event_id": "EV-SYN-001", "lot_id": "SYN-LOT-001",
                "bidder_id": bidder, "field": "price_lines",
                "value": lines, "evidence_id": cand["evidence_id"],
                "action": "confirm", "reviewer_type": "test"})
            self.assertTrue(res.get("ok"), res)
        screen = self._post_json("/api/screen", {})
        self.assertIn("findings", screen,
                      f"筛查响应缺 findings：{str(screen)[:200]}")

    def test_no_run_returns_structured_404(self):
        code, headers, body = self._get("/api/screen/last")
        self.assertEqual(code, 404)
        self.assertIn("尚未运行筛查", body.get("error", ""))

    def test_export_fields_hash_and_finding(self):
        self._confirm_all_and_screen()
        code, headers, export = self._get("/api/screen/last")
        self.assertEqual(code, 200)
        disposition = headers.get("Content-Disposition", "")
        self.assertIn("attachment", disposition)
        self.assertIn(".json", disposition)
        for key in ("run_at", "status", "error", "rules_version",
                    "facts_sha256", "facts_json", "outcome_json"):
            self.assertIn(key, export, f"导出缺字段 {key}")
        self.assertEqual(export["status"], "completed")
        # facts_sha256 逐字节重算一致
        recomputed = hashlib.sha256(
            export["facts_json"].encode("utf-8")).hexdigest()
        self.assertEqual(recomputed, export["facts_sha256"])
        # outcome 含真实 finding 与证据引用
        outcome = json.loads(export["outcome_json"])
        findings = [f for f in outcome.get("findings", [])
                    if f.get("signal") not in ("不计算",)]
        self.assertTrue(findings, "合成等差数据应产出 R004 线索")
        self.assertTrue(all(f.get("evidence_ids") for f in findings))
        facts = json.loads(export["facts_json"])
        self.assertEqual(len(facts["evidence"]), len({
            e["evidence_id"] for e in facts["evidence"]}),
            "证据注册表无重复 ID")

    def test_export_matches_state_screen_run(self):
        self._confirm_all_and_screen()
        _, _, export = self._get("/api/screen/last")
        state = self._get_json("/api/state")
        last = state.get("last_screen_run") or {}
        self.assertEqual(export["facts_sha256"], last.get("facts_sha256"))
        self.assertEqual(export["run_at"], last.get("run_at"))


if __name__ == "__main__":
    unittest.main()

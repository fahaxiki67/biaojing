# -*- coding: utf-8 -*-
"""针对仓库审查中确认的高风险缺陷的最小回归测试。"""

import unittest
import json
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import re

from biaojing import candidates
from biaojing.money import normalize_confirmed_amount, parse_amount


class AmountRegressionTests(unittest.TestCase):
    def test_yuan_and_wanyuan_are_normalized_exactly(self):
        self.assertEqual(parse_amount("100000元")["amount_yuan"], "100000")
        self.assertEqual(parse_amount("人民币100万元")["amount_yuan"], "1000000")
        self.assertEqual(parse_amount("100万")["amount_yuan"], "1000000")
        self.assertEqual(parse_amount("-100元")["amount_yuan"], "-100")
        self.assertEqual(parse_amount("1.5", "ten_thousand_yuan")["amount_yuan"],
                         "15000")
        self.assertEqual(normalize_confirmed_amount("1.5", "ten_thousand_yuan"),
                         "15000")

    def test_uncertain_or_ambiguous_amount_is_not_silently_used(self):
        self.assertEqual(parse_amount("100000")["status"], "needs_unit")
        self.assertEqual(parse_amount("100万元至120万元")["status"], "ambiguous")
        self.assertEqual(parse_amount("100元", "ten_thousand_yuan")["status"],
                         "unit_conflict")
        with self.assertRaises(ValueError):
            normalize_confirmed_amount("100万元至120万元", "yuan")

    def test_foreign_currency_and_conflicting_markers_are_not_yuan(self):
        self.assertEqual(parse_amount("USD 100", "yuan")["currency"], "USD")
        self.assertEqual(parse_amount("CNY $100", "yuan")["status"],
                         "currency_conflict")
        with self.assertRaisesRegex(ValueError, "外币"):
            normalize_confirmed_amount("USD 100", "yuan")
        with self.assertRaisesRegex(ValueError, "冲突"):
            normalize_confirmed_amount("CNY $100", "yuan")

    def test_space_after_label_colon_does_not_hide_bidder_name(self):
        pairs = candidates._extract_pairs("投标人名称： 甲公司")
        self.assertEqual(pairs, [("bidder_name", "甲公司")])

    def test_xlsx_suggestions_do_not_stop_at_fifty_rows(self):
        evidence = [
            {"kind": "xlsx_cell", "sheet": "报价", "cell": "A1",
             "value": "投标人名称", "evidence_id": "E-A1"},
            {"kind": "xlsx_cell", "sheet": "报价", "cell": "B1",
             "value": "总报价(万元)", "evidence_id": "E-B1"},
        ]
        evidence.extend({"kind": "xlsx_cell", "sheet": "报价",
                         "cell": f"B{row}", "value": 1.5,
                         "evidence_id": f"E-B{row}"}
                        for row in range(2, 62))
        suggestions = candidates._xlsx_header_suggestions(evidence)
        prices = [item for item in suggestions if item["field"] == "total_price"]
        self.assertEqual(len(prices), 60)
        self.assertEqual(prices[-1]["value"]["unit_hint"], "ten_thousand_yuan")
        self.assertEqual(parse_amount(prices[-1]["value"]["raw"],
                                      prices[-1]["value"]["unit_hint"])
                         ["amount_yuan"], "15000")

    def test_xlsx_identity_and_contact_headers_create_candidates(self):
        evidence = []
        headers = ("投标人名称", "主体编码", "联系电话", "项目经理",
                   "身份证件号码", "电子邮箱", "银行账户")
        values = ("甲公司", "B-1", "13800138000", "张三", "ID-1",
                  "a@example.com", "62220000")
        for col, (header, value) in enumerate(zip(headers, values), 1):
            letter = chr(64 + col)
            evidence.extend((
                {"kind": "xlsx_cell", "sheet": "资料", "cell": f"{letter}1",
                 "value": header, "evidence_id": f"H-{letter}",
                 "locator": f"资料!{letter}1"},
                {"kind": "xlsx_cell", "sheet": "资料", "cell": f"{letter}2",
                 "value": value, "evidence_id": f"V-{letter}",
                 "locator": f"资料!{letter}2"},
            ))
        found = {(item["field"], str(item["value"]))
                 for item in candidates._xlsx_header_suggestions(evidence)}
        self.assertTrue({("bidder_name", "甲公司"), ("bidder_code", "B-1"),
                         ("contact_phone", "13800138000"),
                         ("person_manager", "张三"),
                         ("id_number", "ID-1"),
                         ("contact_email", "a@example.com"),
                         ("bank_account", "62220000")} <= found)


class EightRuleHttpWorkflowTests(unittest.TestCase):
    """真实 HTTP 上传、确认、绑定、筛查与证据回指的最小合成闭环。"""

    @classmethod
    def setUpClass(cls):
        from biaojing import webapp, workspace

        cls.temp = tempfile.TemporaryDirectory(prefix="biaojing_all_rules_")
        cls.server = webapp.WorkbenchServer(
            ("127.0.0.1", 0), workspace.Workbench(cls.temp.name))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.workbench.close()
        cls.temp.cleanup()

    def request(self, path, body=None, content_type="application/json"):
        headers = {"Origin": self.base, "Content-Type": content_type}
        req = urllib.request.Request(self.base + path, data=body,
                                     headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def post_json(self, path, data):
        status, body = self.request(
            path, json.dumps(data, ensure_ascii=False).encode())
        self.assertEqual(status, 200, body.decode(errors="replace"))
        result = json.loads(body)
        self.assertTrue(result.get("ok"), result)
        return result

    def upload(self, name, data):
        path = "/api/upload?name=" + urllib.parse.quote(name, safe="")
        status, body = self.request(path, data, "application/octet-stream")
        self.assertEqual(status, 200, body.decode(errors="replace"))
        result = json.loads(body)
        if result.get("job_id"):
            self.fail("端到端小样本不应进入异步任务")
        return result["result"]["sha256"]

    def candidates(self):
        found = []
        offset = 0
        while True:
            status, body = self.request(
                f"/api/state?candidate_offset={offset}&candidate_limit=100")
            self.assertEqual(status, 200)
            page = json.loads(body)
            found.extend(page["candidates"])
            offset += len(page["candidates"])
            if not page["candidate_has_more"]:
                return found

    @staticmethod
    def make_workbook(event_index):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "投标资料"
        sheet.append(("主体编码", "投标人名称", "清单编码", "清单名称",
                      "规格型号", "单位", "数量", "综合单价(元)",
                      "总报价(元)", "币种", "是否含税", "中标结果",
                      "联系电话", "项目经理", "身份证件号码"))
        participants = (
            ("BID-A", "甲公司", 1_005_000, "未中标", "13800000001",
             "张三", "PERSON-SHARED", "ID-SHARED", 100),
            ("BID-B", "乙公司", 1_000_000, "中标", "13800000001",
             "张三", "PERSON-SHARED", "ID-SHARED", 110),
            ("BID-C", "丙公司", 1_003_000, "未中标", "13800000003",
             "张三", "PERSON-OTHER", "ID-OTHER", 120),
        )
        row_bidder = {}
        for bidder, name, total, outcome, phone, manager, _, id_number, price in participants:
            for line_index, factor in enumerate((1, 2), 1):
                sheet.append((bidder, name, f"ITEM-{line_index}",
                              f"清单项目{line_index}", f"规格{line_index}",
                              "项", 1, price * factor, total, "CNY", "含税",
                              outcome, phone, manager, id_number))
                row_bidder[sheet.max_row] = bidder
        stream = __import__("io").BytesIO()
        workbook.save(stream)
        workbook.close()
        return stream.getvalue(), row_bidder, participants

    @staticmethod
    def make_pdf(label):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((40, 60), f"合成投标文件 {label}")
        doc.set_metadata({"producer": "Synthetic Bid Device 1.0",
                          "creationDate": "D:20250101120000Z"})
        data = doc.tobytes()
        doc.close()
        return data

    def test_uploaded_documents_can_reach_all_eight_rules_and_open_evidence(self):
        event_specs = []
        for event_no in range(1, 4):
            data, row_bidder, participants = self.make_workbook(event_no)
            sha = self.upload(f"合成采购事件{event_no}.xlsx", data)
            event_specs.append((f"EVENT-{event_no}", sha, row_bidder,
                                participants))

        for event_id, sha, row_bidder, participants in event_specs:
            candidates = [item for item in self.candidates()
                          if item["sha256"] == sha]
            per_bidder = {}
            evidence_bidder = {}
            for candidate in candidates:
                match = re.search(r"!([A-Z]+)(\d+)$",
                                  candidate.get("locator_display", ""))
                if not match:
                    continue
                row = int(match.group(2))
                bidder = row_bidder.get(row)
                if bidder:
                    evidence_bidder[candidate["evidence_id"]] = bidder
                    per_bidder.setdefault(bidder, {}).setdefault(
                        candidate["field"], []).append(candidate)
            for candidate in candidates:
                if candidate["field"] != "price_lines":
                    continue
                lines = candidate["value"]
                bidder = evidence_bidder.get(lines[0].get("evidence")) if lines else None
                if bidder:
                    per_bidder.setdefault(bidder, {}).setdefault(
                        "price_lines", []).append(candidate)
            for bidder, _, total, outcome, phone, manager, person_id, id_number, _ in participants:
                fields = per_bidder[bidder]
                lot = "LOT-1"
                amount = fields["total_price"][0]
                self.post_json("/api/confirm_amount", {
                    "event_id": event_id, "lot_id": lot, "bidder_id": bidder,
                    "raw_value": str(total), "unit": "yuan", "currency": "CNY",
                    "tax_included": True, "evidence_id": amount["evidence_id"],
                    "action": "confirm"})
                self.post_json("/api/confirm", {
                    "event_id": event_id, "lot_id": lot, "bidder_id": bidder,
                    "field": "outcome_result",
                    "value": fields["outcome_result"][0]["value"],
                    "evidence_id": fields["outcome_result"][0]["evidence_id"],
                    "action": "confirm"})
                self.post_json("/api/confirm", {
                    "event_id": event_id, "lot_id": lot, "bidder_id": bidder,
                    "field": "contact_phone", "value": phone,
                    "evidence_id": fields["contact_phone"][0]["evidence_id"],
                    "source_role": "bidder", "action": "confirm"})
                self.post_json("/api/confirm", {
                    "event_id": event_id, "lot_id": lot, "bidder_id": bidder,
                    "field": "person_manager", "value": manager,
                    "evidence_id": fields["person_manager"][0]["evidence_id"],
                    "person_id": person_id, "action": "confirm"})
                self.post_json("/api/confirm", {
                    "event_id": event_id, "lot_id": lot, "bidder_id": bidder,
                    "field": "id_number", "value": id_number,
                    "evidence_id": fields["id_number"][0]["evidence_id"],
                    "person_id": person_id, "action": "confirm"})
                lines = fields["price_lines"][0]
                self.post_json("/api/confirm", {
                    "event_id": event_id, "lot_id": lot, "bidder_id": bidder,
                    "field": "price_lines", "value": lines["value"],
                    "evidence_id": lines["evidence_id"], "action": "confirm"})

        pdf_hashes = []
        for bidder in ("BID-A", "BID-B"):
            pdf_hashes.append((bidder, self.upload(
                f"{bidder}-合成标书.pdf", self.make_pdf(bidder))))
        for index, (bidder, sha) in enumerate(pdf_hashes):
            self.post_json("/api/bind_file", {
                "event_id": "EVENT-1", "lot_id": "LOT-1",
                "bidder_id": bidder, "sha256": sha, "context": "bid_document",
                "source_type": "bid_document",
                "declared_owner_id": "WRONG-OWNER" if index == 0 else bidder})

        status, body = self.request("/api/screen", b"{}")
        self.assertEqual(status, 200, body.decode(errors="replace"))
        result = json.loads(body)
        present = {finding["rule_id"] for finding in result["findings"]}
        self.assertEqual({f"R{i:03d}" for i in range(1, 9)} - present, set())
        for finding in result["findings"]:
            for evidence_id in finding["evidence_ids"]:
                code, body = self.request("/api/evidence/" + evidence_id)
                self.assertEqual(code, 200, evidence_id)
                self.assertEqual(json.loads(body)["evidence_id"], evidence_id)

        state = json.loads(self.request("/api/state")[1])
        self.assertEqual(state["last_screen_run"]["status"], "completed")
        self.assertGreater(state["confirmation_history_total"], 30)


if __name__ == "__main__":
    unittest.main()

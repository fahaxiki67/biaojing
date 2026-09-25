# -*- coding: utf-8 -*-
"""标镜 P3 候选字段抽取：对 P1 抽取证据做确定性标签匹配。

边界（任务书）：
  - 只读 P1 证据的原文/表格 value，输出 (字段, 候选值, evidence_id,
    locator, 解析说明)；正则不匹配、跨行歧义、字段冲突一律留给人工，
    绝不自动拼值，绝不自动确认。
  - evidence_id 在工作台边界确定性分配：E-{sha256 前 12 位}-{序号:04d}，
    同一来源（同字节）重复解析必得到相同 ID；P1 的 locator 与原文保持
    原样，P1 行为不受影响。
  - P1 的 locator 是显示字符串（"paragraph 5"/"page 1"/"Sheet!A1"）。
    送入 P2 的定位 dict 由本模块用 P1 的 typed 字段（kind/page/
    paragraph_index/sheet/cell/table_index/row/col/section_index）构造；
    原字符串另存 locator_display 供界面展示。
  - 清单单元格只对明确表头给出列建议；"总报价(万元)"等带单位表头显式
    按单位换算（×10000）并在说明中标注，绝不静默错标。
"""

from __future__ import annotations

import re
from collections import defaultdict

# 标签 → 字段（长标签优先匹配，避免"项目"吃掉"项目编码"）
FIELD_LABELS = {
    "project_code": ("项目编码", "项目编号"),
    "project_name": ("项目名称", "项目"),
    "lot_code": ("标段编码",),
    "lot_name": ("标段名称", "标段"),
    "event_code": ("采购事件编号", "采购编号", "招标编号"),
    "bidder_code": ("主体编码",),
    "bidder_name": ("投标人名称", "投标单位", "投标人"),
    "total_price": ("投标总报价", "报价总额", "投标总价", "投标报价", "总报价"),
    "legal_rep": ("法定代表人",),
    "person_manager": ("项目负责人", "项目经理"),
    "person_tech": ("技术负责人",),
    "authorize_rep": ("授权代表",),
    "contact_phone": ("联系电话", "电话"),
    "contact_email": ("电子邮箱", "邮箱"),
    "bank_account": ("银行账户", "账户"),
    "uscc": ("统一社会信用代码", "信用代码"),
    "id_number": ("身份证件号码", "证件号码"),
    "reg_number": ("注册编号",),
}

_PRICE_RE = re.compile(r"([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(?:元|万元)?")


def _build_label_pattern() -> re.Pattern:
    labels = []
    seen = set()
    for labels_group in FIELD_LABELS.values():
        for lb in labels_group:
            if lb not in seen:
                seen.add(lb)
                labels.append(lb)
    labels.sort(key=len, reverse=True)  # 长标签优先
    # 冒号可选：兼容"投标总报价为人民币…元"等无冒号标签写法
    return re.compile("(?:" + "|".join(labels) + ")\\s*[:：]?\\s*")


_LABEL_RE = _build_label_pattern()

_LABEL_TO_FIELD = {}
for _field, _lbs in FIELD_LABELS.items():
    for _lb in _lbs:
        _LABEL_TO_FIELD.setdefault(_lb, _field)


def assign_evidence_ids(sha256: str, evidence: list[dict]) -> list[dict]:
    """给 P1 证据列表分配确定性本地 evidence_id（不修改原条目字段）。

    返回新列表：每项 = {**evidence, "evidence_id": "E-{sha12}-{序号:04d}"}。
    同一 sha256 + 相同解析结果 → 相同 ID 序列（P1 解析是确定性的）。
    """
    prefix = "E-" + sha256[:12]
    out = []
    for i, e in enumerate(evidence, start=1):
        item = dict(e)
        item["evidence_id"] = f"{prefix}-{i:04d}"
        out.append(item)
    return out


def to_p2_locator(e: dict) -> dict | None:
    """P1 证据 → P2 定位 dict（用 typed 字段构造；locator 显示字符串不动）。

    返回 None 表示该证据无法构造有效 P2 定位（调用方不得以其触发）。
    """
    kind = e.get("kind")
    if kind == "pdf_page":
        page = e.get("page")
        if isinstance(page, int) and page >= 1:
            return {"kind": "pdf_page", "page": page}
        return None
    if kind == "docx_paragraph":
        pi = e.get("paragraph_index")
        if isinstance(pi, int) and pi >= 1:
            return {"kind": "docx_paragraph", "paragraph_index": pi}
        return None
    if kind == "docx_image":
        index = e.get("image_index")
        if isinstance(index, int) and index >= 1:
            return {"kind": "docx_image", "image_index": index}
        return None
    if kind == "docx_table_cell":
        try:
            return {"kind": "docx_table_cell",
                    "table_index": int(e["table_index"]),
                    "row": int(e["row"]), "col": int(e["col"])}
        except (KeyError, TypeError, ValueError):
            return None
    if kind in ("docx_header_paragraph", "docx_footer_paragraph"):
        try:
            return {"kind": kind, "section_index": int(e["section_index"]),
                    "paragraph_index": int(e["paragraph_index"])}
        except (KeyError, TypeError, ValueError):
            return None
    if kind in ("docx_header_table_cell", "docx_footer_table_cell"):
        try:
            return {"kind": kind, "section_index": int(e["section_index"]),
                    "table_index": int(e["table_index"]),
                    "row": int(e["row"]), "col": int(e["col"])}
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "xlsx_cell":
        sheet, cell = e.get("sheet"), e.get("cell")
        if isinstance(sheet, str) and sheet and isinstance(cell, str) and cell:
            return {"kind": "xlsx_cell", "sheet": sheet, "cell": cell}
        return None
    if kind == "xlsx_sheet_info":
        sheet = e.get("sheet")
        if isinstance(sheet, str) and sheet:
            return {"kind": "xlsx_sheet_info", "sheet": sheet}
        return None
    return None


def _clean(value: str) -> str:
    return value.strip().strip("，,。；;、").strip()


def _price_value(raw: str):
    seg = raw.replace("，", ",")
    m = _PRICE_RE.search(seg)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        return float(num)
    except ValueError:
        return None


def _extract_pairs(text: str) -> list[tuple[str, str]]:
    """从一行/一段文本中按标签切出 (字段, 值) 对。

    带冒号的标签作为切分锚点（值止于下一个锚点）；无冒号匹配仅接受
    total_price 且数值可解析（其余无冒号切分噪声过大，留给人工）。
    """
    pairs = []
    matches = list(_LABEL_RE.finditer(text))
    anchors = [m for m in matches
               if m.group(0).rstrip().endswith((":", "："))]
    for i, m in enumerate(anchors):
        label = m.group(0).rstrip(":：").strip()
        field = _LABEL_TO_FIELD.get(label)
        if field is None:
            continue
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        value = _clean(text[m.end():end])
        if value:
            pairs.append((field, value))
    for m in matches:
        if m.group(0).rstrip().endswith((":", "：")):
            continue
        field = _LABEL_TO_FIELD.get(m.group(0).rstrip(":：").strip())
        if field != "total_price":
            continue
        pv = _price_value(text[m.end():])
        if pv is not None:
            pairs.append(("total_price", pv))
    return pairs


def extract_from_text_evidence(evidence_id: str, p2_locator: dict,
                               locator_display: str, text: str) -> list[dict]:
    """对一段文本证据做标签匹配，返回候选列表。"""
    candidates = []
    for line in str(text).splitlines():
        if not line.strip():
            continue
        for field, value in _extract_pairs(line):
            note = "标签匹配"
            if field in ("contact_phone", "contact_email"):
                if any(k in line for k in ("代理", "公共", "平台")):
                    note += "；疑似代理/公共联系方式，确认前请核对来源角色"
            cand = {"field": field, "value": value,
                    "evidence_id": evidence_id,
                    "locator": p2_locator,
                    "locator_display": locator_display,
                    "note": note}
            if field == "total_price" and "人民币" in line:
                # 原文标明人民币：连带给出币种候选，随总报价一并确认
                cand["companion"] = {"field": "currency", "value": "CNY",
                                     "note": "由原文「人民币」推断，随总报价一并确认"}
            candidates.append(cand)
    return candidates


# XLSX 表头标签 → (字段, 换算倍率)。带单位的表头必须显式换算。
HEADER_LABELS = {
    "投标单位": ("bidder_name", 1),
    "投标人": ("bidder_name", 1),
    "投标人名称": ("bidder_name", 1),
    "总报价(元)": ("total_price", 1),
    "总报价": ("total_price", 1),
    "投标总价": ("total_price", 1),
    "总报价(万元)": ("total_price", 10000),
    "投标总价(万元)": ("total_price", 10000),
    "综合单价(元)": ("unit_price", 1),
    "综合单价": ("unit_price", 1),
    "联系电话": ("contact_phone", 1),
    "主体编码": ("bidder_code", 1),
    "清单编码": ("item_code", 1),
}


def _xlsx_header_suggestions(evidence: list[dict], max_per_sheet: int = 50):
    """XLSX 表头建议：明确表头标签所在列的下方非空单元格给候选。

    使用 P1 typed 字段（e["sheet"]/e["cell"]/e["value"]），不经 locator
    显示字符串解析。
    """
    cells = {}  # (sheet, col, row) -> (evidence_id, value)
    for e in evidence:
        if e.get("kind") != "xlsx_cell":
            continue
        sheet, cell = e.get("sheet"), e.get("cell")
        if not isinstance(sheet, str) or not sheet or not isinstance(cell, str):
            continue
        value = e.get("value")
        if isinstance(value, str) and value.startswith("="):
            continue  # 公式串不作建议
        m = re.match(r"([A-Z]+)(\d+)", cell)
        if not m:
            continue
        cells[(sheet, m.group(1), int(m.group(2)))] = (e.get("evidence_id"),
                                                       value)
    # 表头行：同 sheet 同 row 内 ≥2 个已知表头标签
    header_rows = defaultdict(list)
    for (sheet, col, row), (eid, value) in cells.items():
        if isinstance(value, str) and value.strip() in HEADER_LABELS:
            header_rows[(sheet, row)].append((col, eid, value.strip()))
    suggestions = []
    for (sheet, hrow), heads in header_rows.items():
        if len(heads) < 2:
            continue
        for col, header_eid, label in heads:
            field, scale = HEADER_LABELS[label]
            count = 0
            for (s, c, r), (eid, value) in sorted(
                    cells.items(), key=lambda kv: (kv[0][0], kv[0][2])):
                if s != sheet or c != col or r <= hrow:
                    continue
                if value is None or (isinstance(value, str)
                                     and not value.strip()):
                    continue
                note = f"由表头「{label}」推断，待人工确认"
                if isinstance(value, (int, float)) and scale != 1:
                    value = value * scale
                    note += f"；已按表头单位×{scale} 换算"
                sug = {"field": field, "value": value,
                       "evidence_id": eid,
                       "locator": {"kind": "xlsx_cell", "sheet": sheet,
                                   "cell": f"{c}{r}"},
                       "locator_display": f"{sheet}!{c}{r}",
                       "note": note}
                if field == "total_price" and "(元)" in label:
                    sug["companion"] = {"field": "currency", "value": "CNY",
                                        "note": "由表头「(元)」推断，随总报价一并确认"}
                suggestions.append(sug)
                count += 1
                if count >= max_per_sheet:
                    break
    return suggestions


def extract_candidates(sha256: str, doc_type: str,
                       evidence: list[dict]) -> list[dict]:
    """主入口：evidence 应已过 assign_evidence_ids。返回候选列表。"""
    candidates = []
    for e in evidence:
        eid = e.get("evidence_id")
        kind = e.get("kind")
        locator_display = e.get("locator") or ""
        p2_locator = to_p2_locator(e)
        if p2_locator is None:
            continue
        if kind == "xlsx_cell":
            value = e.get("value")
            if value is None:
                continue
            if isinstance(value, str) and value.startswith("="):
                continue  # 公式串不给候选（缓存值语义另由 P2 处理）
            for field, v in _extract_pairs(str(value)):
                candidates.append({
                    "field": field, "value": v, "evidence_id": eid,
                    "locator": p2_locator,
                    "locator_display": locator_display,
                    "note": "标签匹配"})
        elif kind in ("pdf_page", "docx_paragraph", "docx_image", "docx_table_cell",
                      "docx_header_paragraph", "docx_footer_paragraph",
                      "docx_header_table_cell", "docx_footer_table_cell"):
            text = e.get("text") or ""
            if text.strip():
                matches = extract_from_text_evidence(
                    eid, p2_locator, locator_display, text)
                if e.get("ocr_used"):
                    for candidate in matches:
                        candidate["note"] = "OCR 机器识别文本标签匹配，须对照原件人工复核"
                candidates.extend(matches)
    if doc_type == "xlsx":
        candidates.extend(_xlsx_header_suggestions(evidence))
    return candidates

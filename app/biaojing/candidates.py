# -*- coding: utf-8 -*-
"""标镜 P3 候选字段抽取：对 P1 抽取证据做确定性标签匹配。

边界（任务书）：
  - 只读 P1 证据的原文/表格 value，输出 (字段, 候选值, evidence_id,
    locator, 解析说明)；正则不匹配、跨行歧义、字段冲突一律留给人工，
    绝不自动拼值，绝不自动确认。
  - evidence_id 由完整文件 SHA-256 和物理定位生成，同一来源（同字节）
    重复解析得到相同 ID；P1 的 locator 与原文保持原样，P1 行为不受影响。
  - P1 的 locator 是显示字符串（"paragraph 5"/"page 1"/"Sheet!A1"）。
    送入 P2 的定位 dict 由本模块用 P1 的 typed 字段（kind/page/
    paragraph_index/sheet/cell/table_index/row/col/section_index）构造；
    原字符串另存 locator_display 供界面展示。
    - 清单单元格只对明确表头给出列建议；"总报价(万元)"等带单位表头显式
      按单位换算（×10000）并在说明中标注，绝不静默错标。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from .money import parse_amount

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
    """用完整文件哈希和类型定位生成稳定 ID，不依赖证据遍历顺序。"""
    out = []
    occurrences = defaultdict(int)
    locator_fields = ("kind", "page", "paragraph_index", "image_index",
                      "table_index", "row", "col", "section_index", "sheet",
                      "cell", "locator")
    for e in evidence:
        item = dict(e)
        location = {key: e[key] for key in locator_fields if key in e}
        if not location:
            location = {"kind": e.get("kind"), "locator": e.get("locator", "")}
        canonical = json.dumps(location, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
        occurrences[digest] += 1
        suffix = f"{digest}-{occurrences[digest]}" if occurrences[digest] > 1 else digest
        item["evidence_id"] = f"E-{sha256}-{suffix}"
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
        # 先清除冒号两侧空格再移除冒号；否则「标签： 值」会把冒号留在标签名里。
        label = m.group(0).strip().rstrip(":：").strip()
        field = _LABEL_TO_FIELD.get(label)
        if field is None:
            continue
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        value = _clean(text[m.end():end])
        if value:
            pairs.append((field, value))
    for m in matches:
        if m in anchors:
            continue
        field = _LABEL_TO_FIELD.get(m.group(0).rstrip(":：").strip())
        if field != "total_price":
            continue
        end = next((a.start() for a in anchors if a.start() > m.start()), len(text))
        value = re.split(r"[，；。\n（(]", text[m.end():end], maxsplit=1)[0].strip()
        if parse_amount(value)["number"] is not None:
            pairs.append(("total_price", value))
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
            amount = None
            candidate_value = value
            if field == "total_price":
                amount = parse_amount(value)
                candidate_value = {
                    "raw": value,
                    "number": amount["number"],
                    "unit_hint": amount["unit"],
                    "currency_hint": amount["currency"],
                    "amount_yuan": amount["amount_yuan"],
                    "status": amount["status"],
                }
                if amount["status"] == "normalized":
                    note += f"；单位已归一为元，标准金额 {amount['amount_yuan']}"
                elif amount["status"] != "needs_unit":
                    note += "；金额文本不唯一或单位冲突，须人工核对"
            if field in ("contact_phone", "contact_email"):
                if any(k in line for k in ("代理", "公共", "平台")):
                    note += "；疑似代理/公共联系方式，确认前请核对来源角色"
            cand = {"field": field, "value": candidate_value,
                    "evidence_id": evidence_id,
                    "locator": p2_locator,
                    "locator_display": locator_display,
                    "note": note}
            if amount and amount["currency"] != "unknown":
                cand["currency_hint"] = amount["currency"]
            elif amount and amount["unit"] == "yuan":
                cand["currency_hint"] = "CNY"
            candidates.append(cand)
    return candidates


# XLSX 表头标签 → (字段, 金额单位提示)。金额统一由服务端精确归一化。
HEADER_LABELS = {
    "投标单位": ("bidder_name", 1),
    "投标人": ("bidder_name", 1),
    "投标人名称": ("bidder_name", 1),
    "投标主体名称": ("bidder_name", 1),
    "主体编码": ("bidder_code", 1),
    "投标人编码": ("bidder_code", 1),
    "投标单位编码": ("bidder_code", 1),
    "总报价(元)": ("total_price", "yuan"),
    "总报价": ("total_price", "unknown"),
    "投标总价": ("total_price", "unknown"),
    "总报价(万元)": ("total_price", "ten_thousand_yuan"),
    "投标总价(万元)": ("total_price", "ten_thousand_yuan"),
    "清单名称": ("item_name", "unknown"),
    "项目名称": ("item_name", "unknown"),
    "规格型号": ("spec", "unknown"),
    "项目特征": ("spec", "unknown"),
    "单位": ("unit", "unknown"),
    "数量": ("qty", "unknown"),
    "综合单价(元)": ("unit_price", "yuan"),
    "综合单价": ("unit_price", "unknown"),
    "币种": ("currency", "unknown"),
    "是否含税": ("tax_included", "unknown"),
    "中标结果": ("outcome_result", "unknown"),
    "是否中标": ("outcome_result", "unknown"),
    "投标结果": ("outcome_result", "unknown"),
    "项目负责人": ("person_manager", 1),
    "项目经理": ("person_manager", 1),
    "技术负责人": ("person_tech", 1),
    "授权代表": ("authorize_rep", 1),
    "法定代表人": ("legal_rep", 1),
    "身份证件号码": ("id_number", 1),
    "证件号码": ("id_number", 1),
    "联系电话": ("contact_phone", 1),
    "电话": ("contact_phone", 1),
    "电子邮箱": ("contact_email", 1),
    "邮箱": ("contact_email", 1),
    "银行账户": ("bank_account", 1),
    "账户": ("bank_account", 1),
    "清单编码": ("item_code", 1),
}


def _outcome_value(value):
    text = str(value).strip().casefold()
    if any(token in text for token in ("未中标", "未中", "落标", "未获得", "否")):
        return "lost"
    if any(token in text for token in ("中标", "已中标", "是")):
        return "won"
    return value


def _xlsx_header_suggestions(evidence: list[dict]):
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
            field, unit_hint = HEADER_LABELS[label]
            for (s, c, r), (eid, value) in sorted(
                    cells.items(), key=lambda kv: (kv[0][0], kv[0][2])):
                if s != sheet or c != col or r <= hrow:
                    continue
                if value is None or (isinstance(value, str)
                                     and not value.strip()):
                    continue
                note = f"由表头「{label}」推断，待人工确认"
                suggested_value = value
                if field == "total_price":
                    amount = parse_amount(value, unit_hint)
                    suggested_value = {
                        "raw": str(value),
                        "number": amount["number"],
                        "unit_hint": amount["unit"],
                        "currency_hint": amount["currency"],
                        "amount_yuan": amount["amount_yuan"],
                        "status": amount["status"],
                    }
                    if amount["status"] == "normalized":
                        note += f"；标准金额 {amount['amount_yuan']} 元"
                    elif amount["status"] != "needs_unit":
                        note += "；金额值需人工核对"
                elif field == "outcome_result":
                    suggested_value = _outcome_value(value)
                sug = {"field": field, "value": suggested_value,
                       "evidence_id": eid,
                       "locator": {"kind": "xlsx_cell", "sheet": sheet,
                                   "cell": f"{c}{r}"},
                       "locator_display": f"{sheet}!{c}{r}",
                       "note": note}
                if field == "total_price":
                    sug["currency_hint"] = (
                        amount["currency"] if amount["currency"] != "unknown"
                        else "CNY" if amount["unit"] == "yuan" else "unknown")
                suggestions.append(sug)
    return suggestions


def _xlsx_price_line_candidates(evidence: list[dict]) -> list[dict]:
    """从具备主体列的明细表生成按投标人分组的整组报价候选。"""
    cells = {}
    for item in evidence:
        if item.get("kind") != "xlsx_cell":
            continue
        sheet, cell = item.get("sheet"), item.get("cell")
        match = re.fullmatch(r"([A-Z]+)(\d+)", str(cell or ""))
        if not isinstance(sheet, str) or not match:
            continue
        value = item.get("value")
        if isinstance(value, str) and value.startswith("="):
            value = item.get("cached_value")
        cells[(sheet, match.group(1), int(match.group(2)))] = item

    headers = defaultdict(dict)
    for (sheet, col, row), item in cells.items():
        value = item.get("value")
        if isinstance(value, str) and value.strip() in HEADER_LABELS:
            field, hint = HEADER_LABELS[value.strip()]
            headers[(sheet, row)][field] = (col, item, hint)

    grouped = defaultdict(list)
    group_evidence = defaultdict(set)
    group_primary = {}
    for (sheet, header_row), cols in headers.items():
        bidder_col = cols.get("bidder_code") or cols.get("bidder_name")
        price_col = cols.get("unit_price")
        if not bidder_col or not price_col:
            continue
        item_fields = [field for field in ("item_code", "item_name", "spec")
                       if field in cols]
        if not item_fields or not ("item_code" in cols
                                   or {"item_name", "spec"} <= set(cols)):
            continue
        for row_no in sorted({r for s, _, r in cells
                              if s == sheet and r > header_row}):
            bidder_cell = cells.get((sheet, bidder_col[0], row_no))
            price_cell = cells.get((sheet, price_col[0], row_no))
            if not bidder_cell or not price_cell:
                continue
            bidder_value = bidder_cell.get("value")
            price_value = price_cell.get("value")
            if bidder_value in (None, "") or price_value in (None, ""):
                continue
            parsed_price = parse_amount(price_value, price_col[2])
            line = {"item_code": "unknown", "item_name": "unknown",
                    "spec": "unknown", "unit": "unknown", "qty": None,
                    "unit_price": parsed_price["amount_yuan"],
                    "currency": "unknown", "tax_included": None,
                    "evidence": price_cell.get("evidence_id"),
                    "field_evidence": {"unit_price": price_cell.get("evidence_id")}}
            for field in ("item_code", "item_name", "spec", "unit", "qty",
                          "currency", "tax_included"):
                if field not in cols:
                    continue
                col, _, _ = cols[field]
                source = cells.get((sheet, col, row_no))
                if not source or source.get("value") in (None, ""):
                    continue
                val = source.get("value")
                if field == "currency" and str(val).strip() in ("人民币", "元", "CNY"):
                    val = "CNY"
                if field == "tax_included":
                    normalized_tax = str(val).strip().casefold()
                    val = (True if normalized_tax in ("是", "含税", "true", "yes")
                           else False if normalized_tax in
                           ("否", "不含税", "false", "no") else None)
                line[field] = val
                line["field_evidence"][field] = source.get("evidence_id")
                if source.get("evidence_id"):
                    group_evidence[(sheet, str(bidder_value).strip())].add(
                        source["evidence_id"])
            if parsed_price["status"] != "normalized":
                line["unit_price"] = None
            bidder_name = ""
            if "bidder_name" in cols:
                source = cells.get((sheet, cols["bidder_name"][0], row_no))
                bidder_name = str(source.get("value") or "") if source else ""
            bidder_key = str(bidder_value).strip()
            group_key = (sheet, bidder_key)
            grouped[group_key].append(line)
            group_primary.setdefault(group_key, price_cell)
            group_evidence[group_key].add(price_cell.get("evidence_id"))
            group_evidence[group_key].add(bidder_cell.get("evidence_id"))
            if bidder_name and "bidder_name" in cols:
                source = cells.get((sheet, cols["bidder_name"][0], row_no))
                if source:
                    group_evidence[group_key].add(source.get("evidence_id"))

    output = []
    for (sheet, bidder), lines in sorted(grouped.items()):
        ids = sorted(x for x in group_evidence[(sheet, bidder)] if x)
        if not ids:
            continue
        primary = group_primary[(sheet, bidder)]
        output.append({
            "field": "price_lines", "value": lines,
            "evidence_id": primary.get("evidence_id"), "evidence_ids": ids,
            "locator": to_p2_locator(primary),
            "locator_display": primary.get("locator") or
                f"{sheet}（{len(lines)} 条报价行）",
            "note": "按投标主体汇总的完整清单行；每行保留字段证据定位，须逐项核对后确认",
        })
    return output


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
        candidates.extend(_xlsx_price_line_candidates(evidence))
    return candidates

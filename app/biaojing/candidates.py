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
    - 中标结果语义归一（_outcome_value）：中标候选/候选推荐=candidate、
      最终中标/中标人=won、否决/废标=rejected、无效/无效标=invalid、
      未中标/落标=lost、识别不了=unknown，绝不猜。
    - 公式单元格的有效值取 cached_value；缓存缺失（"unknown"）或错误值
      （"#REF!" 等）一律降级为 None/留空，绝不解析公式串本身。
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
    "taxpayer_id": ("纳税人识别号",),
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

# 纳税人识别号（18 位，GB 32100 字符集：数字 + 去除 I/O/S/V/Z 的大写字母）
_TAXPAYER_ID_RE = re.compile(r"[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Za-z])")

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
    total_price（数值可解析）与 taxpayer_id（紧邻 18 位统一代码字符集，
    见 GB 32100：数字 + A-H/J-N/P/Q/R/T/U/W/X/Y，不含 I/O/S/V/Z），
    其余无冒号切分噪声过大，留给人工。
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
        if field == "total_price":
            end = next((a.start() for a in anchors if a.start() > m.start()), len(text))
            value = re.split(r"[，；。\n（(]", text[m.end():end], maxsplit=1)[0].strip()
            if parse_amount(value)["number"] is not None:
                pairs.append(("total_price", value))
        elif field == "taxpayer_id":
            # 无冒号变体：仅当紧邻的就是 18 位代码本体才给候选，绝不猜
            hit = _TAXPAYER_ID_RE.match(text, m.end())
            if hit:
                pairs.append(("taxpayer_id", hit.group(0)))
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
    "综合单价(万元)": ("unit_price", "ten_thousand_yuan"),
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
    "纳税人识别号": ("taxpayer_id", 1),
    "联系电话": ("contact_phone", 1),
    "电话": ("contact_phone", 1),
    "电子邮箱": ("contact_email", 1),
    "邮箱": ("contact_email", 1),
    "银行账户": ("bank_account", 1),
    "账户": ("bank_account", 1),
    "清单编码": ("item_code", 1),
}


# 中标结果语义归一：按优先级命中第一类；顺序即语义边界——
# 「中标候选人」同时含「中标」「候选」，必须先判候选；「否决」含「否」，
# 必须先于普通落标；都认不出就落 unknown，绝不猜。
_OUTCOME_RULES = (
    ("candidate", ("候选", "拟中标", "预中标", "推荐中标")),
    ("rejected", ("否决", "废标")),
    ("invalid", ("无效",)),
    ("lost", ("未中标", "未中", "落标", "未获得", "否")),
    ("won", ("中标", "是")),
)


def _outcome_value(value):
    text = str(value).strip().casefold()
    if not text:
        return "unknown"
    for outcome, tokens in _OUTCOME_RULES:
        if any(token in text for token in tokens):
            return outcome
    return "unknown"


# 公式缓存值语义：xlsx_parser 对公式单元格另存 cached_value（缓存缺失时
# 为 "unknown"，Excel 错误值为 "#REF!" 等字符串）。取有效值时：
# 公式串本身绝不解析；缓存缺失/错误一律降级为 None，留给人工。
_FORMULA_CACHE_UNKNOWN = "unknown"


def _cell_effective_value(item):
    """单元格有效值：公式单元格取缓存值；缓存缺失/错误返回 None。"""
    value = item.get("value")
    if isinstance(value, str) and value.startswith("="):
        cached = item.get("cached_value")
        if cached is None:
            return None
        if isinstance(cached, str):
            text = cached.strip()
            if not text or text == _FORMULA_CACHE_UNKNOWN:
                return None
            if text.startswith("#"):
                return None  # #REF!/#VALUE!/#DIV/0! 等错误缓存
            return text
        return cached
    return value


# 分节标记行（采购包/下一表等）：截断旧表头、切分报价分组；
# 合计标记行：本身不给建议，也截断旧表头。
_SECTION_MARK_PREFIXES = ("采购包", "标段", "分标", "包件", "包组",
                          "下一表", "下表")
_TOTAL_ROW_MARKS = ("合计", "总计", "小计", "汇总")


def _is_section_mark(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and text.startswith(_SECTION_MARK_PREFIXES)


def _is_total_mark(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    return any(mark in text for mark in _TOTAL_ROW_MARKS)


def _xlsx_header_suggestions(evidence: list[dict]):
    """XLSX 表头建议：明确表头标签所在列的下方非空单元格给候选。

    使用 P1 typed 字段（e["sheet"]/e["cell"]/e["value"]），不经 locator
    显示字符串解析。数据行只归属其上方最近的表头行：重复表头各自生效，
    分节标记行（采购包/下一表等）与合计行截断旧表头作用域——旧表头不得
    套到后续所有行，不同采购包不得仅凭工作表名或投标人名称互相套用。
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
    # 分节/合计标记行（截断点），按工作表归集
    cut_rows = defaultdict(list)
    for (sheet, _col, row), (_eid, value) in cells.items():
        if _is_section_mark(value) or _is_total_mark(value):
            cut_rows[sheet].append(row)
    suggestions = []
    for sheet in sorted({sheet for (sheet, _row) in header_rows}):
        rows = sorted({row for (s, row) in header_rows if s == sheet})
        rows = [row for row in rows if len(header_rows[(sheet, row)]) >= 2]
        cuts = sorted(set(cut_rows.get(sheet, ())))
        for idx, hrow in enumerate(rows):
            next_hrow = rows[idx + 1] if idx + 1 < len(rows) else None
            cut = next((r for r in cuts if r > hrow), None)
            # 旧表头作用域：到下一个表头行或最近的截断行（不含）为止
            span_end = min((x for x in (next_hrow, cut) if x is not None),
                           default=None)
            for col, header_eid, label in header_rows[(sheet, hrow)]:
                field, unit_hint = HEADER_LABELS[label]
                for (s, c, r), (eid, value) in sorted(
                        cells.items(), key=lambda kv: (kv[0][0], kv[0][2])):
                    if s != sheet or c != col or r <= hrow:
                        continue
                    if span_end is not None and r >= span_end:
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
    """从具备主体列的明细表生成按投标人分组的整组报价候选。

    公式单元格一律取缓存值（cached_value）参与解析；缓存缺失或为
    #REF! 等错误值时该字段降级（单价 None / 文本字段留 unknown），
    绝不把公式串本身当数值。数据行只归属其上方最近的表头行；分节
    标记行（采购包/下一表等）与合计行截断旧表头；分组键带分节边界，
    不同采购包即使同表同名也不合并。
    """
    cells = {}
    for item in evidence:
        if item.get("kind") != "xlsx_cell":
            continue
        sheet, cell = item.get("sheet"), item.get("cell")
        match = re.fullmatch(r"([A-Z]+)(\d+)", str(cell or ""))
        if not isinstance(sheet, str) or not match:
            continue
        cells[(sheet, match.group(1), int(match.group(2)))] = item

    # 表头行：同 sheet 同 row 内 ≥2 个已知表头标签（与表头建议口径一致，
    # 避免单个杂散标签行冒充表头截断作用域）
    label_rows = defaultdict(dict)
    for (sheet, col, row), item in cells.items():
        value = item.get("value")
        if isinstance(value, str) and value.strip() in HEADER_LABELS:
            field, hint = HEADER_LABELS[value.strip()]
            label_rows[(sheet, row)][field] = (col, item, hint)
    sheet_headers = defaultdict(list)
    for (sheet, row), cols in label_rows.items():
        if len(cols) >= 2:
            sheet_headers[sheet].append((row, cols))
    for sheet in sheet_headers:
        sheet_headers[sheet].sort(key=lambda pair: pair[0])

    # 分节/合计标记行
    section_rows = defaultdict(list)
    cut_rows = defaultdict(list)
    for (sheet, col, row), item in cells.items():
        value = item.get("value")
        if _is_section_mark(value):
            section_rows[sheet].append(row)
        if _is_section_mark(value) or _is_total_mark(value):
            cut_rows[sheet].append(row)
    for sheet in section_rows:
        section_rows[sheet].sort()

    grouped = defaultdict(list)
    group_evidence = defaultdict(set)
    group_primary = {}
    for sheet, hlist in sorted(sheet_headers.items()):
        cuts = sorted(set(cut_rows.get(sheet, ())))
        for idx, (header_row, cols) in enumerate(hlist):
            bidder_col = cols.get("bidder_code") or cols.get("bidder_name")
            price_col = cols.get("unit_price")
            if not bidder_col or not price_col:
                continue
            item_fields = [field for field in ("item_code", "item_name", "spec")
                           if field in cols]
            if not item_fields or not ("item_code" in cols
                                       or {"item_name", "spec"} <= set(cols)):
                continue
            next_hrow = hlist[idx + 1][0] if idx + 1 < len(hlist) else None
            cut = next((r for r in cuts if r > header_row), None)
            # 旧表头作用域：到下一个表头行或最近的截断行（不含）为止
            span_end = min((x for x in (next_hrow, cut) if x is not None),
                           default=None)
            for row_no in sorted({r for s, _, r in cells
                                  if s == sheet and r > header_row
                                  and (span_end is None or r < span_end)}):
                bidder_cell = cells.get((sheet, bidder_col[0], row_no))
                price_cell = cells.get((sheet, price_col[0], row_no))
                if not bidder_cell or not price_cell:
                    continue
                bidder_value = _cell_effective_value(bidder_cell)
                if bidder_value in (None, ""):
                    continue  # 主体未知（如公式无缓存）→ 行不归属，不猜
                if price_cell.get("value") in (None, ""):
                    continue  # 单价格本身为空
                price_value = _cell_effective_value(price_cell)
                parsed_price = parse_amount(
                    "" if price_value is None else price_value, price_col[2])
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
                    if not source:
                        continue
                    val = _cell_effective_value(source)
                    if val in (None, ""):
                        continue  # 缓存缺失/错误 → 字段留默认，留给人工
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
                    bidder_name = (str(_cell_effective_value(source) or "")
                                   if source else "")
                # 分组键带分节边界：不同采购包（或“下一表”分节）即使
                # 同工作表、同投标人名称也不合并
                section = 0
                for boundary in section_rows.get(sheet, ()):
                    if boundary <= row_no:
                        section = boundary
                    else:
                        break
                bidder_key = str(bidder_value).strip()
                group_key = (sheet, section, bidder_key)
                grouped[group_key].append(line)
                group_primary.setdefault(group_key, price_cell)
                group_evidence[group_key].add(price_cell.get("evidence_id"))
                group_evidence[group_key].add(bidder_cell.get("evidence_id"))
                if bidder_name and "bidder_name" in cols:
                    source = cells.get((sheet, cols["bidder_name"][0], row_no))
                    if source:
                        group_evidence[group_key].add(source.get("evidence_id"))

    output = []
    for (sheet, section, bidder), lines in sorted(grouped.items()):
        ids = sorted(x for x in group_evidence[(sheet, section, bidder)] if x)
        if not ids:
            continue
        primary = group_primary[(sheet, section, bidder)]
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
                continue  # 公式串不给候选；缓存值仅用于报价行（见下）
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

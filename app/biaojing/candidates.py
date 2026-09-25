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
      未中标/落标=lost、识别不了=unknown，绝不猜。F 线加固：否向线索
      （不/未/非/无/没/从未/并非/不是）或疑问线索（是否/吗/？/?/能否）
      出现时绝不 candidate/won；正反并存、否定的非显性落标「中标」
      一律 unknown；rejected 只认否决/废标/被否决。
    - 公式单元格的有效值取 cached_value；缓存缺失（"unknown"）或错误值
      （"#REF!" 等）一律降级为 None/留空，绝不解析公式串本身。
    - F 线（表头/报价行）：相邻标签行字段不相交时按双层表头合并成
      一个逻辑表头，无法安全合并时输出 header_structure 披露；整行
      全空的分隔行截断表头作用域（表内偶发空单元格不误伤）；同一
      表头行同字段多列时单价列不自动取值、文本列取第一列，均在组
      note 披露；币种归一白名单覆盖 RMB/￥/人民币（元）等常见写法，
      不识别币种一律 unknown，绝不把自定义字符串带进可比组。
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
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


# 中标结果语义归一（F 线钉死的保守口径）：
#   - 否向线索（不/未/非/无/没/从未/并非/不是）或疑问线索（是否/吗/？/?/能否）
#     出现时，绝不映射 candidate/won；
#   - 正反信号并存（候选标记 + 否定的中标，如「候选但从未中标」）→ unknown；
#   - 否定的「中标」若非显性落标词（未中标/未中/落标/未获得）→ unknown
#     而非 lost（「不是中标人」「未被认定为中标」不猜成落标）；
#   - rejected 只认明确词（否决/废标/被否决），裸「否」是落标不是否决，
#     「是否」里的「否」子串更不算；
#   - 显性落标→lost、否决/废标→rejected、无效→invalid、中标/是→won、
#     候选/拟中标/预中标/推荐中标→candidate、空/乱→unknown 全部保持；
#     认不出一律 unknown，绝不猜。
_OUTCOME_NEGATION_CUES = ("不", "未", "非", "无", "没", "从未", "并非", "不是")
_OUTCOME_QUESTION_CUES = ("是否", "吗", "？", "?", "能否")
_OUTCOME_CANDIDATE_TOKENS = ("候选", "拟中标", "预中标", "推荐中标")
_OUTCOME_REJECTED_TOKENS = ("否决", "废标")
_OUTCOME_LOST_TOKENS = ("未中标", "未中", "落标", "未获得")


def _outcome_value(value):
    text = str(value).strip().casefold()
    if not text:
        return "unknown"
    negated = any(cue in text for cue in _OUTCOME_NEGATION_CUES)
    questioned = any(cue in text for cue in _OUTCOME_QUESTION_CUES)
    # 否决/无效分支同样受否向与疑问守卫约束（复核四轮）：先剥离命中的
    # 状态词再判守卫——「无效」本身含否向字「无」，不剥离会自伤；
    # 「未被否决」「是否被否决？」「并非无效」一律 unknown，不猜
    rejected_hit = next((t for t in _OUTCOME_REJECTED_TOKENS if t in text),
                        None)
    if rejected_hit is not None:
        without = text.replace(rejected_hit, "")
        if any(cue in without for cue in _OUTCOME_NEGATION_CUES) or \
                any(cue in without for cue in _OUTCOME_QUESTION_CUES):
            return "unknown"
        return "rejected"
    if "无效" in text:
        without = text.replace("无效", "")
        if any(cue in without for cue in _OUTCOME_NEGATION_CUES) or \
                any(cue in without for cue in _OUTCOME_QUESTION_CUES):
            return "unknown"
        return "invalid"
    if any(token in text for token in _OUTCOME_CANDIDATE_TOKENS):
        if negated or questioned:
            return "unknown"  # 候选标记 + 否向/疑问并存 → 正反信号冲突，不猜
        return "candidate"
    lost_hit = next((token for token in _OUTCOME_LOST_TOKENS if token in text),
                    None)
    if lost_hit is not None:
        without = text.replace(lost_hit, "")
        if any(cue in without for cue in _OUTCOME_NEGATION_CUES) or \
                any(cue in without for cue in _OUTCOME_QUESTION_CUES):
            return "unknown"
        return "lost"
    if text.rstrip("。，,、；;．.") == "否":
        return "lost"  # 整格裸「否」= 未中标；「是否」等子串不在此列
    if negated or questioned:
        # 否定的「中标」非显性落标词、或疑问句 → 绝不 candidate/won/lost
        return "unknown"
    if "中标" in text or text.rstrip("。，,、；;．.") == "是":
        return "won"
    return "unknown"


# 公式缓存值语义：xlsx_parser 对公式单元格另存 cached_value（缓存缺失时
# 为 "unknown"，Excel 错误值为 "#REF!" 等字符串）。取有效值时：
# 公式串本身绝不解析；缓存缺失/错误一律降级为 None，留给人工。
_FORMULA_CACHE_UNKNOWN = "unknown"


# 币种归一（F 线缺口 3，扩白名单）：常见人民币写法全部归一为 CNY，
# 避免 R004 comparable_key 按币种字符串分组时把同币种可比行拆散；
# USD/EUR/JPY/HKD/GBP 等标准代码大小写不敏感、大写保留为各自币种；
# 其余非空不识别值一律置 unknown（走「币种未知不进组」保守路径），
# 绝不把自定义字符串带进 comparable_key。
_CURRENCY_CNY_ALIASES = frozenset((
    "cny", "rmb", "人民币", "人民币元", "人民币(元)", "人民币（元）",
    "rmb(元)", "rmb（元）", "元", "￥", "¥",
))
_CURRENCY_STANDARD_CODES = frozenset(("USD", "EUR", "JPY", "HKD", "GBP"))


def _normalize_currency_value(value):
    text = str(value).strip()
    if text.casefold() in _CURRENCY_CNY_ALIASES:
        return "CNY"
    if text.upper() in _CURRENCY_STANDARD_CODES:
        return text.upper()
    return "unknown"


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


def _is_blank_value(value):
    """单元格是否为空（None 或全空白字符串）。"""
    return value is None or (isinstance(value, str) and not value.strip())


def _pure_label_row_values(row_values):
    """双层表头合并护栏：一行内所有非空值是否全为已知表头标签。

    只有非空值全是表头标签的行才允许并入逻辑表头——防止把恰好长成
    标签样子的数据单元格（如品名「单位」）所在的数据行吃进表头。
    """
    for value in row_values.values():
        if _is_blank_value(value):
            continue
        if not (isinstance(value, str) and value.strip() in HEADER_LABELS):
            return False
    return True


def _merge_label_rows(rows_map, row_values):
    """相邻表头行合并（双层表头，F 线缺口 5a）。

    相邻两行各含 ≥1 个已知表头标签、字段不相交、且下一行非空值全部是
    表头标签时，合并为一个逻辑表头（列映射取并集，数据行从第二行之后
    开始）；字段重叠无法安全合并的相邻标签行作为疑似双层表头返回，由
    调用方披露，绝不静默归零。

    返回 (logical, conflicts)：
      logical   —— [(start_row, end_row, {field: entry})]，按 start_row 升序；
      conflicts —— [(row_a, row_b, "字段重叠：…")]。
    """
    rows = sorted(rows_map)
    logical = []
    conflicts = []
    i = 0
    while i < len(rows):
        start = rows[i]
        merged = dict(rows_map[start])
        end = start
        j = i + 1
        while j < len(rows) and rows[j] == end + 1:
            nxt = rows_map[rows[j]]
            overlap = sorted(set(merged) & set(nxt))
            if overlap:
                conflicts.append(
                    (end, rows[j], "字段重叠：" + "、".join(overlap)))
                break
            if not _pure_label_row_values(row_values.get(rows[j], {})):
                break  # 下一行含非表头内容，按普通行处理：不合并也不披露
            merged.update(nxt)
            end = rows[j]
            j += 1
        logical.append((start, end, merged))
        i = j
    return logical, conflicts


def _first_blank_row(rows_nonempty, after_row, before_row):
    """全空分隔行（F 线缺口 5b）：(after_row, before_row) 开区间内第一行
    不在 rows_nonempty（该表非空行集合）中的行号；只有整行（在已扫描
    列范围内）全空才截断，表内偶发空单元格行不受影响。无则返回 None。"""
    row = after_row + 1
    while before_row is None or row < before_row:
        if row not in rows_nonempty:
            return row
        row += 1
    return None


def _xlsx_header_suggestions(evidence: list[dict]):
    """XLSX 表头建议：明确表头标签所在列的下方非空单元格给候选。

    使用 P1 typed 字段（e["sheet"]/e["cell"]/e["value"]），不经 locator
    显示字符串解析。数据行只归属其上方最近的逻辑表头：重复表头各自生效，
    分节标记行（采购包/下一表等）、合计行与整行全空的分隔行截断旧表头
    作用域——旧表头不得套到后续所有行；相邻标签行字段不相交时按双层
    表头合并（与 price_lines 同口径，F 线缺口 5a/5b），数据行从第二层
    之后开始，孤立的但行标签数不足的行不当表头也不截断作用域。
    """
    cells = {}  # (sheet, col, row) -> (evidence_id, value)
    for e in evidence:
        if e.get("kind") != "xlsx_cell":
            continue
        sheet, cell = e.get("sheet"), e.get("cell")
        if not isinstance(sheet, str) or not sheet or not isinstance(cell, str):
            continue
        value = e.get("value")
        # 公式单元格在此**注册不排除**：仅含公式的行也要计入非空行集合，
        # 否则会被全空分隔行截断逻辑误判、静默截断表头作用域（F 线声明
        # 的建议路径公式行限制）；公式串在下方产出建议时才跳过
        m = re.match(r"([A-Z]+)(\d+)", cell)
        if not m:
            continue
        cells[(sheet, m.group(1), int(m.group(2)))] = (e.get("evidence_id"),
                                                       value)
    # 表头标签：同 sheet 同 row 内按列收集（同字段多列全部保留，逐列建议）
    label_cols = defaultdict(dict)  # (sheet,row) -> {col: (field, hint, label, eid)}
    for (sheet, col, row), (eid, value) in cells.items():
        if isinstance(value, str) and value.strip() in HEADER_LABELS:
            field, hint = HEADER_LABELS[value.strip()]
            label_cols[(sheet, row)][col] = (field, hint, value.strip(), eid)
    # 分节/合计标记行（截断点），按工作表归集
    cut_rows = defaultdict(list)
    for (sheet, _col, row), (_eid, value) in cells.items():
        if _is_section_mark(value) or _is_total_mark(value):
            cut_rows[sheet].append(row)
    # 非空行集合（整行全空分隔行截断作用域）与行值（双层表头合并护栏）
    nonempty_rows = defaultdict(set)
    row_values = defaultdict(dict)
    for (sheet, col, row), (_eid, value) in cells.items():
        if not _is_blank_value(value):
            nonempty_rows[sheet].add(row)
            row_values[sheet].setdefault(row, {})[col] = value
    suggestions = []
    for sheet in sorted({sheet for (sheet, _row) in label_cols}):
        col_maps = {row: cols for (s, row), cols in label_cols.items()
                    if s == sheet}
        field_maps = {}
        for row, cols in col_maps.items():
            fields = {}
            for col in sorted(cols):
                field, hint, label, eid = cols[col]
                fields.setdefault(field, (col, hint, label, eid))
            field_maps[row] = fields
        logical, _conflicts = _merge_label_rows(field_maps,
                                                row_values.get(sheet, {}))
        # 疑似双层表头披露由 price_lines 路径统一输出，这里不重复；
        # 不足 2 个标签的孤立标签行不当表头、也不截断前一表头的作用域
        usable = []
        for start_row, end_row, _fields in logical:
            label_count = sum(len(col_maps.get(r, {}))
                              for r in range(start_row, end_row + 1))
            if label_count >= 2:
                usable.append((start_row, end_row))
        cuts = sorted(set(cut_rows.get(sheet, ())))
        nonempty = nonempty_rows.get(sheet, set())
        for idx, (start_row, end_row) in enumerate(usable):
            next_header = (usable[idx + 1][0]
                           if idx + 1 < len(usable) else None)
            cut = next((r for r in cuts if r > end_row), None)
            blank = _first_blank_row(nonempty, end_row, next_header)
            # 旧表头作用域：到下一个逻辑表头、最近的截断行或全空分隔行
            #（不含）为止
            span_end = min((x for x in (next_header, cut, blank)
                            if x is not None), default=None)
            merged_cols = {}
            for r in range(start_row, end_row + 1):
                merged_cols.update(col_maps.get(r, {}))
            for col, (field, unit_hint, label,
                      _header_eid) in sorted(merged_cols.items()):
                for (s, c, r), (eid, value) in sorted(
                        cells.items(), key=lambda kv: (kv[0][0], kv[0][2])):
                    if s != sheet or c != col or r <= end_row:
                        continue
                    if span_end is not None and r >= span_end:
                        continue
                    if value is None or (isinstance(value, str)
                                         and not value.strip()):
                        continue
                    if isinstance(value, str) and value.startswith("="):
                        continue  # 公式串不作建议（缓存值语义见 price_lines）
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


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_letter(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _xlsx_price_line_candidates(evidence: list[dict]) -> list[dict]:
    """从具备主体列的明细表生成按投标人分组的整组报价候选。

    公式单元格一律取缓存值（cached_value）参与解析；缓存缺失或为
    #REF! 等错误值时该字段降级（单价 None / 文本字段留 unknown），
    绝不把公式串本身当数值。数据行只归属其上方最近的逻辑表头；分节
    标记行（采购包/下一表等）、合计行与整行全空的分隔行截断旧表头；
    相邻标签行字段不相交时按双层表头合并，数据行从第二层之后开始，
    无法安全合并的疑似双层表头输出披露（header_structure 候选）；
    同一逻辑表头行内同字段多列时单价列不自动取值、文本列取第一列，
    并一律在组 note 披露；分组键带分节边界，不同采购包即使同表同名
    也不合并。
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

    # 合并区域索引（P2-8 下半）：解析层已逐区域披露 xlsx_merged_range，
    # 此处换算成 (列起, 列止, 行起, 行止)，供纵向主体锚点回填与
    # 横向合并表头覆盖列识别；旧解析输出无该证据时行为与原先一致
    merge_spans = defaultdict(list)
    vertical_merge_ranges = defaultdict(list)
    for item in evidence:
        if item.get("kind") != "xlsx_merged_range":
            continue
        sheet = item.get("sheet")
        m = re.fullmatch(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?",
                         str(item.get("range") or ""))
        if not isinstance(sheet, str) or not m:
            continue
        c1, r1 = _col_index(m.group(1)), int(m.group(2))
        c2 = _col_index(m.group(3)) if m.group(3) else c1
        r2 = int(m.group(4)) if m.group(4) else r1
        start_col, end_col = min(c1, c2), max(c1, c2)
        start_row, end_row = min(r1, r2), max(r1, r2)
        merge_spans[sheet].append((start_col, end_col, start_row, end_row))
        if start_col == end_col and start_row < end_row:
            vertical_merge_ranges[(sheet, start_col)].append(
                (start_row, end_row))

    # 只对无重叠的纵向单列合并回填。二分查找保持逐行查找为 O(log n)，
    # 避免大表每个空主体都扫描最多 1000 个合并区域；重叠区域不自动归属。
    vertical_merge_starts = {}
    for key, spans in vertical_merge_ranges.items():
        spans.sort()
        if any(start <= previous_end
               for (_, previous_end), (start, _) in zip(spans, spans[1:])):
            continue
        vertical_merge_starts[key] = [start for start, _ in spans]

    # 表头行字段映射：同字段多列全部记入 dup_label_cols（含首列，按列序），
    # label_rows 只存首列（F 线缺口 4：原实现后者覆盖前者，静默取末列）；
    # 合并表头覆盖列以锚点标签识别字段，交给同字段多列的保守策略接管
    label_rows = defaultdict(dict)
    dup_label_cols = defaultdict(lambda: defaultdict(list))
    for (sheet, col, row), item in cells.items():
        value = item.get("value")
        label = value.strip() if isinstance(value, str) else None
        if label and label in HEADER_LABELS:
            field, hint = HEADER_LABELS[label]
            row_fields = label_rows[(sheet, row)]
            dup_label_cols[(sheet, row)][field].append(col)
            if field not in row_fields:
                row_fields[field] = (col, item, hint)

    # 合并表头覆盖列补注册（P2-8 下半）：被覆盖的空单元格不产证据，
    # 上面的逐格循环看不到它们——按锚点标签为首尾列补记同字段；完整
    # 合并范围另有 xlsx_merged_range 证据，避免按超宽范围逐列展开；
    # 由此形成的同字段多列交给既有重复字段保守策略（unit_price
    # 不自动取值并披露，文本取第一列），绝不自动选列
    for sheet, spans in merge_spans.items():
        for (c1, c2, r1, r2) in spans:
            if r1 != r2 or c1 == c2:
                continue  # 只处理单行横向合并表头
            anchor_cell = cells.get((sheet, _col_letter(c1), r1))
            anchor_value = (anchor_cell.get("value") if anchor_cell else None)
            label = (anchor_value.strip()
                     if isinstance(anchor_value, str) else None)
            if label not in HEADER_LABELS:
                continue
            field, hint = HEADER_LABELS[label]
            row_fields = label_rows[(sheet, r1)]
            known = dup_label_cols[(sheet, r1)][field]
            for col_idx in (c1,) if c1 == c2 else (c1, c2):
                col = _col_letter(col_idx)
                if col not in known:
                    known.append(col)
                row_fields.setdefault(field, (col, anchor_cell, hint))

    # 非空行集合（整行全空分隔行截断作用域）、行值（合并护栏）、
    # 分节/合计标记行
    nonempty_rows = defaultdict(set)
    sheet_row_values = defaultdict(dict)
    section_rows = defaultdict(list)
    cut_rows = defaultdict(list)
    for (sheet, col, row), item in cells.items():
        value = item.get("value")
        if not _is_blank_value(value):
            nonempty_rows[sheet].add(row)
            sheet_row_values[sheet].setdefault(row, {})[col] = value
        if _is_section_mark(value):
            section_rows[sheet].append(row)
        if _is_section_mark(value) or _is_total_mark(value):
            cut_rows[sheet].append(row)
    for sheet in section_rows:
        section_rows[sheet].sort()

    # 双层表头合并（F 线缺口 5a）+ 疑似双层表头披露（不许静默归零）
    sheet_field_maps = defaultdict(dict)
    for (sheet, row), row_fields in label_rows.items():
        sheet_field_maps[sheet][row] = row_fields
    logical_headers = {}
    disclosures = []
    for sheet, rows_map in sheet_field_maps.items():
        logical, conflicts = _merge_label_rows(
            rows_map, sheet_row_values.get(sheet, {}))
        # 不足 2 个字段的孤立标签行不当表头、也不截断前一表头作用域
        logical_headers[sheet] = [(start_row, end_row, fields)
                                  for start_row, end_row, fields in logical
                                  if len(fields) >= 2]
        for row_a, row_b, reason in conflicts:
            entry = next(iter(rows_map[row_a].values()))
            anchor = entry[1]
            disclosures.append({
                "field": "header_structure",
                "value": {"sheet": sheet, "rows": [row_a, row_b],
                          "reason": reason},
                "evidence_id": anchor.get("evidence_id"),
                "locator": to_p2_locator(anchor) or
                    {"kind": "xlsx_sheet_info", "sheet": sheet},
                "locator_display": anchor.get("locator") or
                    f"{sheet}!{entry[0]}{row_a}",
                "note": f"疑似双层表头，未自动成组（{reason}）；"
                        "须人工确认表头行后再抽取报价",
            })

    grouped = defaultdict(list)
    group_evidence = defaultdict(set)
    group_primary = {}
    group_notes = defaultdict(set)
    group_header_row = {}
    span_orphans = defaultdict(int)
    for sheet in sorted(logical_headers):
        hlist = logical_headers[sheet]
        cuts = sorted(set(cut_rows.get(sheet, ())))
        nonempty = nonempty_rows.get(sheet, set())
        for idx, (header_row, header_end, cols) in enumerate(hlist):
            bidder_col = cols.get("bidder_code") or cols.get("bidder_name")
            # 同一逻辑表头（含合并的多行）内同字段全部列；首列生效，余列披露
            #（仅统计真正重复（≥2 列）的字段）
            dup_fields = {}
            for part_row in range(header_row, header_end + 1):
                for field, cols_list in dup_label_cols.get(
                        (sheet, part_row), {}).items():
                    if len(cols_list) >= 2:
                        dup_fields.setdefault(field, []).extend(cols_list)
            dup_price = "unit_price" in dup_fields
            price_col = None if dup_price else cols.get("unit_price")
            if not bidder_col or (not price_col and not dup_price):
                continue
            item_fields = [field for field in ("item_code", "item_name", "spec")
                           if field in cols]
            if not item_fields or not ("item_code" in cols
                                       or {"item_name", "spec"} <= set(cols)):
                continue
            next_header = hlist[idx + 1][0] if idx + 1 < len(hlist) else None
            cut = next((r for r in cuts if r > header_end), None)
            blank = _first_blank_row(nonempty, header_end, next_header)
            # 旧表头作用域：到下一个逻辑表头、最近的截断行或全空分隔行
            #（不含）为止
            span_end = min((x for x in (next_header, cut, blank)
                            if x is not None), default=None)
            for row_no in sorted({r for s, _, r in cells
                                  if s == sheet and r > header_end
                                  and (span_end is None or r < span_end)}):
                # 分组键带分节边界：不同采购包（或“下一表”分节）即使
                # 同工作表、同投标人名称也不合并。分节号只依赖 row_no 与
                # section_rows[sheet]，在字段循环前即可确定——清单字段的
                # 组证据必须用同一三元组键（F 线缺口 2：原 (sheet, bidder)
                # 二元组键与输出端三元组键不匹配，清单证据从未进顶层）。
                section = 0
                for boundary in section_rows.get(sheet, ()):
                    if boundary <= row_no:
                        section = boundary
                    else:
                        break
                bidder_cell = cells.get((sheet, bidder_col[0], row_no))
                price_cell = (cells.get((sheet, price_col[0], row_no))
                              if price_col else None)
                if price_col and (not price_cell
                                  or price_cell.get("value") in (None, "")):
                    continue  # 无单价单元格或单价格本身为空
                bidder_value = (_cell_effective_value(bidder_cell)
                                if bidder_cell else None)
                merged_bidder = None
                if not bidder_cell or bidder_value in (None, ""):
                    # 主体缺失先试合并区域锚点回填（P2-8 下半）：纵向合并
                    # 的投标人列以锚点值归属，缺行不再静默看似完整；
                    # 锚点也为空（公式无缓存等）→ 维持不归属，不猜
                    bidder_idx = _col_index(bidder_col[0])
                    key = (sheet, bidder_idx)
                    starts = vertical_merge_starts.get(key, ())
                    ranges = vertical_merge_ranges.get(key, ())
                    index = bisect_right(starts, row_no) - 1
                    if index >= 0 and ranges[index][1] >= row_no:
                        anchor_row = ranges[index][0]
                        anchor_cell = cells.get(
                            (sheet, bidder_col[0], anchor_row))
                        anchor_value = (_cell_effective_value(anchor_cell)
                                        if anchor_cell else None)
                        if anchor_value not in (None, ""):
                            bidder_value = anchor_value
                            merged_bidder = (anchor_cell, bidder_idx,
                                             anchor_row)
                if merged_bidder is None and (not bidder_cell
                                              or bidder_value in (None, "")):
                    # 主体缺失（公式无缓存/疑似合并单元格缺行）：行不归属，
                    # 但有单价的行计数披露，绝不静默丢弃
                    if price_cell is not None:
                        span_orphans[(sheet, header_row)] += 1
                    continue
                bidder_key = str(bidder_value).strip()
                group_key = (sheet, section, bidder_key)
                group_header_row.setdefault(group_key, header_row)
                if price_col:
                    price_value = _cell_effective_value(price_cell)
                    formula_value = price_cell.get("value")
                    if (price_value is None and isinstance(formula_value, str)
                            and formula_value.startswith("=")):
                        locator = (price_cell.get("locator")
                                   or f"{sheet}!{price_col[0]}{row_no}")
                        group_notes[group_key].add(
                            f"{locator}：单价为公式但缓存值缺失或无效，"
                            "未推断金额；须回到原表核对")
                    elif (isinstance(formula_value, str)
                          and formula_value.startswith("=")):
                        locator = (price_cell.get("locator")
                                   or f"{sheet}!{price_col[0]}{row_no}")
                        group_notes[group_key].add(
                            f"{locator}：单价采用公式缓存值 {price_value}，"
                            "Excel 可能尚未重新计算；须人工核对")
                    parsed_price = parse_amount(
                        "" if price_value is None else price_value,
                        price_col[2])
                else:
                    parsed_price = None  # 重复单价列：不自动取值
                line = {"item_code": "unknown", "item_name": "unknown",
                        "spec": "unknown", "unit": "unknown", "qty": None,
                        "unit_price": (parsed_price["amount_yuan"]
                                       if parsed_price else None),
                        "currency": "unknown", "tax_included": None,
                        "evidence": (price_cell.get("evidence_id")
                                     if price_cell else None),
                        "field_evidence": (
                            {"unit_price": price_cell.get("evidence_id")}
                            if price_cell else {})}
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
                    if field == "currency":
                        val = _normalize_currency_value(val)
                    if field == "tax_included":
                        normalized_tax = str(val).strip().casefold()
                        val = (True if normalized_tax in ("是", "含税", "true", "yes")
                               else False if normalized_tax in
                               ("否", "不含税", "false", "no") else None)
                    line[field] = val
                    line["field_evidence"][field] = source.get("evidence_id")
                    if source.get("evidence_id"):
                        group_evidence[group_key].add(source["evidence_id"])
                if parsed_price and parsed_price["status"] != "normalized":
                    line["unit_price"] = None
                    if parsed_price["status"] == "needs_unit":
                        # 保留原金额文本，让人工可在逐行确认界面选择元/万元；
                        # 规则仍只消费经确认归一到元的 unit_price。
                        line["unit_price_raw"] = parsed_price["raw"]
                        line["amount_unit"] = "unknown"
                        locator = (price_cell.get("locator") if price_cell
                                   else f"{sheet}!{price_col[0]}{row_no}")
                        group_notes[group_key].add(
                            f"{locator}：单价单位未知，未归一且不进入 R004；"
                            "请人工核对后更正或标 unknown")
                elif parsed_price and "currency" not in cols:
                    # 与 total_price 的候选 metadata 统一：只有显式“元”表头
                    # 且金额文本无其他币种标记时推断 CNY，并保留表头证据与
                    # 待核提示；无单位/外币不猜，也不影响显式币种列。
                    header_cell = price_col[1] if price_col else None
                    if parsed_price["currency"] != "unknown":
                        line["currency"] = parsed_price["currency"]
                    elif price_col and price_col[2] == "yuan":
                        line["currency"] = "CNY"
                        header_eid = (header_cell.get("evidence_id")
                                      if header_cell else None)
                        if header_eid:
                            line["field_evidence"]["currency"] = header_eid
                            group_evidence[group_key].add(header_eid)
                        label = (header_cell.get("value")
                                 if header_cell else "元")
                        group_notes[group_key].add(
                            f"币种 CNY 由表头“{label}”推断，须人工核对")
                bidder_name = ""
                if "bidder_name" in cols:
                    source = cells.get((sheet, cols["bidder_name"][0], row_no))
                    if source is None and merged_bidder is not None:
                        source = merged_bidder[0]
                    bidder_name = (str(_cell_effective_value(source) or "")
                                   if source else "")
                grouped[group_key].append(line)
                group_primary.setdefault(group_key, price_cell or bidder_cell)
                if price_cell is not None:
                    group_evidence[group_key].add(price_cell.get("evidence_id"))
                if merged_bidder is not None:
                    anchor_cell, anchor_col_idx, anchor_row = merged_bidder
                    if anchor_cell is not None \
                            and anchor_cell.get("evidence_id"):
                        group_evidence[group_key].add(
                            anchor_cell["evidence_id"])
                    group_notes[group_key].add(
                        "投标人来自合并区域锚点 "
                        f"{_col_letter(anchor_col_idx)}{anchor_row}，"
                        "归属经回填，须人工核对")
                elif bidder_cell is not None:
                    group_evidence[group_key].add(bidder_cell.get("evidence_id"))
                if bidder_name and "bidder_name" in cols:
                    source = cells.get((sheet, cols["bidder_name"][0], row_no))
                    if source:
                        group_evidence[group_key].add(source.get("evidence_id"))
                for field, cols_list in dup_fields.items():
                    desc = "、".join(cols_list)
                    if field == "unit_price":
                        group_notes[group_key].add(
                            f"表头重复：unit_price 出现在 {desc} 列，"
                            "未自动取值，须人工指定")
                    else:
                        group_notes[group_key].add(
                            f"表头重复：{field} 出现在 {desc} 列，"
                            "已取第一列，须人工核对")

    output = []
    for (sheet, section, bidder), lines in sorted(grouped.items()):
        ids = sorted(x for x in group_evidence[(sheet, section, bidder)] if x)
        if not ids:
            continue
        key = (sheet, section, bidder)
        primary = group_primary[key]
        note = "按投标主体汇总的完整清单行；每行保留字段证据定位，须逐项核对后确认"
        extras = sorted(group_notes.get(key, ()))
        if extras:
            note += "；" + "；".join(extras)
        orphans = span_orphans.get((sheet, group_header_row.get(key)), 0)
        if orphans:
            note += (f"；检测到 {orphans} 行有单价但主体为空"
                     "（疑似合并单元格导致的缺行或公式无缓存），须人工核对")
        output.append({
            "field": "price_lines", "value": lines,
            "evidence_id": primary.get("evidence_id"), "evidence_ids": ids,
            "locator": to_p2_locator(primary),
            "locator_display": primary.get("locator") or
                f"{sheet}（{len(lines)} 条报价行）",
            "note": note,
        })
    output.extend(disclosures)
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

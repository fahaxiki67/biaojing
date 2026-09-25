# -*- coding: utf-8 -*-
"""标镜 P2 规则引擎：对已人工确认/归一化的结构化事实执行 R001—R008 筛查。

边界（任务书口径，不得越过）：
  - 输入是"已人工确认/明确归一化"的事实 JSON，不是 P1 原文抽取结果。
    规则引擎绝不把 P1 的原文抽取自动当成身份、角色或事实确认。
  - 未知值一律保持 unknown，不补零、不空串、不猜。
  - 身份只通过显式稳定 ID（bidder_id/person_id/event_id）关联；名称仅作
    显示，同名不合并。
  - 缺少可回指证据（evidence_id）的事实不得参与触发 finding。
  - 不输出串标概率或违法结论；signal 仅表示人工核查优先级提示。

事实 JSON schema（biaojing.p2/1）顶层：
  {"schema": "biaojing.p2/1",
   "events": [ {"event_id","event_name",
                "lots":[{"lot_id","lot_name",
                         "bids":[{"bidder_id","bidder_name","role",
                                  "uscc"?,"uscc_evidence"?,
                                  "total_price"?,"currency"?,"tax_included"?,
                                  "total_price_evidence"?,
                                  "outcome":{"status":"known|unknown","result"?},
                                  "evidence",
                                  "contacts":[{"kind","value","evidence"}],
                                  "persons":[{"person_id","name","id_number"?,
                                              "role","evidence"}],
                                  "price_lines":[{"item_code","item_name","spec",
                                                  "unit","qty","unit_price",
                                                  "currency","tax_included","evidence"}],
                                  "files":[{"file_ref","sha256","source_type",
                                            "context","declared_owner_id"?,
                                            "declared_uscc"?,"metadata"?,
                                            "evidence"}]}]}]}],
   "evidence": [ {"evidence_id","file_sha256","source_type","role",
                  "quote","locator"} ] }
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from .money import decimal_text

RULE_VERSION = "P2-1.1"
SCHEMA = "biaojing.p2/1"

# R004 数值口径（写死并逐 finding 披露）
PRICE_PRECISION = 2
REL_TOL = 1e-6

# R006 通用软件特征（小写包含匹配）：相同也只算待核信号
GENERIC_PRODUCERS = (
    "microsoft", "wps", "kingsoft", "libreoffice", "openoffice",
    "openpyxl", "pymupdf", "skia", "oracle", "adobe", "pdfium", "itext",
)

# R001 排除上下文：这些文件归属差异不算主体混用
R001_EXCLUDED_CONTEXTS = {
    "legal_performance": "合法业绩材料",
    "joint_venture_reference": "联合体引用材料",
    "tenderer_document": "招标人文件",
}

DEFAULT_THRESHOLDS = {
    "R004": {"min_comparable_bidders": 3, "min_rows_strong": 2},
    "R005": {"min_positive_bidders": 2, "range_ratio_max": 0.05, "cv_max": 0.03},
    "R006": {"near_window_seconds": 120},
    "R007": {"known_lost_min": 3, "above_winning_min": 3},
    "R008": {"common_events_min": 3},
}


def _finite_amount(value, allow_unknown: bool = False) -> bool:
    if value is None:
        return allow_unknown
    if allow_unknown and isinstance(value, str) and value.strip().casefold() == "unknown":
        return True
    if isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False


def _decimal(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("布尔值不是金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("金额不是有效十进制数") from exc
    if not result.is_finite():
        raise ValueError("金额不是有限值")
    return result


def _known(value) -> bool:
    return value is not None and bool(str(value).strip()) \
        and str(value).strip().casefold() != "unknown"


# ---------------------------------------------------------------- 事实模型

class Evidence:
    __slots__ = ("evidence_id", "file_sha256", "source_type", "role", "quote", "locator")

    def __init__(self, raw: dict):
        self.evidence_id = raw.get("evidence_id")
        self.file_sha256 = raw.get("file_sha256", "unknown")
        self.source_type = raw.get("source_type", "unknown")
        self.role = raw.get("role", "unknown")
        self.quote = raw.get("quote", "")
        self.locator = raw.get("locator", {})


class Contact:
    __slots__ = ("kind", "value", "source_role", "evidence")

    def __init__(self, raw: dict):
        self.kind = raw.get("kind", "unknown")
        self.value = raw.get("value")
        # 联系方式的来源角色（bidder/tenderer/agency/platform/public_service/
        # unknown）：只有显式可归属投标人的联系方式才参与交叉
        self.source_role = raw.get("source_role", "unknown")
        self.evidence = raw.get("evidence")


class Person:
    __slots__ = ("person_id", "name", "id_number", "role", "evidence")

    def __init__(self, raw: dict):
        self.person_id = raw.get("person_id")
        self.name = raw.get("name", "unknown")
        self.id_number = raw.get("id_number", "unknown")
        self.role = raw.get("role", "unknown")
        self.evidence = raw.get("evidence")


class PriceLine:
    __slots__ = ("item_code", "item_name", "spec", "unit", "qty",
                 "unit_price", "currency", "tax_included", "evidence",
                 "field_evidence")

    def __init__(self, raw: dict):
        self.item_code = raw.get("item_code", "unknown")
        self.item_name = raw.get("item_name", "unknown")
        self.spec = raw.get("spec", "unknown")
        self.unit = raw.get("unit", "unknown")
        self.qty = raw.get("qty")
        self.unit_price = raw.get("unit_price")
        self.currency = raw.get("currency", "unknown")
        self.tax_included = raw.get("tax_included")
        self.evidence = raw.get("evidence")
        self.field_evidence = raw.get("field_evidence") or {}

    def comparable_key(self) -> tuple:
        # 税口径三态（True/False/None=unknown）：None 不得折成 False，
        # unknown 不得与已知"不含税"混比
        return (self.item_code, self.item_name, self.spec, self.unit,
                self.currency, self.tax_included)


class FileRef:
    __slots__ = ("file_ref", "sha256", "source_type", "context",
                 "declared_owner_id", "declared_uscc", "metadata", "evidence")

    def __init__(self, raw: dict):
        self.file_ref = raw.get("file_ref", "unknown")
        self.sha256 = raw.get("sha256", "unknown")
        self.source_type = raw.get("source_type", "unknown")
        self.context = raw.get("context", "bid_document")
        self.declared_owner_id = raw.get("declared_owner_id")
        self.declared_uscc = raw.get("declared_uscc")
        self.metadata = raw.get("metadata") or {}
        self.evidence = raw.get("evidence")


class Bid:
    __slots__ = ("lot_id", "lot_name", "event_id", "bidder_id", "bidder_name",
                 "role", "uscc", "uscc_evidence", "total_price", "currency",
                 "tax_included", "total_price_evidence", "outcome_status",
                 "outcome_result", "evidence", "contacts", "persons",
                 "price_lines", "files")

    def __init__(self, raw: dict, lot_id: str, lot_name: str, event_id: str):
        self.lot_id = lot_id
        self.lot_name = lot_name
        self.event_id = event_id
        self.bidder_id = raw.get("bidder_id")
        self.bidder_name = raw.get("bidder_name", "unknown")
        self.role = raw.get("role", "bidder")
        self.uscc = raw.get("uscc")
        self.uscc_evidence = raw.get("uscc_evidence")
        self.total_price = raw.get("total_price")
        self.currency = raw.get("currency", "unknown")
        self.tax_included = raw.get("tax_included")
        self.total_price_evidence = raw.get("total_price_evidence")
        outcome = raw.get("outcome") or {}
        self.outcome_status = outcome.get("status", "unknown")
        self.outcome_result = outcome.get("result")  # known 时为 won/lost/...
        self.evidence = raw.get("evidence")
        self.contacts = [Contact(c) for c in raw.get("contacts", [])]
        self.persons = [Person(p) for p in raw.get("persons", [])]
        self.price_lines = [PriceLine(x) for x in raw.get("price_lines", [])]
        self.files = [FileRef(f) for f in raw.get("files", [])]


# 受支持的 P1 定位契约 kind 及其最小坐标要求（复核第 9 条）
_SUPPORTED_LOCATOR_KINDS = {
    "pdf_page": ("page",),
    "docx_paragraph": ("paragraph_index",),
    "docx_image": ("image_index",),
    "docx_table_cell": ("table_index", "row", "col"),
    "docx_header_paragraph": ("section_index", "paragraph_index"),
    "docx_header_table_cell": ("section_index", "table_index", "row", "col"),
    "docx_footer_paragraph": ("section_index", "paragraph_index"),
    "docx_footer_table_cell": ("section_index", "table_index", "row", "col"),
    "xlsx_cell": ("sheet", "cell"),
    "xlsx_sheet_info": ("sheet",),
}


def _locator_valid(locator) -> bool:
    if not isinstance(locator, dict):
        return False
    kind = locator.get("kind")
    if not isinstance(kind, str) or kind not in _SUPPORTED_LOCATOR_KINDS:
        return False
    for field in _SUPPORTED_LOCATOR_KINDS[kind]:
        v = locator.get(field)
        if field in ("sheet", "cell"):
            if not isinstance(v, str) or not v.strip():
                return False
        else:
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                return False
    return True


class FactBase:
    """已归一化事实集合 + 证据注册表。

    证据强制：任何将触发 finding 的事实，其 evidence_id 必须能在注册表
    解析且 locator 完整（kind 受支持、非空，坐标按 kind 校验）；否则该
    事实不参与触发，并记入 excluded_facts（输出中披露"缺证据/未核验"）。
    """

    def __init__(self):
        self.evidence: dict[str, Evidence] = {}
        self.bids: list[Bid] = []
        self.unresolved_evidence: list[str] = []
        self.excluded_facts: list[dict] = []

    @classmethod
    def from_dict(cls, data: dict) -> "FactBase":
        if data.get("schema") != SCHEMA:
            raise ValueError(f"schema 须为 {SCHEMA}，收到 {data.get('schema')!r}")
        fb = cls()
        for raw in data.get("evidence", []):
            ev = Evidence(raw)
            if ev.evidence_id:
                fb.evidence[ev.evidence_id] = ev
        for ev in data.get("events", []):
            event_id = ev.get("event_id")
            for lot in ev.get("lots", []):
                lot_id, lot_name = lot.get("lot_id"), lot.get("lot_name", "")
                for bid in lot.get("bids", []):
                    fb.bids.append(Bid(bid, lot_id, lot_name, event_id))
        fb.validate_prices()
        return fb

    def validate_prices(self) -> None:
        """所有结构化金额与税口径在规则边界统一校验。"""
        for bid in self.bids:
            if not _finite_amount(bid.total_price, allow_unknown=True):
                raise ValueError(
                    f"{bid.event_id}/{bid.lot_id}/{bid.bidder_id} 总报价须为有限数值、None 或 'unknown'，"
                    f"收到 {bid.total_price!r}")
            if bid.tax_included is not None and type(bid.tax_included) is not bool:
                raise ValueError(
                    f"{bid.event_id}/{bid.lot_id}/{bid.bidder_id} tax_included 只接受 True、False 或 None")
            for i, line in enumerate(bid.price_lines, start=1):
                if not _finite_amount(line.unit_price, allow_unknown=False):
                    raise ValueError(
                        f"{bid.event_id}/{bid.lot_id}/{bid.bidder_id} 清单行 {i} 单价须为有限数值或 None，"
                        f"收到 {line.unit_price!r}")
                if line.tax_included is not None and type(line.tax_included) is not bool:
                    raise ValueError(
                        f"{bid.event_id}/{bid.lot_id}/{bid.bidder_id} 清单行 {i} tax_included 只接受 True、False 或 None")

    def usable(self, evidence_id) -> bool:
        """证据可回指：存在于注册表且 locator 完整。"""
        if evidence_id is None:
            return False
        ev = self.evidence.get(evidence_id)
        if ev is None:
            return False
        return _locator_valid(ev.locator)

    def exclude(self, fact_kind: str, bid: "Bid", evidence_id,
                reason: str) -> None:
        """记录因证据缺失/无效（或来源角色不可归属）而被排除的事实。"""
        self.excluded_facts.append({
            "fact_kind": fact_kind,
            "event_id": bid.event_id,
            "lot_id": bid.lot_id,
            "bidder_id": bid.bidder_id,
            "evidence_id": evidence_id,
            "reason": reason,
        })

    def resolve(self, evidence_id) -> Evidence | None:
        if evidence_id is None:
            return None
        ev = self.evidence.get(evidence_id)
        if ev is None:
            self.unresolved_evidence.append(str(evidence_id))
        return ev

    def bidder_bids(self) -> dict[str, list[Bid]]:
        out = defaultdict(list)
        for b in self.bids:
            out[b.bidder_id].append(b)
        return out

    def by_event(self) -> dict[str, list[Bid]]:
        out = defaultdict(list)
        for b in self.bids:
            out[b.event_id].append(b)
        return out


# ---------------------------------------------------------------- finding

def make_finding(rule_id: str, scope: dict, inputs: dict, params: dict,
                 trigger_reason: str, evidence_ids: list,
                 limitations: list, alternatives: list,
                 signal: str = "线索") -> dict:
    return {
        "rule_id": rule_id,
        "rule_version": RULE_VERSION,
        "scope": scope,
        "inputs": inputs,
        "params": params,
        "trigger_reason": trigger_reason,
        "evidence_ids": list(evidence_ids),
        "limitations": limitations,
        "alternative_explanations": alternatives,
        "review_status": "pending_manual",
        "signal": signal,
    }


def _close(a: float, b: float) -> bool:
    a, b = _decimal(a), _decimal(b)
    scale = max(abs(a), abs(b), Decimal(1))
    return abs(a - b) <= scale * Decimal(str(REL_TOL))


# ---------------------------------------------------------------- R001

def screen_r001(fb: FactBase, thresholds: dict) -> list[dict]:
    """主体混用：投标主体与文件归属/名称/统一社会信用代码冲突。

    排除：招标人文件、合法业绩、联合体引用上下文；declared unknown 不判。
    证据要求：文件归属冲突由文件证据单独支持；**统一社会信用代码冲突
    必须文件侧与主体侧（uscc_evidence）证据均可解析**才触发，缺任一侧
    则排除该冲突并记入 excluded_facts。
    """
    findings = []
    for bid in fb.bids:
        if bid.role != "bidder":
            continue
        for f in bid.files:
            if not fb.usable(f.evidence):
                fb.exclude("file", bid, f.evidence,
                           "evidence_missing_or_invalid" if f.evidence
                           else "evidence_absent")
                continue
            if f.context in R001_EXCLUDED_CONTEXTS:
                continue
            conflicts = []
            ev_ids = [f.evidence]
            if f.declared_owner_id is not None and f.declared_owner_id != bid.bidder_id:
                conflicts.append(
                    f"文件 {f.file_ref} 声明归属主体 {f.declared_owner_id}，"
                    f"与投标主体 {bid.bidder_id} 不一致")
            if (f.declared_uscc is not None and bid.uscc is not None
                    and f.declared_uscc != bid.uscc):
                if fb.usable(bid.uscc_evidence):
                    conflicts.append(
                        f"文件 {f.file_ref} 声明统一社会信用代码 {f.declared_uscc}，"
                        f"与投标主体 {bid.bidder_id} 的 {bid.uscc} 不一致")
                    if bid.uscc_evidence not in ev_ids:
                        ev_ids.append(bid.uscc_evidence)
                else:
                    fb.exclude(
                        "uscc_conflict", bid, bid.uscc_evidence,
                        "uscc_conflict_missing_subject_evidence"
                        "（主体侧 USCC 证据缺失/无效，冲突不触发）")
            if not conflicts:
                continue
            findings.append(make_finding(
                "R001",
                scope={"event_id": bid.event_id, "lot_id": bid.lot_id,
                       "bidder_id": bid.bidder_id, "file_ref": f.file_ref},
                inputs={"declared_owner_id": f.declared_owner_id,
                        "declared_uscc": f.declared_uscc,
                        "bidder_uscc": bid.uscc,
                        "file_context": f.context},
                params={"excluded_contexts": sorted(R001_EXCLUDED_CONTEXTS),
                        "uscc_evidence_policy": "USCC 冲突须双方字段证据均可解析"},
                trigger_reason="；".join(conflicts),
                evidence_ids=ev_ids,
                limitations=["仅基于已归一化声明字段，同一主体多证件/更名须人工确认"],
                alternatives=["文件可能误贴、模板复用或录入错误"],
            ))
    return findings


# ---------------------------------------------------------------- R002

def screen_r002(fb: FactBase, thresholds: dict) -> list[dict]:
    """联系方式交叉：跨独立投标主体的电话/邮箱/账户一致。

    双重排除：
      - bid.role != bidder（招标人/代理/平台等主体）不参与；
      - contact.source_role 必须显式为 "bidder"——投标文件中抄录的平台/
        代理/公共服务电话（source_role=platform/agency/public_service）
        或来源 unknown 的联系方式不参与交叉。
    证据缺失/无效的联系方式不触发（记入 excluded_facts）。
    """
    by_value = defaultdict(list)  # (kind, value) -> [(bid, contact)] 已过校验
    for bid in fb.bids:
        if bid.role != "bidder":
            continue
        for c in bid.contacts:
            if not c.value or c.value == "unknown":
                continue
            if c.source_role != "bidder":
                fb.exclude("contact", bid, c.evidence,
                           f"contact_source_role_{c.source_role}")
                continue
            if not fb.usable(c.evidence):
                fb.exclude("contact", bid, c.evidence,
                           "evidence_missing_or_invalid" if c.evidence
                           else "evidence_absent")
                continue
            by_value[(c.kind, str(c.value))].append((bid, c))
    findings = []
    for (kind, value), occs in sorted(by_value.items()):
        distinct = {b.bidder_id for b, _ in occs}
        if len(distinct) < 2:
            continue
        findings.append(make_finding(
            "R002",
            scope={"contact_kind": kind, "contact_value": value,
                   "bidder_ids": sorted(distinct)},
            inputs={"occurrences": [
                {"event_id": b.event_id, "lot_id": b.lot_id,
                 "bidder_id": b.bidder_id, "source_role": c.source_role,
                 "evidence": c.evidence}
                for b, c in occs]},
            params={"role_excluded": ["tenderer", "agency", "platform",
                                      "public_service", "非 bidder 主体角色"],
                    "contact_source_role_required": "bidder",
                    "excluded_source_roles": ["tenderer", "agency",
                                              "platform", "public_service",
                                              "unknown"]},
            trigger_reason=f"{kind} {value} 出现在 {len(distinct)} 个不同投标主体"
                           "（仅统计可归属投标人的联系方式）",
            # 只引用实际通过校验的 occurrence 证据，绝不混入失效项
            evidence_ids=sorted({c.evidence for _, c in occs}),
            limitations=["联系方式可能为公共电话、代填或历史号码；须人工核实归属"],
            alternatives=["同一代理机构代联系、集团内共享座机、录入错误"],
        ))
    return findings


# ---------------------------------------------------------------- R003

def screen_r003(fb: FactBase, thresholds: dict) -> list[dict]:
    """人员交叉：以稳定 person_id 为**唯一**关联键。

    证件号缺失（unknown）或矛盾不拆开同一 person_id，只在 finding 中
    披露证件差异作人工核对提示；同名但 person_id 不同 → 仅"待核信号"
    提示，不判定同一人；person_id 缺失/unknown 不参与。
    """
    by_pid = defaultdict(list)
    same_name_diff_id = defaultdict(list)
    for bid in fb.bids:
        for p in bid.persons:
            if not p.person_id or p.person_id == "unknown":
                continue
            if not fb.usable(p.evidence):
                fb.exclude("person", bid, p.evidence,
                           "evidence_missing_or_invalid" if p.evidence
                           else "evidence_absent")
                continue
            by_pid[p.person_id].append((bid, p))
            same_name_diff_id[p.name].append((bid, p))
    findings = []
    for pid, occ in sorted(by_pid.items()):
        bidders = sorted({b.bidder_id for b, _ in occ})
        if len(bidders) < 2:
            continue
        id_variants = sorted({p.id_number for _, p in occ})
        id_conflict = len(id_variants) > 1
        limitations = ["同一人可合法服务于多家（离职、兼职、借调），须人工核实时点"]
        if id_conflict:
            limitations.append(
                f"组内证件号存在 {len(id_variants)} 种取值（{id_variants}），"
                "含 unknown 或矛盾，须人工核对，不影响按稳定 person_id 关联")
        findings.append(make_finding(
            "R003",
            scope={"person_id": pid, "bidder_ids": bidders},
            inputs={"occurrences": [
                {"event_id": b.event_id, "lot_id": b.lot_id,
                 "bidder_id": b.bidder_id, "role": p.role,
                 "person_name": p.name, "id_number": p.id_number,
                 "evidence": p.evidence}
                for b, p in occ]},
            params={"identity_key": "person_id（唯一关联键；证件号不参与分键）",
                    "id_number_variants": id_variants,
                    "id_number_conflict": id_conflict},
            trigger_reason=f"显式身份 {pid} 出现在 {len(bidders)} 个不同投标主体",
            evidence_ids=sorted({p.evidence for _, p in occ}),
            limitations=limitations,
            alternatives=["人员流动、挂靠借用、证件录入错误"],
        ))
    # 同名不同 person_id：仅人工核对提示
    for name, occ in sorted(same_name_diff_id.items()):
        keys = {p.person_id for _, p in occ}
        if len(keys) < 2:
            continue
        bidders = sorted({b.bidder_id for b, _ in occ})
        if len(bidders) < 2:
            continue
        findings.append(make_finding(
            "R003",
            scope={"person_name": name, "bidder_ids": bidders},
            inputs={"distinct_person_ids": len(keys),
                    "occurrences": [
                        {"event_id": b.event_id, "bidder_id": b.bidder_id,
                         "person_id": p.person_id, "evidence": p.evidence}
                        for b, p in occ]},
            params={"identity_key": "person_id"},
            trigger_reason=f"同名人员「{name}」携带 {len(keys)} 个不同显式身份 ID，"
                           "仅列人工核对，不判定同一人",
            evidence_ids=sorted({p.evidence for _, p in occ}),
            limitations=["同名极常见；本条不构成人员交叉认定"],
            alternatives=["同名的不同自然人"],
            signal="待核信号",
        ))
    return findings


# ---------------------------------------------------------------- R004

def detect_pattern(values: list[float]):
    """行内模式检测；金额采用十进制，完全相同与等差分开报告。

    等差与等比为两种独立可触发模式；等比要求全部报价为正。
    """
    values = [_decimal(value) for value in values]
    n = len(values)
    if n > 1 and all(value == values[0] for value in values[1:]):
        return "完全相同", ["0"] * (n - 1)
    steps = [round(values[i + 1] - values[i], PRICE_PRECISION)
             for i in range(n - 1)]
    if steps and all(_close(s, steps[0]) for s in steps[1:]):
        return "等差", [decimal_text(step) for step in steps]
    if all(v > 0 for v in values):
        ratios = [round(values[i + 1] / values[i], 8) for i in range(n - 1)]
        if ratios and all(_close(r, ratios[0]) for r in ratios[1:]):
            return "等比", [decimal_text(ratio) for ratio in ratios]
    return None


def screen_r004(fb: FactBase, thresholds: dict) -> list[dict]:
    """报价规律：可比清单行内/跨行等差或等比模式。

    口径：
      - 比较组键 = (event_id, lot_id, 可比键)。**不同事件/标段的同名清单
        绝不拼成同一竞价组**；"跨行"只统计同一 (event_id, lot_id) 内的
        不同可比行。
      - 清单编码已知，或名称与规格均已知；单位、币种、税口径也必须已知。
        unknown 字段不因哨兵值相同而组成可比组。
      - 组内有效投标人 ≥3 才检测；以首个全体报价唯一的参考行确定主体顺序，
        再用于同标段其他行；单行筛查按金额排序，不依赖 bidder_id；
        精度 round(2)、相对容差 1e-6。
      - 等差与等比为两种独立模式，逐行输出模式、排序、匹配/未匹配
        分子分母与证据。
      - 同 (event_id, lot_id) 内 ≥2 行成模式且投标人排序一致 → 线索；
        否则仅单行等差/等比 → 弱线索。
      - 字段不足或证据缺失/无效的行不参与（记入 excluded_facts）。
    """
    fb.validate_prices()
    min_b = thresholds["R004"]["min_comparable_bidders"]
    min_rows_strong = thresholds["R004"]["min_rows_strong"]
    groups = defaultdict(dict)  # (event,lot,comp_key) -> {bidder_id: line}
    excluded_rows = []
    for bid in fb.bids:
        if bid.role != "bidder":
            continue
        for line in bid.price_lines:
            missing = []
            if not (_known(line.item_code)
                    or (_known(line.item_name) and _known(line.spec))):
                missing.append("清单编码未知且名称/规格不完整")
            if not _known(line.unit):
                missing.append("单位未知")
            if not _known(line.currency):
                missing.append("币种未知")
            if type(line.tax_included) is not bool:
                missing.append("税口径未知")
            if line.unit_price is None:
                missing.append("单价缺失")
            if not fb.usable(line.evidence):
                missing.append("证据缺失/无效" if line.evidence else "证据缺失")
            if missing:
                reason = "；".join(missing)
                fb.exclude("price_line", bid, line.evidence, reason)
                excluded_rows.append({
                    "event_id": bid.event_id, "lot_id": bid.lot_id,
                    "bidder_id": bid.bidder_id, "evidence_id": line.evidence,
                    "unit_price": line.unit_price,
                    "reason": reason,
                })
                continue
            key = (bid.event_id, bid.lot_id) + line.comparable_key()
            groups[key].setdefault(bid.bidder_id, line)

    pattern_rows = []   # 达门槛且呈模式的竞价组
    unmatched_no_pattern = 0            # 达门槛但无模式
    unmatched_below = 0                 # 投标人不足门槛
    reference_orders = {}
    for key, per_bidder in sorted(groups.items(), key=lambda kv: str(kv[0])):
        lot_key = key[:2]
        if len(per_bidder) < min_b or lot_key in reference_orders:
            continue
        prices = [_decimal(line.unit_price) for line in per_bidder.values()]
        if len(set(prices)) == len(prices):
            reference_orders[lot_key] = tuple(
                bidder for bidder, _ in sorted(
                    per_bidder.items(), key=lambda pair: _decimal(pair[1].unit_price)))

    for key, per_bidder in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(per_bidder) < min_b:
            unmatched_below += 1
            continue
        reference = reference_orders.get(key[:2])
        fixed_identity_order = bool(reference and set(reference) == set(per_bidder))
        if fixed_identity_order:
            ordered = [(bidder, per_bidder[bidder]) for bidder in reference]
        else:
            # 同价时 ID 只稳定展示顺序；只要存在价差，检测结果由价格序列决定。
            ordered = sorted(per_bidder.items(),
                             key=lambda pair: (_decimal(pair[1].unit_price), pair[0]))
        values = [round(_decimal(line.unit_price), PRICE_PRECISION)
                  for _, line in ordered]
        pattern = detect_pattern(values)
        if pattern is None:
            unmatched_no_pattern += 1
            continue
        pattern_rows.append({
            "event_id": key[0], "lot_id": key[1],
            "comparable_key": [str(x) for x in key[2:]],
            "bidders_in_fixed_order": [b for b, _ in ordered],
            "values": [decimal_text(value) for value in values],
            "mode": pattern[0], "steps_or_ratios": pattern[1],
            "evidence": [line.evidence for _, line in ordered],
            "field_evidence": [line.field_evidence for _, line in ordered],
            "identity_order_confirmed": fixed_identity_order,
        })

    total_groups = len(groups)          # 全部可比组
    matched = len(pattern_rows)         # 达门槛且呈模式
    threshold_groups = matched + unmatched_no_pattern
    findings = []
    if pattern_rows:
        # "跨行"只在同一 (event_id, lot_id) 内成立
        by_lot = defaultdict(list)
        for row in pattern_rows:
            if row["identity_order_confirmed"] and row["mode"] != "完全相同":
                by_lot[(row["event_id"], row["lot_id"])].append(row)
        strong_lots = []
        for (event_id, lot_id), rows in sorted(by_lot.items()):
            if len(rows) >= min_rows_strong:
                orders = {tuple(r["bidders_in_fixed_order"]) for r in rows}
                if len(orders) == 1:
                    strong_lots.append((event_id, lot_id, rows))
        strong = bool(strong_lots)
        all_ev = [e for r in pattern_rows for e in r["evidence"] if e]
        all_ev.extend(e for row in pattern_rows for field_map in row["field_evidence"]
                      for e in field_map.values() if e and fb.usable(e))
        detail_keys = ("event_id", "lot_id", "comparable_key",
                       "bidders_in_fixed_order", "values", "mode",
                       "steps_or_ratios", "evidence", "field_evidence",
                       "identity_order_confirmed")
        findings.append(make_finding(
            "R004",
            scope={"strong_lot_groups": [
                {"event_id": e, "lot_id": l, "rows": len(rows)}
                for e, l, rows in strong_lots],
                "weak_only": not strong},
            inputs={"comparable_rows_matched": matched,
                    "comparable_rows_unmatched_no_pattern": unmatched_no_pattern,
                    "comparable_groups_below_threshold": unmatched_below,
                    "comparable_rows_excluded": len(excluded_rows),
                    "excluded_rows_detail": excluded_rows[:50],
                    "excluded_rows_detail_truncated": len(excluded_rows) > 50,
                    "matched_ratio": (round(matched / threshold_groups, 4)
                                      if threshold_groups else 0.0),
                    "statistics_unit": "竞价组=(event_id, lot_id, 可比键)；"
                                       "分母只含达投标人门槛的组，"
                                       "不足门槛组单独披露",
                    "rows_detail": [
                        {k: r[k] for k in detail_keys}
                        for r in pattern_rows[:10]]},
            params={"grouping": "比较组键 = event_id + lot_id + 可比键"
                                "（不同事件/标段绝不拼组）",
                    "min_comparable_bidders": min_b,
                    "sort": "首个全体报价唯一的参考行按价格升序固定主体顺序；"
                            "无参考行时仅按价格序列作弱筛查",
                    "precision": PRICE_PRECISION,
                    "tolerance": f"相对 {REL_TOL}",
                    "modes": ["完全相同", "等差", "等比"],
                    "unmatched_definition": "竞价组有效投标人不足 min_comparable_bidders"},
            trigger_reason=(
                f"{matched} 个竞价组呈行内模式（等差/等比）；"
                + (f"{len(strong_lots)} 个标段内 ≥{min_rows_strong} 行且投标人固定排序一致，跨行对应关系保持"
                   if strong else
                   "未在同一标段内形成多行一致模式，单行等差/等比为弱线索")),
            evidence_ids=sorted(set(all_ev)),
            limitations=["等差/等比可源于定额取费、同源清单模板或巧合，不构成协同证明"],
            alternatives=["同一定额组价、同一软件默认取值、行业惯常报价结构"],
            signal="线索" if strong else "弱线索",
        ))
    return findings, {"comparable_rows_matched": matched,
                      "comparable_rows_unmatched_no_pattern": unmatched_no_pattern,
                      "comparable_groups_below_threshold": unmatched_below,
                      "comparable_groups_total": total_groups,
                      "comparable_rows_excluded": len(excluded_rows),
                      "excluded_rows_detail": excluded_rows[:50],
                      "excluded_rows_detail_truncated": len(excluded_rows) > 50}


# ---------------------------------------------------------------- R005

def screen_r005(fb: FactBase, thresholds: dict) -> list[dict]:
    """总报价集中：极差率=(max-min)/有效报价均值，CV=总体标准差/均值。

    统计分母按 (event_id, lot_id) 划分——**不同标段的报价不进同一分母**。
    至少两家正数有效报价才计算；分母为零或数据 unknown 明确不计算、不补
    零；报价证据缺失/无效时不进入分母（记入 excluded_facts）。
    """
    fb.validate_prices()
    min_n = thresholds["R005"]["min_positive_bidders"]
    th_rr = thresholds["R005"]["range_ratio_max"]
    th_cv = thresholds["R005"]["cv_max"]
    findings = []
    # 分母 = (event_id, lot_id, 币种, 税口径)：币种/税口径互异或 unknown
    # 的报价互相隔离，绝不进同一统计分母
    groups = defaultdict(list)
    excluded_by_group = defaultdict(list)
    for bid in fb.bids:
        if bid.role != "bidder":
            continue
        key = (bid.event_id, bid.lot_id)
        p = bid.total_price
        if p is None or p == "unknown":
            excluded_by_group[key].append({"bidder_id": bid.bidder_id,
                                           "reason": "报价 unknown"})
            continue
        if not _finite_amount(p):
            excluded_by_group[key].append({"bidder_id": bid.bidder_id,
                                           "reason": "报价非数值"})
            continue
        amount = _decimal(p)
        if amount <= 0:
            excluded_by_group[key].append({"bidder_id": bid.bidder_id,
                                           "reason": "报价非正数"})
            continue
        if bid.currency in ("unknown", None, ""):
            excluded_by_group[key].append({"bidder_id": bid.bidder_id,
                                           "reason": "币种 unknown，不进入统计分母"})
            continue
        if bid.tax_included is None:
            excluded_by_group[key].append({"bidder_id": bid.bidder_id,
                                           "reason": "税口径 unknown，不进入统计分母"})
            continue
        if not fb.usable(bid.total_price_evidence):
            fb.exclude("total_price", bid, bid.total_price_evidence,
                       "evidence_missing_or_invalid" if bid.total_price_evidence
                       else "evidence_absent")
            excluded_by_group[key].append({"bidder_id": bid.bidder_id,
                                           "reason": "报价证据缺失/无效，不进入分母"})
            continue
        groups[key + (bid.currency, bid.tax_included)].append((bid, amount))

    # excluded-only 组（该 event+lot 只有被排除报价）也参与"不计算"披露：
    # 合成为 (event_id, lot_id, None, None) 的虚拟口径组；若同 (event, lot)
    # 已有真实口径组，则其排除明细已并入该组，不重复造组
    for (event_id, lot_id), excl in excluded_by_group.items():
        if not any(k[0] == event_id and k[1] == lot_id for k in groups):
            groups.setdefault((event_id, lot_id, None, None), [])

    for key3 in sorted(groups, key=str):
        event_id, lot_id, currency, tax = key3[0], key3[1], key3[2], key3[3]
        pairs = groups[key3]
        excluded = excluded_by_group.get((event_id, lot_id), [])
        prices = [p for _, p in pairs]
        ev_ids = [b.total_price_evidence for b, _ in pairs
                  if b.total_price_evidence]
        base = {"event_id": event_id, "lot_id": lot_id,
                "currency": currency, "tax_included": tax,
                "prices": [decimal_text(p) for p in prices],
                "excluded": excluded,
                "n_positive": len(prices), "n_min": min_n}
        if len(prices) < min_n:
            if excluded or prices:
                findings.append(make_finding(
                    "R005",
                    scope={"event_id": event_id, "lot_id": lot_id,
                           "currency": currency, "tax_included": tax},
                    inputs=base,
                    params={"formula": "极差率=(max-min)/有效报价均值；CV=总体标准差/均值"},
                    trigger_reason="有效正数报价不足，按口径明确不计算（不补零）",
                    evidence_ids=[],
                    limitations=["样本不足时本规则不产出任何集中度数值"],
                    alternatives=[],
                    signal="不计算",
                ))
            continue
        with localcontext() as ctx:
            ctx.prec = 32
            mean = sum(prices, Decimal(0)) / Decimal(len(prices))
        if mean == 0:
            findings.append(make_finding(
                "R005", scope={"event_id": event_id, "lot_id": lot_id,
                               "currency": currency, "tax_included": tax},
                inputs=base,
                params={"formula": "极差率=(max-min)/有效报价均值；CV=总体标准差/均值"},
                trigger_reason="有效报价均值为 0，分母为零，明确不计算",
                evidence_ids=[], limitations=[], alternatives=[],
                signal="不计算"))
            continue
        rng = max(prices) - min(prices)
        range_ratio = rng / mean
        with localcontext() as ctx:
            ctx.prec = 32
            variance = sum(((p - mean) ** 2 for p in prices), Decimal(0)) \
                / Decimal(len(prices))
            pstdev = variance.sqrt()
        cv = pstdev / mean
        range_threshold = Decimal(str(th_rr))
        cv_threshold = Decimal(str(th_cv))
        if range_ratio <= range_threshold or cv <= cv_threshold:
            findings.append(make_finding(
                "R005",
                scope={"event_id": event_id, "lot_id": lot_id,
                       "currency": currency, "tax_included": tax},
                inputs={**base,
                        "mean": decimal_text(round(mean, 4)),
                        "pstdev": decimal_text(round(pstdev, 4)),
                        "max": decimal_text(max(prices)),
                        "min": decimal_text(min(prices))},
                params={"formula": "极差率=(max-min)/有效报价均值；CV=总体标准差/均值",
                        "range_ratio": float(round(range_ratio, 6)),
                        "cv": float(round(cv, 6)),
                        "threshold_range_ratio_max": th_rr,
                        "threshold_cv_max": th_cv,
                        "denominator_scope": "event_id + lot_id + 币种 + 税口径"
                                             "（互异或 unknown 的口径隔离，"
                                             "不同标段不进同一分母）",
                        "threshold_note": "可调筛查参数，非法定标准"},
                trigger_reason=(f"极差率 {range_ratio:.4f} ≤ {th_rr} 或 "
                                f"CV {cv:.4f} ≤ {th_cv}，总报价呈高度集中"),
                evidence_ids=sorted(set(ev_ids)),
                limitations=["报价集中可源于同一预算定额、市场价透明或清单约束"],
                alternatives=["业主预算控制价约束、市场竞争充分、同源计价软件"],
            ))
    return findings


# ---------------------------------------------------------------- R006

def _parse_creation(ts):
    """解析文档内创建时间：返回 (datetime, 时区标注, 是否含时间部分) 或 None。

    aware 一律规范化到 UTC 再比较；naive 保持原值，且不得与 aware 跨类比较。
    仅接受 ISO 8601 风格（含常见空格分隔）；解析失败返回 None，绝不猜。
    """
    if not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw or raw.casefold() == "unknown":
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    has_time = len(raw) > 10 or (dt.hour or dt.minute or dt.second) != 0
    if dt.tzinfo is not None:
        tz_label = str(dt.tzinfo) or "UTC"
        dt = dt.astimezone(timezone.utc)
    else:
        tz_label = "未标注时区(naive)"
    return dt, tz_label, has_time


_R006_CLUSTER_LIMITATIONS = [
    "创建时间相近或同日不能单独证明共同编制；元数据时间可被复制或由统一平台生成",
    "相同软件型号或版本不代表同一台物理设备，不得据其推断同一来源设备",
    "时间为文档内元数据的创建时间，不是文件系统导入/复制时间；"
    "导入/复制时间不得当作原件创建时间",
]

_R006_CLUSTER_ALTERNATIVES = [
    "平台转码或另存为会复制/改写创建时间戳",
    "同一模板或批量导出生成",
    "统一扫描平台",
    "同版办公软件",
]


def screen_r006(fb: FactBase, thresholds: dict) -> list[dict]:
    """文件属性相似：相同元数据值 + 来源/时间披露；通用软件或来源
    unknown 降为待核信号，单独不能证明共同编制。

    时间聚集子分析（相近但不完全相同 / 同日）：
      - 完全相等的 (producer, creation_date) 仍走原分组；
      - 创建时间可解析时，aware 一律规范化到 UTC；naive 与 aware 不跨类比较；
      - 相近窗口（默认 120 秒，可用 thresholds R006.near_window_seconds 覆盖）
        内成"时间相近"簇；同日但超出窗口成"同日聚集"弱信号簇；
      - 聚簇一律"待核信号"，保留 Creator/Producer、原始时间、时区、规范化
        时间与窗口理由；同一批文件已被完全相等分组覆盖时不再重复上报。
    """
    window = int(thresholds.get("R006", {}).get("near_window_seconds", 120))
    by_sig = defaultdict(list)
    timed = []  # (dt, tz_label, has_time, bid, file)
    for bid in fb.bids:
        if bid.role != "bidder":
            continue
        for f in bid.files:
            if not fb.usable(f.evidence):
                fb.exclude("file", bid, f.evidence,
                           "evidence_missing_or_invalid" if f.evidence
                           else "evidence_absent")
                continue
            producer = (f.metadata.get("producer") or "unknown").strip()
            created = (f.metadata.get("creation_date") or "unknown").strip()
            sig = (producer.lower(), created)
            by_sig[sig].append((bid, f))
            parsed = _parse_creation(created)
            if parsed is not None:
                dt, tz_label, has_time = parsed
                timed.append((dt, tz_label, has_time, bid, f))
    findings = []
    exact_sets: list[frozenset] = []
    for (producer, created), occ in sorted(by_sig.items()):
        bidders = sorted({b.bidder_id for b, _ in occ})
        if len(bidders) < 2:
            continue
        created_unknown = created == "unknown"
        if producer == "unknown" and created_unknown:
            continue  # 无任何属性可比较
        generic = any(g in producer for g in GENERIC_PRODUCERS)
        unknown_src = any(f.source_type in ("unknown", "", None) for _, f in occ)
        reasons = []
        if generic:
            reasons.append(f"producer「{producer}」属通用软件")
        if unknown_src:
            reasons.append("存在来源 unknown 的文件")
        if created_unknown:
            reasons.append("创建时间缺失，不构成时间一致")
        # created unknown 本身即降级：不得声称创建时间一致
        degraded = generic or unknown_src or created_unknown
        exact_sets.append(frozenset((b.bidder_id, f.file_ref) for b, f in occ))
        findings.append(make_finding(
            "R006",
            scope={"producer": producer, "creation_date": created,
                   "bidder_ids": bidders},
            inputs={"files": [
                {"event_id": b.event_id, "lot_id": b.lot_id,
                 "bidder_id": b.bidder_id, "file_ref": f.file_ref,
                 "sha256": f.sha256[:16] + "…", "source_type": f.source_type,
                 "evidence": f.evidence}
                for b, f in occ]},
            params={"generic_producer_list": list(GENERIC_PRODUCERS),
                    "degraded": degraded,
                    "degrade_reasons": reasons,
                    "unknown_time_policy":
                        "creation_date 缺失不得作为相等值参与匹配"},
            trigger_reason=(
                (f"{len(bidders)} 个投标主体的文件 producer 相同"
                 f"（producer={producer!r}）" if created_unknown else
                 f"{len(bidders)} 个投标主体的文件具有相同元数据"
                 f"（producer={producer!r}, creation_date={created!r}）")
                + ("；因" + "、".join(reasons) + "，降为待核信号" if reasons else "")),
            evidence_ids=sorted({f.evidence for _, f in occ}),
            limitations=["相同通用软件不能单独证明共同编制；元数据可被复制或由统一平台生成"],
            alternatives=["统一扫描平台、同版办公软件、模板同源"],
            signal="待核信号" if degraded else "线索",
        ))

    # ---- 时间聚集子分析（相近但不完全相同 / 同日）----
    if len(timed) < 2:
        return findings
    parent = list(range(len(timed)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(len(timed)):
        dt_i, _, has_i, _, _ = timed[i]
        for j in range(i + 1, len(timed)):
            dt_j, _, has_j, _, _ = timed[j]
            # naive 与 aware 不得跨类比较；无时间部分不得判秒级相近
            if (dt_i.tzinfo is None) != (dt_j.tzinfo is None):
                continue
            if not (has_i and has_j):
                continue
            if abs((dt_i - dt_j).total_seconds()) <= window:
                union(i, j)
    groups = defaultdict(list)
    for i in range(len(timed)):
        groups[find(i)].append(i)
    tight_sets: list[frozenset] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        entries = [timed[i] for i in members]
        bidders = sorted({b.bidder_id for _, _, _, b, _ in entries})
        if len(bidders) < 2:
            continue
        key_set = frozenset((b.bidder_id, f.file_ref) for _, _, _, b, f in entries)
        if any(key_set <= s for s in exact_sets):
            continue  # 已被完全相等分组覆盖，不重复上报
        tight_sets.append(key_set)
        dts = [dt for dt, _, _, _, _ in entries]
        span = int((max(dts) - min(dts)).total_seconds())
        findings.append(make_finding(
            "R006",
            scope={"cluster_mode": "tight_window", "normalized_date":
                   min(dts).date().isoformat(), "bidder_ids": bidders},
            inputs={"files": [
                {"event_id": b.event_id, "lot_id": b.lot_id,
                 "bidder_id": b.bidder_id, "file_ref": f.file_ref,
                 "sha256": f.sha256[:16] + "…",
                 "creator": (f.metadata.get("creator") or "unknown"),
                 "producer": ((f.metadata.get("producer") or "unknown").strip()),
                 "creation_date": ((f.metadata.get("creation_date")
                                    or "unknown").strip()),
                 "timezone": tz_label, "normalized_time": dt.isoformat(),
                 "has_time": has_time, "source_type": f.source_type,
                 "evidence": f.evidence}
                for dt, tz_label, has_time, b, f in entries]},
            params={"cluster_mode": "tight_window",
                    "near_window_seconds": window,
                    "window_rationale":
                        f"组内创建时间最大相差 {span} 秒，处于 {window} 秒"
                        "相近窗口内（相近但不完全相同）",
                    "time_source": "document_internal_metadata"
                                   "（文档内元数据创建时间，非导入/复制时间）",
                    "generic_producer_list": list(GENERIC_PRODUCERS),
                    "degraded": True,
                    "degrade_reasons": ["创建时间相近但不完全相同，弱于完全一致"]},
            trigger_reason=(
                f"{len(bidders)} 个投标主体的文件创建时间相近但不完全相同"
                f"（组内最大相差 {span} 秒，≤{window} 秒窗口），降为待核信号"),
            evidence_ids=sorted({f.evidence for _, _, _, _, f in entries}),
            limitations=list(_R006_CLUSTER_LIMITATIONS),
            alternatives=list(_R006_CLUSTER_ALTERNATIVES),
            signal="待核信号",
        ))
    # 同日聚集：同规范化日期且同类（aware/naive），不受相近窗口约束
    day_groups = defaultdict(list)
    for dt, tz_label, has_time, b, f in timed:
        day_groups[(dt.date(), dt.tzinfo is None)].append(
            (dt, tz_label, has_time, b, f))
    for (_, _), entries in sorted(
            day_groups.items(), key=lambda kv: str(kv[0][0])):
        bidders = sorted({b.bidder_id for _, _, _, b, _ in entries})
        if len(bidders) < 2:
            continue
        key_set = frozenset((b.bidder_id, f.file_ref) for _, _, _, b, f in entries)
        if any(key_set <= s for s in exact_sets) \
                or any(key_set <= s for s in tight_sets):
            continue  # 完全相等或更近的簇已覆盖
        dts = [dt for dt, _, _, _, _ in entries]
        date_label = min(dts).date().isoformat()
        if all(has for _, _, has, _, _ in entries):
            span = int((max(dts) - min(dts)).total_seconds())
            span_note = f"，组内时间跨度 {span} 秒"
        else:
            span_note = "（部分文件仅有日期、无时间部分）"
        findings.append(make_finding(
            "R006",
            scope={"cluster_mode": "same_day",
                   "normalized_date": date_label, "bidder_ids": bidders},
            inputs={"files": [
                {"event_id": b.event_id, "lot_id": b.lot_id,
                 "bidder_id": b.bidder_id, "file_ref": f.file_ref,
                 "sha256": f.sha256[:16] + "…",
                 "creator": (f.metadata.get("creator") or "unknown"),
                 "producer": ((f.metadata.get("producer") or "unknown").strip()),
                 "creation_date": ((f.metadata.get("creation_date")
                                    or "unknown").strip()),
                 "timezone": tz_label, "normalized_time": dt.isoformat(),
                 "has_time": has_time, "source_type": f.source_type,
                 "evidence": f.evidence}
                for dt, tz_label, has_time, b, f in entries]},
            params={"cluster_mode": "same_day",
                    "near_window_seconds": window,
                    "window_rationale":
                        f"规范化日期同为 {date_label}，同日聚集（弱于相近窗口）"
                        + span_note,
                    "time_source": "document_internal_metadata"
                                   "（文档内元数据创建时间，非导入/复制时间）",
                    "generic_producer_list": list(GENERIC_PRODUCERS),
                    "degraded": True,
                    "degrade_reasons": ["同日聚集为弱信号，时间未必相近"]},
            trigger_reason=(
                f"{len(bidders)} 个投标主体的文件创建时间同日聚集"
                f"（规范化日期 {date_label}{span_note}），降为待核信号"),
            evidence_ids=sorted({f.evidence for _, _, _, _, f in entries}),
            limitations=list(_R006_CLUSTER_LIMITATIONS),
            alternatives=list(_R006_CLUSTER_ALTERNATIVES),
            signal="待核信号",
        ))
    return findings


# ---------------------------------------------------------------- R007

def screen_r007(fb: FactBase, thresholds: dict) -> list[dict]:
    """重复参投行为：只统计明确覆盖的不同采购事件与已知结果。

    口径：
      - 未中标按**不同采购事件去重**（同一事件多个标段均 lost 只计 1 个
        事件），不按标段行数累加。
      - 结果 unknown 不算未中标、不参与触发。
      - "高于已知中标价"只在**同 event + 同 lot**、同币种、同税口径
        内比较（跨标段、异币种、异税口径不比较），比较对象为最低可比价。
      - 证据缺失/无效的投标不参与统计（记入 excluded_facts）。
    """
    fb.validate_prices()
    th_lost = thresholds["R007"]["known_lost_min"]
    th_above = thresholds["R007"]["above_winning_min"]
    findings = []
    events = fb.by_event()
    per_bidder = fb.bidder_bids()
    for bidder_id in sorted(per_bidder):
        bids = per_bidder[bidder_id]
        usable_bids = []
        for b in bids:
            if fb.usable(b.evidence):
                usable_bids.append(b)
            else:
                fb.exclude("bid", b, b.evidence,
                           "evidence_missing_or_invalid" if b.evidence
                           else "evidence_absent")
        events_covered = sorted({b.event_id for b in usable_bids})
        known = [b for b in usable_bids if b.outcome_status == "known"
                 and b.outcome_result in ("won", "lost")]
        lost_events = sorted({b.event_id for b in known
                              if b.outcome_result == "lost"})
        unknown_n = len(bids) - len(known)
        above = []  # 高于已知中标价的明细（仅同 event + 同 lot；须双方价格证据）
        excluded_comparisons = []
        for b in usable_bids:
            rivals_won = [
                o for o in events.get(b.event_id, [])
                if o.lot_id == b.lot_id  # 价格比较仅限同标段
                and o.bidder_id != b.bidder_id and o.role == "bidder"
                and o.outcome_status == "known" and o.outcome_result == "won"]
            comparable_wins = []
            for o in rivals_won:
                reasons = []
                for candidate, label in ((b, "本方"), (o, "中标方")):
                    price = candidate.total_price
                    if not _finite_amount(price) or _decimal(price) <= 0:
                        reasons.append(f"{label}报价缺失、非正数或无效")
                    if not _known(candidate.currency):
                        reasons.append(f"{label}币种未知")
                    if type(candidate.tax_included) is not bool:
                        reasons.append(f"{label}税口径未知")
                    if not fb.usable(candidate.total_price_evidence):
                        reasons.append(f"{label}报价证据缺失/无效")
                if _known(b.currency) and _known(o.currency) \
                        and b.currency != o.currency:
                    reasons.append("币种不一致")
                if type(b.tax_included) is bool and type(o.tax_included) is bool \
                        and b.tax_included != o.tax_included:
                    reasons.append("税口径不一致")
                if reasons:
                    reason = "；".join(dict.fromkeys(reasons))
                    detail = {
                        "event_id": b.event_id, "lot_id": b.lot_id,
                        "bidder_id": b.bidder_id, "rival_bidder_id": o.bidder_id,
                        "bid_price": b.total_price, "bid_currency": b.currency,
                        "bid_tax_included": b.tax_included,
                        "rival_price": o.total_price,
                        "rival_currency": o.currency,
                        "rival_tax_included": o.tax_included,
                        "evidence_id": b.total_price_evidence,
                        "rival_price_evidence_id": o.total_price_evidence,
                        "reason": reason,
                    }
                    excluded_comparisons.append(detail)
                    fb.exclude("r007_price_comparison", b,
                               b.total_price_evidence,
                               f"与中标方 {o.bidder_id} 比价排除：{reason}")
                else:
                    comparable_wins.append(o)
            if comparable_wins and _decimal(b.total_price) > min(
                    _decimal(o.total_price) for o in comparable_wins):
                best = min(comparable_wins,
                           key=lambda o: _decimal(o.total_price))
                above.append({"event_id": b.event_id, "lot_id": b.lot_id,
                              "bid_price": decimal_text(_decimal(b.total_price)),
                              "min_won_price_same_lot":
                                  decimal_text(_decimal(best.total_price)),
                              "bid_price_evidence": b.total_price_evidence,
                              "won_price_evidence": best.total_price_evidence})
        if len(lost_events) < th_lost and len(above) < th_above:
            continue
        findings.append(make_finding(
            "R007",
            scope={"bidder_id": bidder_id},
            inputs={"events_covered": events_covered,
                    "n_events_covered": len(events_covered),
                    "lost_event_ids": lost_events,
                    "n_known_lost_events": len(lost_events),
                    "n_known_outcome_bids": len(known),
                    "n_unknown_outcome_bids": unknown_n,
                    "n_above_winning_price": len(above),
                    "above_detail": above,
                    "n_price_comparisons_excluded": len(excluded_comparisons),
                    "price_comparison_excluded": excluded_comparisons[:50],
                    "price_comparison_excluded_truncated":
                        len(excluded_comparisons) > 50,
                    "price_compare_note":
                        "仅比较同 event + 同 lot 内正数有限报价、已知且相同币种、"
                        "已知且相同税口径的最低已知中标价；双方价格证据均须可解析；"
                        "不可比项目记录排除原因，跨标段价格不比较；unknown 结果不计入"},
            params={"known_lost_min": th_lost, "above_winning_min": th_above,
                    "lost_counting": "按不同采购事件去重（同事件多标段只计 1）",
                    "unknown_policy": "结果 unknown 不算未中标、不参与触发"},
            trigger_reason=(f"已知未中标事件 {len(lost_events)} 个（阈值 {th_lost}）；"
                            f"同标段高于已知中标价 {len(above)} 次（阈值 {th_above}）"),
            evidence_ids=sorted(
                {b.evidence for b in known if b.evidence}
                | {e for a in above
                   for e in (a["bid_price_evidence"], a["won_price_evidence"])
                   if e}),
            limitations=["未覆盖本系统未导入的事件；未中标可因实力、报价策略等正常原因"],
            alternatives=["市场竞争常态化参与、专业领域只有少数潜在投标人"],
        ))
    return findings


# ---------------------------------------------------------------- R008

def screen_r008(fb: FactBase, thresholds: dict) -> list[dict]:
    """共同参投组合：只有同一 event + 同一 lot 确实同场投标的主体对才算
    共同参投，再按 distinct event 计数。

    同一事件不同标段各自单独投标的主体对不算共同参投；同事件的标段/
    版本/重复文件按事件去重；显式列出覆盖事件与已知胜负分布。
    证据缺失/无效的投标不参与（记入 excluded_facts）。
    """
    th = thresholds["R008"]["common_events_min"]
    pair_lots = defaultdict(lambda: defaultdict(set))
    # pair_lots[(a,b)][event_id] = {共同同场投标的 lot_id, ...}
    by_lot = defaultdict(list)
    for bid in fb.bids:
        if bid.role != "bidder":
            continue
        if not fb.usable(bid.evidence):
            fb.exclude("bid", bid, bid.evidence,
                       "evidence_missing_or_invalid" if bid.evidence
                       else "evidence_absent")
            continue
        by_lot[(bid.event_id, bid.lot_id)].append(bid)
    for (event_id, lot_id), bids in by_lot.items():
        bidders = sorted({b.bidder_id for b in bids})
        for i in range(len(bidders)):
            for j in range(i + 1, len(bidders)):
                pair_lots[(bidders[i], bidders[j])][event_id].add(lot_id)
    findings = []
    for (a, b), ev_lots in sorted(pair_lots.items()):
        evs = sorted(ev_lots)
        if len(evs) < th:
            continue
        # 胜负分布按共同 event + 共同 lot 计算，不受其他标段污染
        outcome_dist = Counter()
        for event_id in evs:
            for lot_id in sorted(ev_lots[event_id]):
                same_lot = [o for o in by_lot[(event_id, lot_id)]]
                da = {o.outcome_result for o in same_lot
                      if o.bidder_id == a and o.outcome_status == "known"}
                db = {o.outcome_result for o in same_lot
                      if o.bidder_id == b and o.outcome_status == "known"}
                outcome_dist[f"a={'won' if 'won' in da else 'lost' if 'lost' in da else 'unknown'}"
                             f"|b={'won' if 'won' in db else 'lost' if 'lost' in db else 'unknown'}"] += 1
        # 证据只引用用于证明同 event+lot 共投的 usable 投标 occurrence
        bid_ev = set()
        for event_id in evs:
            for lot_id in ev_lots[event_id]:
                for o in by_lot[(event_id, lot_id)]:
                    if o.bidder_id in (a, b) and o.evidence:
                        bid_ev.add(o.evidence)
        findings.append(make_finding(
            "R008",
            scope={"bidder_pair": [a, b]},
            inputs={"common_event_ids": evs,
                    "common_lots_by_event": {e: sorted(l) for e, l in ev_lots.items()},
                    "n_common_events": len(evs),
                    "outcome_distribution_known": dict(outcome_dist),
                    "dedup_rule": "同 event + 同 lot 真同场才计共同参投；"
                                  "按 distinct event 计数；同事件多标段/"
                                  "版本/重复文件只计 1 个事件"},
            params={"common_events_min": th,
                    "same_lot_required": True},
            trigger_reason=f"主体 {a} 与 {b} 在 {len(evs)} 个不同采购事件"
                           "于同一标段同场投标（阈值 "
                           f"{th}）",
            evidence_ids=sorted(bid_ev),
            limitations=["共同参投本身合法；须结合价格与人员等信号综合人工研判"],
            alternatives=["同领域只有少数合格投标人、联合体惯常搭配、地域市场集中"],
        ))
    return findings


# ---------------------------------------------------------------- 汇总

SCREENERS = [screen_r001, screen_r002, screen_r003, screen_r004,
             screen_r005, screen_r006, screen_r007, screen_r008]


def screen_all(fb: FactBase, thresholds: dict | None = None) -> dict:
    """执行 R001—R008，并说明逐规则的检查条件与覆盖状态。"""
    fb.validate_prices()
    # 每个规则的内层阈值 dict 必须复制：调用方经 setdefault().update() 覆盖时
    # 不得就地污染 DEFAULT_THRESHOLDS（否则测试与调用顺序互相影响）
    th = {k: dict(v) for k, v in DEFAULT_THRESHOLDS.items()}
    if thresholds:
        for k, v in thresholds.items():
            th.setdefault(k, {}).update(v)
    findings = []
    r004_stats = None
    for fn in SCREENERS:
        out = fn(fb, th)
        if fn is screen_r004:
            findings_4, r004_stats = out
            findings.extend(findings_4)
        else:
            findings.extend(out)
    by_rule = Counter(f["rule_id"] for f in findings)
    by_signal = Counter(f["signal"] for f in findings)
    by_bidder = defaultdict(set)
    for bid in fb.bids:
        if bid.role == "bidder" and fb.usable(bid.evidence):
            by_bidder[bid.bidder_id].add(bid.event_id)
    pair_events = Counter()
    by_event_bidders = defaultdict(set)
    for bid in fb.bids:
        if bid.role == "bidder" and fb.usable(bid.evidence):
            by_event_bidders[bid.event_id].add(bid.bidder_id)
    for event_bidders in by_event_bidders.values():
        bidders = sorted(event_bidders)
        for i, first in enumerate(bidders):
            for second in bidders[i + 1:]:
                pair_events[(first, second)] += 1
    eligible = {
        "R001": sum(1 for bid in fb.bids for file in bid.files
                    if file.context not in R001_EXCLUDED_CONTEXTS
                    and fb.usable(file.evidence)
                    and (file.declared_owner_id is not None
                         or file.declared_uscc is not None)),
        "R002": sum(1 for bid in fb.bids for c in bid.contacts
                    if bid.role == "bidder" and c.source_role == "bidder"
                    and _known(c.value) and fb.usable(c.evidence)),
        "R003": sum(1 for bid in fb.bids for person in bid.persons
                    if person.person_id and not str(person.person_id).startswith("unverified:")
                    and fb.usable(person.evidence)),
        "R004": (r004_stats or {}).get("comparable_rows_matched", 0)
                + (r004_stats or {}).get("comparable_rows_unmatched_no_pattern", 0),
        "R005": sum(1 for bid in fb.bids if _finite_amount(bid.total_price)
                    and _decimal(bid.total_price) > 0
                    and _known(bid.currency) and type(bid.tax_included) is bool
                    and fb.usable(bid.total_price_evidence)),
        "R006": sum(1 for bid in fb.bids for file in bid.files
                    if fb.usable(file.evidence)
                    and any(_known(file.metadata.get(key))
                            for key in ("producer", "creation_date"))),
        "R007": sum(1 for bid in fb.bids if bid.outcome_status == "known"
                    and bid.outcome_result in ("won", "lost")
                    and fb.usable(bid.evidence)),
        "R008": max(pair_events.values(), default=0),
    }
    names = {
        "R001": "文件主体归属", "R002": "联系方式交叉",
        "R003": "人员交叉", "R004": "清单报价规律",
        "R005": "总报价集中", "R006": "文件属性相似",
        "R007": "重复参投行为", "R008": "共同参投组合",
    }
    rule_statuses = {}
    for rule_id, name in names.items():
        rule_findings = [f for f in findings if f["rule_id"] == rule_id]
        positive = [f for f in rule_findings if f["signal"] != "不计算"]
        enough = eligible[rule_id] > 0
        reason = ""
        if rule_id == "R004":
            enough = eligible[rule_id] > 0
            reason = ("可比清单行不足 3 家或关键字段/证据不齐"
                      if not enough else "已筛查具备可比条件的清单行")
        elif rule_id == "R005":
            enough = eligible[rule_id] >= th["R005"]["min_positive_bidders"]
            reason = ("同标段已知币种、税口径和有效证据的正报价不足"
                      if not enough else "已按标段、币种和税口径分组筛查")
        elif rule_id == "R008":
            enough = eligible[rule_id] >= th["R008"]["common_events_min"]
            reason = ("可回指的共同参投事件未达到规则阈值"
                      if not enough else "已检查达到阈值的共同参投组合")
        elif rule_id == "R002":
            enough = eligible[rule_id] > 0 and len({
                bid.bidder_id for bid in fb.bids
                if any(c.source_role == "bidder" and _known(c.value)
                       and fb.usable(c.evidence) for c in bid.contacts)}) >= 2
            reason = "需两个以上主体的已确认投标人联系方式" if not enough else ""
        elif rule_id == "R003":
            enough = eligible[rule_id] > 0 and len({
                bid.bidder_id for bid in fb.bids
                if any(p.person_id and not str(p.person_id).startswith("unverified:")
                       and fb.usable(p.evidence) for p in bid.persons)}) >= 2
            reason = "需跨两个以上主体的显式人员身份" if not enough else ""
        elif rule_id == "R007":
            enough = eligible[rule_id] > 0
            reason = "缺少带有效证据的中标/未中标确认" if not enough else ""
        elif not enough:
            reason = "缺少可回指且已确认的规则输入"
        rule_statuses[rule_id] = {
            "name": name,
            "status": ("findings" if positive else
                       "checked_no_finding" if enough else "insufficient_data"),
            "input_count": eligible[rule_id],
            "finding_count": len(positive),
            "not_calculated_count": len(rule_findings) - len(positive),
            "reason": reason or ("发现待人工复核线索" if positive else
                                 "已检查，未发现该规则线索"),
        }
    return {
        "findings": findings,
        "r004_stats": r004_stats or {},
        "rule_statuses": rule_statuses,
        "run_status": "completed",
        "excluded_facts": list(fb.excluded_facts),
        "summary": {
            "total": len(findings),
            "by_rule": dict(sorted(by_rule.items())),
            "by_signal": dict(sorted(by_signal.items())),
        },
        "unresolved_evidence": sorted(set(fb.unresolved_evidence)),
        "disclaimer": ("findings 为人工核查线索，signal 仅表示核查优先级提示，"
                       "不是串标概率或违法结论；本引擎仅对已人工归一化的事实执行"
                       "筛查，未实现任意原始投标文件的全自动字段抽取。"),
    }

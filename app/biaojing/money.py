# -*- coding: utf-8 -*-
"""金额解析与归一化：不猜测缺失单位，统一以 Decimal 元值保存。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata

_NUMBER = re.compile(
    r"(?<![\d.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(万元|元|万)?")
_FOREIGN_CODES = re.compile(r"(?<![A-Z])(USD|EUR|GBP|JPY|AUD|CAD|HKD|SGD)(?![A-Z])")
_UNIT_ALIASES = {
    "元": "yuan", "yuan": "yuan", "cny_yuan": "yuan",
    "万元": "ten_thousand_yuan", "万": "ten_thousand_yuan",
    "ten_thousand_yuan": "ten_thousand_yuan", "wan_yuan": "ten_thousand_yuan",
    "unknown": "unknown", "": "unknown", None: "unknown",
}


def decimal_text(value: Decimal) -> str:
    """返回不带指数、不过度补零的精确十进制文本。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_amount(raw, unit_hint=None) -> dict:
    """解析唯一金额；多金额、缺单位、单位冲突均不生成标准金额。"""
    source = str(raw)
    normalized = unicodedata.normalize("NFKC", source).strip()
    matches = list(_NUMBER.finditer(normalized))
    upper = normalized.upper()
    has_cny = any(mark in upper for mark in ("人民币", "CNY", "¥", "￥"))
    foreign = set(_FOREIGN_CODES.findall(upper))
    if "美元" in normalized or "美金" in normalized or "$" in normalized:
        foreign.add("USD")
    if "欧元" in normalized or "€" in normalized:
        foreign.add("EUR")
    if "英镑" in normalized or "£" in normalized:
        foreign.add("GBP")
    if "日元" in normalized:
        foreign.add("JPY")
    if "港币" in normalized or "港元" in normalized:
        foreign.add("HKD")
    currency = ("conflict" if (has_cny and foreign) or len(foreign) > 1 else
                "CNY" if has_cny else next(iter(foreign), "unknown"))
    if len(matches) != 1:
        return {"raw": source, "number": None, "unit": "unknown",
                "currency": currency, "amount_yuan": None,
                "status": "ambiguous" if matches else "unparsed"}

    if currency == "conflict":
        return {"raw": source, "number": None, "unit": "unknown",
                "currency": currency, "amount_yuan": None,
                "status": "currency_conflict"}

    match = matches[0]
    number = match.group(1).replace(",", "")
    explicit_unit = _UNIT_ALIASES[match.group(2)] if match.group(2) else None
    hinted_unit = _UNIT_ALIASES.get(unit_hint, "unknown")
    if explicit_unit and hinted_unit not in ("unknown", explicit_unit):
        return {"raw": source, "number": number, "unit": explicit_unit,
                "currency": currency, "amount_yuan": None,
                "status": "unit_conflict"}
    unit = explicit_unit or hinted_unit
    if unit == "unknown":
        return {"raw": source, "number": number, "unit": unit,
                "currency": currency, "amount_yuan": None,
                "status": "needs_unit"}
    try:
        amount = Decimal(number)
        if not amount.is_finite():
            raise InvalidOperation
        if unit == "ten_thousand_yuan":
            amount *= Decimal("10000")
    except (InvalidOperation, ValueError):
        return {"raw": source, "number": number, "unit": unit,
                "currency": currency, "amount_yuan": None,
                "status": "unparsed"}
    return {"raw": source, "number": number, "unit": unit,
            "currency": currency, "amount_yuan": decimal_text(amount),
            "status": "normalized"}


def normalize_confirmed_amount(raw, unit) -> str:
    """人工确认单位后，以精确十进制字符串返回人民币元金额。"""
    parsed = parse_amount(raw, unit)
    if parsed["currency"] == "conflict":
        raise ValueError("金额币种标记冲突，请核实原件后重新确认")
    if parsed["currency"] not in ("CNY", "unknown"):
        raise ValueError("检测到外币；当前版本不做汇率换算，请核实后标为 unknown")
    if parsed["status"] != "normalized":
        raise ValueError("金额无法唯一归一化；请核对数值和单位")
    return parsed["amount_yuan"]

# -*- coding: utf-8 -*-
"""输出记录构造与 JSON 安全化。

file 级记录字段契约（P1）：
  file            输入引用（文件系统绝对路径，或 zip 内条目 `zip路径::条目名`）
  origin          来源 {"kind": "filesystem"|"zip_entry", "container"?, "entry"?}
  sha256          原始字节 SHA-256
  size_bytes      原始字节长度
  source_type     来源类型，默认 unknown（不由程序推断）
  doc_type        pdf/docx/xlsx/doc_legacy/xls_legacy/ole_legacy/zip/unknown
  parser          解析库及版本（无法解析时为 null）
  extract_method  提取方式描述（与 parser 分列）
  extract_status  success/partial/failed/pending_ocr/pending_convert/duplicate/unknown
  evidence        原文证据列表，每条带精确定位 locator/kind
  counts          覆盖计数（分子分母，见各解析器）
  metadata        文件核心属性（未解析时为 null）
  notes           口径与异常说明
  error           仅 failed：失败原因
  duplicate_of    仅 duplicate：批次内首个相同字节引用
"""

from __future__ import annotations

import datetime
import math


def json_safe(value):
    """把 openpyxl 等库返回的对象转成严格 JSON 标量。

    NaN/Infinity 不是合法 JSON，转成字符串显式暴露，绝不静默变 0 或 null。
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    return str(value)


def make_file_record(file_ref: str, origin: dict, sha256: str, size: int,
                     source_type: str, doc_type: str, note: str = "") -> dict:
    rec = {
        "file": file_ref,
        "origin": origin,
        "sha256": sha256,
        "size_bytes": size,
        "source_type": source_type,
        "doc_type": doc_type,
        "parser": None,
        "extract_method": None,
        "extract_status": "unknown",
        "evidence": [],
        "counts": {},
        "metadata": None,
        "notes": [],
    }
    if note:
        rec["notes"].append(note)
    return rec


def fail_record(rec: dict, error: str) -> dict:
    rec["extract_status"] = "failed"
    rec["error"] = error
    return rec

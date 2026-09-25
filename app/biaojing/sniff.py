# -*- coding: utf-8 -*-
"""文件类型判定：扩展名优先，magic 兜底；不猜测业务语义。

doc_type 取值：pdf / docx / xlsx / doc_legacy / xls_legacy / ole_legacy /
zip / unknown。
"""

from __future__ import annotations

import os

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# OOXML 容器内用于区分 docx/xlsx 的标志性成员
_DOCX_MARKERS = ("word/document.xml",)
_XLSX_MARKERS = ("xl/workbook.xml",)


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _looks_ooxml_zip(names: set[str]) -> str | None:
    if any(n in names for n in _DOCX_MARKERS):
        return "docx"
    if any(n in names for n in _XLSX_MARKERS):
        return "xlsx"
    return None


def classify(name: str, head: bytes, full_data: bytes | None = None) -> tuple[str, str]:
    """返回 (doc_type, note)。

    head：文件前至少 8 字节；full_data 仅在扩展名未知且容器为 zip 时用于
    进一步区分 docx/xlsx，可为 None。
    """
    ext = _ext(name)
    if ext == ".pdf":
        return "pdf", ""
    if ext == ".docx":
        return "docx", ""
    if ext in (".xlsx", ".xlsm"):
        return "xlsx", ""
    if ext == ".doc":
        return "doc_legacy", "旧版 Word 二进制格式，须先本地转换为 docx"
    if ext == ".xls":
        return "xls_legacy", "旧版 Excel 二进制格式，须先本地转换为 xlsx"
    if ext == ".zip":
        return "zip", ""

    # 扩展名未知或缺失：按 magic 兜底，note 记录扩展不匹配事实
    if head[: len(_PDF_MAGIC)] == _PDF_MAGIC:
        return "pdf", f"扩展名 {ext!r} 非 .pdf，但字节头为 PDF，按 magic 判定"
    if head[: len(_ZIP_MAGIC)] == _ZIP_MAGIC:
        inner = _ooxml_member_names(full_data)
        as_ooxml = _looks_ooxml_zip(inner) if inner is not None else None
        if as_ooxml:
            return as_ooxml, f"扩展名 {ext!r} 缺失或不识别，字节头为 zip 且含 OOXML 成员，按内容判定"
        return "zip", f"扩展名 {ext!r} 缺失或不识别，字节头为 zip，按普通 zip 容器处理"
    if head[: len(_OLE_MAGIC)] == _OLE_MAGIC:
        return "ole_legacy", "OLE2 容器（无法确认是 doc 还是 xls 或其他），按待转换处理"
    return "unknown", f"扩展名 {ext!r} 不识别且字节头无已知特征"


def _ooxml_member_names(data: bytes) -> set[str] | None:
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return set(zf.namelist())
    except Exception:
        return None

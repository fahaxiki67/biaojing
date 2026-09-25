# -*- coding: utf-8 -*-
"""XLSX/XLSM 解析：openpyxl 双次加载，遍历全部工作表（含隐藏）。

口径：
  双次加载——data_only=False 取公式原文、数字格式、批注；data_only=True 取
  公式缓存值。公式单元格缓存值读不到（None）时记 "unknown"，绝不填 0 或空串。
  工作表含 hidden / veryHidden，state 字段如实标注。
  定位 locator 为 `Sheet名!A1`（Excel 同款 1 起坐标）。
  全空单元格（无值无批注）不产出证据；cells_total 只含已产出单元格。
  数字格式一律保留（含 "General"），批注保留原文。

遍历安全边界（防稀疏超大声明表耗时/内存失控）：
  iter_rows 会按工作表维度生成坐标组合，稀疏表可虚大到十万行、单行可到
  16,384 格（XFD）。故采用逐行窗口式扫描：每行单独调用
  iter_rows(min_row=r, max_row=r, max_col=min(行宽, 剩余单元格配额))，
  openpyxl 每行至多生成剩余配额个 Cell；扫描行数与单元格总数两级硬上限，
  计数含空格。触顶时 extract_status 降为 partial，counts 记录已扫描行/格数
  与 scan_truncated 标记，绝不静默漏项当作全量完成。

文件级状态：有任一证据且未触顶 -> success；触顶（扫描触顶或证据输出触顶）
-> partial；工作簿完全为空 -> failed（如实暴露）。
公式缓存缺失通过 counts.formulas_cached_unknown 显式暴露，不降低 status
（那是文件自身内容缺失，不是本引擎抽取缺口）。
"""

from __future__ import annotations

import io
import posixpath
import xml.etree.ElementTree as ET
import zipfile

import openpyxl

from .model import json_safe

PARSER_NAME = f"openpyxl {openpyxl.__version__}"
EXTRACT_METHOD = (
    "openpyxl 双只读流式加载（公式视图 + data_only 缓存值视图），遍历全部工作表"
    "（含 hidden/veryHidden），逐单元格抽取原值/公式/缓存值/数字格式/批注；"
    "扫描行数与扫描单元格总数设硬上限，触顶降为 partial 并记录已扫描量"
)

DEFAULT_MAX_SCAN_ROWS = 50_000
DEFAULT_MAX_SCAN_CELLS = 2_000_000
EXTRACTION_VERSION = (
    f"{PARSER_NAME}; read-only streaming; formula/cache paired; comments via OOXML; "
    f"rows={DEFAULT_MAX_SCAN_ROWS}; cells={DEFAULT_MAX_SCAN_CELLS}; v1"
)


class _HeldBytesIO(io.BytesIO):
    """close 为空操作的 BytesIO：流的生命周期由 parse() 的局部变量持有。

    openpyxl 非只读模式不关闭其内部 ZipFile，靠 GC 兜底；若底层 BytesIO 先
    于该 ZipFile 被回收，Python 3.14 会在 stderr 打印 "Exception ignored ...
    ValueError: I/O operation on closed file"。BytesIO 不持有系统资源，跳过
    close 无泄漏，使兜底 close 恒可成功，从根上消除噪音与回收顺序竞态。
    """

    def close(self):
        pass


def _comments_by_sheet(data: bytes) -> tuple[dict, list[str]]:
    """只读解压 OOXML 批注部件，避免为批注把整本工作簿载入内存。"""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    comments, notes = {}, []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            workbook = ET.fromstring(package.read("xl/workbook.xml"))
            relationships = ET.fromstring(
                package.read("xl/_rels/workbook.xml.rels"))
            targets = {r.attrib["Id"]: r.attrib["Target"]
                       for r in relationships.findall(f"{{{pkg_ns}}}Relationship")}
            names = set(package.namelist())
            for sheet in workbook.findall(f"{{{ns}}}sheets/{{{ns}}}sheet"):
                sheet_name = sheet.attrib.get("name", "")
                target = targets.get(sheet.attrib.get(f"{{{rel_ns}}}id"))
                if not target:
                    continue
                sheet_path = (posixpath.normpath(target.lstrip("/"))
                              if target.startswith("/") else
                              posixpath.normpath(posixpath.join("xl", target)))
                rel_path = posixpath.join(posixpath.dirname(sheet_path), "_rels",
                                          posixpath.basename(sheet_path) + ".rels")
                if rel_path not in names:
                    continue
                rels = ET.fromstring(package.read(rel_path))
                for rel in rels.findall(f"{{{pkg_ns}}}Relationship"):
                    if not rel.attrib.get("Type", "").endswith("/comments"):
                        continue
                    comment_target = rel.attrib.get("Target", "")
                    comment_path = (posixpath.normpath(comment_target.lstrip("/"))
                                    if comment_target.startswith("/") else
                                    posixpath.normpath(posixpath.join(
                                        posixpath.dirname(sheet_path),
                                        comment_target)))
                    info = package.getinfo(comment_path)
                    if info.file_size > 16 * 1024 * 1024:
                        notes.append(f"工作表 [{sheet_name}] 批注部件超过 16MB，批注未读取")
                        continue
                    root = ET.fromstring(package.read(comment_path))
                    sheet_comments = comments.setdefault(sheet_name, {})
                    for comment in root.findall(f".//{{{ns}}}comment"):
                        text_node = comment.find(f"{{{ns}}}text")
                        value = "" if text_node is None else "".join(
                            node.text or "" for node in text_node.iter()
                            if node.tag.rsplit("}", 1)[-1] == "t")
                        sheet_comments[comment.attrib.get("ref", "")] = value
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        notes.append(f"Excel 批注读取不完整：{type(exc).__name__}: {exc}")
    return comments, notes


def parse(data: bytes, max_cells: int = 20_000,
          max_scan_rows: int = DEFAULT_MAX_SCAN_ROWS,
          max_scan_cells: int = DEFAULT_MAX_SCAN_CELLS) -> dict:
    comments, comment_notes = _comments_by_sheet(data)
    notes = list(comment_notes)
    evidence_truncated = False
    scan_truncated = False
    wb_f = openpyxl.load_workbook(
        _HeldBytesIO(data), data_only=False, read_only=True, keep_vba=False
    )
    try:
        wb_v = openpyxl.load_workbook(
            _HeldBytesIO(data), data_only=True, read_only=True, keep_vba=False
        )
    except Exception as exc:
        wb_f.close()
        return {
            "ok": False,
            "status": "failed",
            "error": f"data_only 视图加载失败：{type(exc).__name__}: {exc}",
            "evidence": [], "counts": {}, "metadata": None, "notes": [],
        }
    try:
        evidence = []
        sheets_total = sheets_hidden = 0
        cells_total = formulas_total = cached_unknown = 0
        scanned_rows_total = scanned_cells_total = 0
        comments_incomplete = bool(comment_notes)

        sheet_views = wb_v.worksheets
        for idx, ws in enumerate(wb_f.worksheets):
            if scan_truncated:
                notes.append(
                    f"工作表 [{ws.title}] 因前表触顶未扫描（本底座不做静默漏项）")
                continue
            sheets_total += 1
            state = getattr(ws, "sheet_state", "visible")
            if state in ("hidden", "veryHidden"):
                sheets_hidden += 1
            declared = f"{ws.max_row or 0}x{ws.max_column or 0}"
            evidence.append({
                "kind": "xlsx_sheet_info",
                "locator": f"[{ws.title}]",
                "sheet": ws.title,
                "sheet_state": state,
                "sheet_type": str(getattr(ws, "sheet_type", "worksheet")),
                "dimension_rows_x_cols": declared,
            })
            ws_v = sheet_views[idx] if idx < len(sheet_views) else None
            sheet_max_col = ws.max_column or 1
            sheet_max_row = ws.max_row or 1
            stream_width = min(sheet_max_col, max_scan_cells)
            formula_rows = ws.iter_rows(max_col=stream_width)
            value_rows = (ws_v.iter_rows(max_col=stream_width)
                          if ws_v is not None else None)
            row_idx = 1
            while row_idx <= sheet_max_row:
                remaining = max_scan_cells - scanned_cells_total
                if remaining <= 0:
                    scan_truncated = True
                    notes.append(
                        f"工作表 [{ws.title}] 扫描触顶（硬上限："
                        f"{max_scan_rows} 行 / {max_scan_cells} 单元格，计数含空格），"
                        "该表及其后未扫部分如实降为 partial")
                    break
                if scanned_rows_total >= max_scan_rows:
                    scan_truncated = True
                    notes.append(
                        f"工作表 [{ws.title}] 扫描触顶（硬上限："
                        f"{max_scan_rows} 行 / {max_scan_cells} 单元格，计数含空格），"
                        "该表及其后未扫部分如实降为 partial")
                    break
                row = next(formula_rows, None)
                if row is None:
                    break
                value_row = next(value_rows, None) if value_rows is not None else None
                scanned_rows_total += 1
                row_idx += 1
                take = min(len(row), remaining)
                row_cut_short = take < sheet_max_col
                if row_cut_short:
                    scan_truncated = True
                    notes.append(
                        f"工作表 [{ws.title}] 扫描触顶（硬上限："
                        f"{max_scan_rows} 行 / {max_scan_cells} 单元格，计数含空格），"
                        "该表及其后未扫部分如实降为 partial")
                for column, cell in enumerate(row[:take]):
                    scanned_cells_total += 1
                    value = cell.value
                    coordinate = (
                        f"{openpyxl.utils.get_column_letter(column + 1)}{row_idx - 1}")
                    comment = comments.get(ws.title, {}).get(coordinate)
                    if value is None and comment is None:
                        continue
                    if len(evidence) >= max_cells:
                        evidence_truncated = True
                        break
                    is_formula = isinstance(value, str) and value.startswith("=")
                    cached = None
                    if is_formula:
                        formulas_total += 1
                        cv = (value_row[column].value
                              if value_row is not None and column < len(value_row)
                              else None)
                        if cv is None:
                            cached = "unknown"
                            cached_unknown += 1
                        else:
                            cached = json_safe(cv)
                    rec = {
                        "kind": "xlsx_cell",
                        "locator": f"{ws.title}!{coordinate}",
                        "sheet": ws.title,
                        "cell": coordinate,
                        "value": json_safe(value),
                        "number_format": cell.number_format,
                    }
                    if is_formula:
                        rec["formula"] = value
                        rec["cached_value"] = cached
                    if comment is not None:
                        rec["comment"] = comment
                    evidence.append(rec)
                    cells_total += 1
                if evidence_truncated:
                    notes.append(
                        f"单元格证据达到单文件输出上限 {max_cells} 条，"
                        "其余单元格已扫描但未输出证据（counts 反映已输出部分）")
                    break
                if row_cut_short:
                    break

        counts = {
            "sheets_total": sheets_total,
            "sheets_hidden": sheets_hidden,
            "scanned_rows_total": scanned_rows_total,
            "scanned_cells_total": scanned_cells_total,
            "scan_truncated": scan_truncated,
            "evidence_truncated": evidence_truncated,
            "comments_incomplete": comments_incomplete,
            "cells_total": cells_total,
            "formulas_total": formulas_total,
            "formulas_cached_unknown": cached_unknown,
        }

        if scan_truncated or evidence_truncated or comments_incomplete:
            status = "partial"
        elif cells_total == 0:
            return {
                "ok": False,
                "status": "failed",
                "error": "工作簿所有工作表均无单元格值与批注（空表）",
                "evidence": evidence,
                "counts": counts,
                "metadata": _meta(wb_f),
                "notes": notes,
            }
        else:
            status = "success"

        return {
            "ok": True,
            "status": status,
            "error": None,
            "evidence": evidence,
            "counts": counts,
            "metadata": _meta(wb_f),
            "notes": notes,
        }
    finally:
        wb_f.close()
        wb_v.close()


def _meta(wb) -> dict:
    core = wb.properties
    metadata = {
        "creator": core.creator or "",
        "last_modified_by": core.lastModifiedBy or "",
        "created": core.created.isoformat() if core.created else "",
        "modified": core.modified.isoformat() if core.modified else "",
    }
    return {k: v for k, v in metadata.items() if v != ""}

# -*- coding: utf-8 -*-
"""标镜 CLI：多路径/文件夹/ZIP 批量导入 -> 机器可读 JSON。

用法：
    python -m biaojing <路径...> [-o 输出.json] [选项]

批次纪律：每个文件独立 try/except，单文件失败不影响整批；重复字节文件
标记 duplicate 并回指批次内首个引用；来源类型一律不由程序推断。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

from . import PRODUCT_NAME, VERSION
from . import hashing, sniff
from .model import fail_record, make_file_record
from . import pdf_parser, docx_parser, xlsx_parser
from . import zip_container
from .walker import iter_inputs

DEFAULT_MAX_ZIP_ENTRIES = 1000
DEFAULT_MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EVIDENCE = 20000
DEFAULT_MAX_SCAN_ROWS = xlsx_parser.DEFAULT_MAX_SCAN_ROWS
DEFAULT_MAX_SCAN_CELLS = xlsx_parser.DEFAULT_MAX_SCAN_CELLS


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PRODUCT_NAME,
        description=f"{PRODUCT_NAME}（python -m biaojing）P1 解析底座："
                    "本地文件结构抽取与证据定位，输出机器可读 JSON"
                    "（扫描 PDF 可使用本机 Tesseract 离线 OCR；"
                    "不做规则筛查、不联网）。",
    )
    ap.add_argument("paths", nargs="+", help="输入路径：文件、目录或 .zip（可多个）")
    ap.add_argument("-o", "--output", default=None,
                    help="JSON 输出文件路径（缺省打印到 stdout）")
    ap.add_argument("--source-type", default="unknown",
                    help="来源类型标注（默认 unknown；程序不做推断，"
                         "由调用方对整批显式指定）")
    ap.add_argument("--max-zip-entries", type=int, default=DEFAULT_MAX_ZIP_ENTRIES,
                    help=f"zip 内文件数上限（默认 {DEFAULT_MAX_ZIP_ENTRIES}）")
    ap.add_argument("--max-zip-total-bytes", type=int,
                    default=DEFAULT_MAX_ZIP_TOTAL_BYTES,
                    help="zip 声明解压总量上限，字节（默认 512MB）")
    ap.add_argument("--max-evidence", type=int, default=DEFAULT_MAX_EVIDENCE,
                    help=f"xlsx 单文件单元格证据上限（默认 {DEFAULT_MAX_EVIDENCE}）")
    ap.add_argument("--max-scan-rows", type=int, default=DEFAULT_MAX_SCAN_ROWS,
                    help=f"xlsx 单文件扫描行数硬上限（默认 {DEFAULT_MAX_SCAN_ROWS}）")
    ap.add_argument("--max-scan-cells", type=int, default=DEFAULT_MAX_SCAN_CELLS,
                    help=f"xlsx 单文件扫描单元格总数硬上限（默认 {DEFAULT_MAX_SCAN_CELLS}）")
    ap.add_argument("--compact", action="store_true", help="紧凑 JSON（不缩进）")
    return ap


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limits = {
        "max_zip_entries": args.max_zip_entries,
        "max_zip_total_bytes": args.max_zip_total_bytes,
        "max_evidence_per_file": args.max_evidence,
        "max_scan_rows": args.max_scan_rows,
        "max_scan_cells": args.max_scan_cells,
    }
    records: list[dict] = []
    seen_sha: dict[str, str] = {}
    inputs_listed: list[str] = []

    for display, origin, data, error in iter_inputs(
        args.paths, args.max_zip_entries, args.max_zip_total_bytes
    ):
        inputs_listed.append(display)
        rec = _process_one(display, origin, data, error,
                           args.source_type, args.max_evidence,
                           args.max_scan_rows, args.max_scan_cells,
                           args.max_zip_entries, args.max_zip_total_bytes,
                           seen_sha)
        records.append(rec)

    summary = {
        "total_inputs": len(records),
        "by_status": {},
        "by_doc_type": {},
    }
    for rec in records:
        summary["by_status"][rec["extract_status"]] = \
            summary["by_status"].get(rec["extract_status"], 0) + 1
        summary["by_doc_type"][rec["doc_type"]] = \
            summary["by_doc_type"].get(rec["doc_type"], 0) + 1

    payload = {
        "product": PRODUCT_NAME,
        "product_name": PRODUCT_NAME,
        "module": "biaojing",
        "version": VERSION,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_type_policy": "程序不推断来源类型；除 --source-type 显式指定外一律 unknown",
        "inputs": inputs_listed,
        "config": limits,
        "summary": summary,
        "files": records,
    }
    text = json.dumps(payload, ensure_ascii=False,
                      indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"已写出：{args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def _process_one(display: str, origin: dict, data: bytes | None,
                 error: str | None, source_type: str, max_evidence: int,
                 max_scan_rows: int, max_scan_cells: int,
                 max_zip_entries: int, max_zip_total_bytes: int,
                 seen_sha: dict[str, str], progress=None,
                 cancelled=None) -> dict:
    origin = dict(origin)
    origin.setdefault("kind", "filesystem")
    try:
        if data is None:
            doc_type0, note0 = sniff.classify(
                origin.get("entry") or origin.get("path") or display,
                b"", None)
            rec = make_file_record(display, origin, sha256="", size=0,
                                   source_type=source_type, doc_type=doc_type0,
                                   note=note0)
            return fail_record(rec, error or "未知错误")

        sha = hashing.sha256_bytes(data)
        head = data[:8]
        doc_type, sniff_note = sniff.classify(
            origin.get("entry") or origin.get("path") or display,
            head, data,
        )
        rec = make_file_record(display, origin, sha256=sha, size=len(data),
                               source_type=source_type, doc_type=doc_type,
                               note=sniff_note)

        if sha in seen_sha:
            rec["extract_status"] = "duplicate"
            rec["duplicate_of"] = seen_sha[sha]
            rec["notes"].append(
                "与批次内先前文件字节完全相同；解析结果复用所指记录，"
                "本条保留自身引用与来源。字节相同不等于共同编制。")
            return rec
        seen_sha[sha] = display

        if origin.get("kind") == "zip_entry_rejected":
            return fail_record(rec, error or "zip 条目被拒收")

        if error:
            return fail_record(rec, error)

        if doc_type == "zip":
            if origin.get("kind") == "zip_entry":
                return fail_record(rec, "嵌套 zip 不展开（P1 范围限制）")
            return fail_record(rec, "普通 zip 容器未被展开（异常路径）")
        if doc_type in ("pdf",):
            parsed = (pdf_parser.parse(data) if progress is None and cancelled is None
                      else pdf_parser.parse(data, progress=progress,
                                            cancelled=cancelled))
            return _apply(rec, parsed,
                          pdf_parser.PARSER_NAME, pdf_parser.EXTRACT_METHOD)
        if doc_type in ("docx", "xlsx"):
            # docx/xlsx 本身是 zip 容器：成员数与声明解压总量先过限额护栏，
            # 拒绝时不交给解析库解压，全程不落盘
            reason = zip_container.guard_ooxml(
                data, max_zip_entries, max_zip_total_bytes)
            if reason:
                return fail_record(rec, reason)
            if doc_type == "docx":
                return _apply(rec, docx_parser.parse(
                    data, progress=progress, cancelled=cancelled),
                              docx_parser.PARSER_NAME, docx_parser.EXTRACT_METHOD)
            return _apply(rec,
                          xlsx_parser.parse(data, max_cells=max_evidence,
                                            max_scan_rows=max_scan_rows,
                                            max_scan_cells=max_scan_cells),
                          xlsx_parser.PARSER_NAME, xlsx_parser.EXTRACT_METHOD)
        if doc_type in ("doc_legacy", "xls_legacy", "ole_legacy"):
            rec["extract_status"] = "pending_convert"
            rec["notes"].append("旧版二进制格式，本底座不伪装支持；待本地转换为 docx/xlsx 后重跑")
            return rec
        rec["extract_status"] = "unknown"
        rec["notes"].append("无法识别的来源类型，未解析")
        return rec
    except Exception as exc:  # 单文件隔离：任何异常不拖垮批次
        rec = locals().get("rec")
        if isinstance(rec, dict):
            return fail_record(rec, f"未预期异常：{type(exc).__name__}: {exc}")
        fallback = make_file_record(display, origin, sha256="", size=0,
                                    source_type=source_type, doc_type="unknown")
        return fail_record(fallback, f"未预期异常：{type(exc).__name__}: {exc}")


def _apply(rec: dict, result: dict, parser_name: str, method: str) -> dict:
    rec["parser"] = parser_name
    rec["extract_method"] = method
    rec["extract_status"] = result["status"]
    rec["evidence"] = result["evidence"]
    rec["counts"] = result["counts"]
    rec["metadata"] = result["metadata"]
    rec["notes"].extend(result["notes"])
    rec["cancelled"] = bool(result.get("cancelled", False))
    if not result["ok"]:
        rec["error"] = result["error"]
    return rec


if __name__ == "__main__":
    sys.exit(run())

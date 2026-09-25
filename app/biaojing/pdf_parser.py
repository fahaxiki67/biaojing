# -*- coding: utf-8 -*-
"""PDF 解析：原生文本优先，扫描页使用本机 Tesseract 离线 OCR。

页级判定口径：
  text         该页抽到非空白文本
  ocr          该页文本由本机 OCR 识别，必须对照原件人工复核
  pending_ocr  该页无文本但有图像，OCR 不可用、失败或触及资源限制
  blank        该页既无文本也无图像（OCR 也无从下手，如实记录）

文件级映射：
  全部页 text/ocr                -> success
  有 text/ocr 且含 pending_ocr/blank -> partial
  全部页 pending_ocr             -> pending_ocr
  全部页 blank 或 0 页           -> failed（无内容可抽取，如实失败而非静默通过）
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import time

import pymupdf

from .model import json_safe

PARSER_NAME = f"pymupdf {pymupdf.__version__ if hasattr(pymupdf, '__version__') else '?'}"
EXTRACT_METHOD = "pymupdf 逐页抽取；无文本扫描页尝试本机 Tesseract 离线 OCR；保留物理页序与页标签"

OCR_DPI = 200
OCR_MAX_PIXELS = 25_000_000
OCR_MAX_IMAGE_BYTES = 25_000_000
OCR_PAGE_TIMEOUT = 15
OCR_SPARSE_MIN_CHARS = 20
EXTRACTION_VERSION = (f"{PARSER_NAME}; OCR dpi={OCR_DPI}, psm=3+11, "
                      f"min={OCR_SPARSE_MIN_CHARS}, timeout={OCR_PAGE_TIMEOUT}s, profile=v1")


def _ocr_runtime() -> tuple[str | None, str | None, str | None]:
    """只用本机 Tesseract；缺少中文模型时明确保留待 OCR 状态。"""
    binary = os.environ.get("BIAOJING_TESSERACT") or shutil.which("tesseract")
    if not binary and sys.platform == "darwin":
        binary = next((p for p in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract")
                       if os.path.isfile(p)), None)
    if not binary and sys.platform == "win32":
        candidates = [os.path.join(os.environ[key], "Tesseract-OCR", "tesseract.exe")
                      for key in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(key)]
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs",
                                           "Tesseract-OCR", "tesseract.exe"))
        binary = next((p for p in candidates if os.path.isfile(p)), None)
    if not binary:
        return None, None, "本机未安装 Tesseract，扫描页仍待 OCR"
    try:
        result = subprocess.run([binary, "--list-langs"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=5, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None, None, "本机 Tesseract 无法启动，扫描页仍待 OCR"
    langs = set(result.stdout.splitlines())
    if result.returncode != 0 or "chi_sim" not in langs:
        return None, None, "本机 Tesseract 缺少 chi_sim 中文模型，扫描页仍待 OCR"
    lang = "chi_sim+eng" if "eng" in langs else "chi_sim"
    return binary, lang, None


def _ocr_png(png: bytes, binary: str, lang: str,
             timeout: float) -> tuple[str, str | None]:
    """OCR 一个 PNG；自动版面为空时补试稀疏文字，共用单项时限。"""
    deadline = time.monotonic() + timeout
    for mode in ("3", "11"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "", "OCR 超时"
        try:
            result = subprocess.run(
                [binary, "stdin", "stdout", "-l", lang, "--psm", mode],
                input=png, capture_output=True, timeout=remaining, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return "", "OCR 超时"
        except OSError:
            return "", "Tesseract 启动失败"
        if result.returncode:
            return "", "Tesseract 识别失败"
        text = result.stdout.decode("utf-8", errors="replace").strip()
        if text:
            if mode == "11" and len(text) < OCR_SPARSE_MIN_CHARS:
                # 少量印章/签字或噪声不能掩盖主体文字未读出；原页继续待复核。
                return "", f"OCR 稀疏补试仅返回 {len(text)} 个字符（少于 {OCR_SPARSE_MIN_CHARS}），不足以确认页面已识别"
            return text, "已通过稀疏文字模式补试，需对照原页核实" if mode == "11" else None
    return "", "OCR 自动版面和稀疏文字模式均未识别到文字"


def _raster_dimensions(data: bytes) -> tuple[int, int] | None:
    """只从常见栅格格式头部读取尺寸，先限像素再交给解码器分配内存。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:3] == b"GIF" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"BM") and len(data) >= 26:
        return abs(int.from_bytes(data[18:22], "little", signed=True)), abs(
            int.from_bytes(data[22:26], "little", signed=True))
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(data):
            break
        size = int.from_bytes(data[i:i + 2], "big")
        if size < 2 or i + size > len(data):
            break
        if marker in sof and size >= 7:
            return (int.from_bytes(data[i + 5:i + 7], "big"),
                    int.from_bytes(data[i + 3:i + 5], "big"))
        i += size
    return None


def ocr_image(data: bytes, binary: str, lang: str,
              timeout: float = OCR_PAGE_TIMEOUT) -> tuple[str, str | None]:
    """OCR 一个有字节数和像素上限的 PNG/JPEG/GIF/BMP 内嵌图。"""
    deadline = time.monotonic() + timeout
    if len(data) > OCR_MAX_IMAGE_BYTES:
        return "", "图片字节数超出 OCR 上限"
    dimensions = _raster_dimensions(data)
    if not dimensions:
        return "", "图片格式无法安全预检（支持 PNG/JPEG/GIF/BMP）"
    width, height = dimensions
    if width < 1 or height < 1 or width * height > OCR_MAX_PIXELS:
        return "", "图片像素超出 OCR 上限"
    try:
        pix = pymupdf.Pixmap(data)
        png = pix.tobytes("png")
    except Exception as exc:
        return "", f"图片解码失败（{type(exc).__name__}）"
    return _ocr_png(png, binary, lang, max(0, deadline - time.monotonic()))


def _ocr_page(page, binary: str, lang: str,
              timeout: float) -> tuple[str, str | None]:
    """OCR 一个有界页面；自动版面为空时补试稀疏文字，共用单页时限。"""
    deadline = time.monotonic() + timeout
    scale = OCR_DPI / 72
    width = math.ceil(page.rect.width * scale)
    height = math.ceil(page.rect.height * scale)
    if width < 1 or height < 1 or width * height > OCR_MAX_PIXELS:
        return "", "页面像素超出 OCR 上限"
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        png = pix.tobytes("png")
    except Exception as exc:
        return "", f"页面渲染失败（{type(exc).__name__}）"
    return _ocr_png(png, binary, lang, max(0, deadline - time.monotonic()))


def page_count(data: bytes) -> int | None:
    """快速取得未加密 PDF 页数；失败时交由正常解析返回具体错误。"""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return None if doc.needs_pass else doc.page_count
    except Exception:
        return None


def parse(data: bytes, ocr_pages: set[int] | None = None,
          progress=None, cancelled=None) -> dict:
    """返回 {ok, status, evidence, counts, metadata, notes, error}。"""
    if ocr_pages is not None and any(type(p) is not int or p < 1 for p in ocr_pages):
        raise ValueError("ocr_pages 须为从 1 开始的物理页码集合")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PDF 无法打开：{type(exc).__name__}: {exc}",
            "evidence": [], "counts": {}, "metadata": None, "notes": [],
        }
    try:
        if doc.needs_pass:
            return {
                "ok": False,
                "status": "failed",
                "error": "PDF 已加密，未提供口令，无法抽取（不猜测内容）",
                "evidence": [], "counts": {}, "metadata": None, "notes": [],
            }
        notes = []
        if doc.is_repaired:
            notes.append("PDF 结构损坏，已由解析库自动修复后读取（原件字节未改动）")
        evidence = []
        pages_text = pages_ocr = pages_pending_ocr = pages_blank = 0
        labels = {}
        ocr_binary = ocr_lang = None
        ocr_checked = False
        ocr_note_added = False
        user_cancelled = False
        pages_processed = 0
        total = doc.page_count
        for i, page in enumerate(doc, start=1):
            if cancelled and cancelled():
                user_cancelled = True
                for pending_page in range(i, total + 1):
                    evidence.append({
                        "kind": "pdf_page", "locator": f"page {pending_page}",
                        "page": pending_page, "page_status": "pending_ocr",
                        "page_error": "用户取消处理；此页尚未扫描，可重试",
                        "page_note": None, "text": "", "ocr_used": False,
                    })
                    pages_pending_ocr += 1
                notes.append(
                    f"用户取消处理；已处理 {i - 1}/{total} 页，"
                    "其余页面已保留待处理，可从工作台重试")
                break
            if progress:
                progress(i, total, "processing", None)
            page_read_failed = False
            page_error = None
            page_note = None
            try:
                text = page.get_text("text")
            except Exception as exc:
                text = ""
                page_read_failed = True
                page_error = f"文本读取失败（{type(exc).__name__}）"
                notes.append(f"第 {i} 页文本读取失败（{type(exc).__name__}），尝试 OCR")
            has_text = bool(text and text.strip())
            try:
                has_images = bool(page.get_images(full=True))
            except Exception:
                has_images = False
                page_read_failed = True
                page_error = page_error or "图像检测失败"
                notes.append(f"第 {i} 页图像检测失败，尝试 OCR，不按空白页处理")
            ocr_used = False
            if has_text:
                page_status = "text"
                pages_text += 1
            elif has_images or page_read_failed:
                if ocr_pages is not None and i not in ocr_pages:
                    page_status = "pending_ocr"
                    page_error = page_error or "本次未重试此页"
                else:
                    if not ocr_checked:
                        ocr_binary, ocr_lang, unavailable = _ocr_runtime()
                        ocr_checked = True
                        if unavailable:
                            notes.append(unavailable)
                    if ocr_binary and ocr_lang:
                        text, ocr_error = _ocr_page(
                            page, ocr_binary, ocr_lang, OCR_PAGE_TIMEOUT)
                        if text:
                            page_status = "ocr"
                            ocr_used = True
                            pages_ocr += 1
                            if ocr_error:
                                page_note = ocr_error
                                notes.append(f"第 {i} 页 {ocr_error}")
                            if not ocr_note_added:
                                notes.append("扫描页文字由本机 OCR 机器识别，须对照原件人工复核")
                                ocr_note_added = True
                        else:
                            page_status = "pending_ocr"
                            page_error = ocr_error or page_error
                            if ocr_error:
                                notes.append(f"第 {i} 页 {ocr_error}，仍待 OCR")
                    else:
                        page_status = "pending_ocr"
                        page_error = unavailable or page_error
                if page_status == "pending_ocr" and page_error is None:
                    page_error = "扫描页仍待 OCR"
                if page_status == "pending_ocr":
                    pages_pending_ocr += 1
            else:
                page_status = "blank"
                pages_blank += 1
            if not text or not text.strip():
                # 无文本页只登记状态；有 OCR 文本时作为可回指页面证据保留
                evidence.append({
                    "kind": "pdf_page",
                    "locator": f"page {i}",
                    "page": i,
                    "page_status": page_status,
                    "page_error": page_error if page_status == "pending_ocr" else None,
                    "page_note": page_note,
                    "text": "",
                    "ocr_used": False,
                })
            else:
                evidence.append({
                    "kind": "pdf_page",
                    "locator": f"page {i}",
                    "page": i,
                    "page_status": page_status,
                    "page_error": page_error if page_status == "pending_ocr" else None,
                    "page_note": page_note,
                    "text": text,
                    "ocr_used": ocr_used,
                })
            try:
                label = page.get_label()
            except Exception:
                label = ""
            if label:
                labels[i] = label
            pages_processed = i
            if progress:
                progress(i, total, "page_done", page_status)
        readable = pages_text + pages_ocr
        if total == 0 or (not user_cancelled and readable == 0
                          and pages_blank == total and total > 0):
            return {
                "ok": False,
                "status": "failed",
                "error": f"PDF 无可抽取内容（{total} 页，全部无文本无图像）",
                "evidence": evidence,
                "counts": _counts(total, pages_text, pages_ocr,
                                  pages_pending_ocr, pages_blank),
                "metadata": _meta(doc),
                "notes": notes,
                "cancelled": user_cancelled,
                "pages_processed": pages_processed,
            }
        if not user_cancelled and readable == total:
            status = "success"
        elif readable == 0 and pages_pending_ocr == total:
            status = "pending_ocr"
        else:
            status = "partial"
        if labels:
            notes.append(f"页标签（PDF PageLabels）与物理页序不同时一并列出：{labels}")
        return {
            "ok": True,
            "status": status,
            "error": None,
            "evidence": evidence,
            "counts": _counts(total, pages_text, pages_ocr,
                              pages_pending_ocr, pages_blank),
            "metadata": _meta(doc),
            "notes": notes,
            "cancelled": user_cancelled,
            "pages_processed": pages_processed,
        }
    finally:
        doc.close()


def _counts(total: int, text: int, ocr: int, pending: int, blank: int) -> dict:
    return {
        "pages_total": total,
        "pages_text": text,
        "pages_ocr": ocr,
        "pages_pending_ocr": pending,
        "pages_blank": blank,
    }


def _meta(doc) -> dict:
    raw = doc.metadata or {}
    return {k: json_safe(v) for k, v in raw.items() if v not in (None, "")}

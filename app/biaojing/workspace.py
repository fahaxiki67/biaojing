# -*- coding: utf-8 -*-
"""标镜 P3 工作台持久化：原件只读存证、覆盖率、候选、确认事实、筛查。

安全与纪律：
  - 原始字节按 SHA-256 哈希路径保存（files/<sha[:2]>/<sha>），客户端
    文件名只作为来源引用记录，绝不作为存储路径。
  - 同字节重复上传：新增一条来源引用（duplicate 状态），保留每个来源。
  - evidence_id 在工作台边界确定性分配（sha + 序号），映射持久化；
    重复导入同源同 ID。
  - 确认/更正字段必须引用可解析的证据，缺证据只能标 unknown；
    更正保留原始候选值。
  - 筛查：确认事实组装为 P2 schema（内存 FactBase）→ screen_all；
    同时写入 P2 store 做事件累计（同确认集幂等，内容变化冲突可见）。
"""

from __future__ import annotations

import datetime
from functools import wraps
import json
import os
import re
import sqlite3
import threading

from . import candidates
from . import store as p2_store
from .rules import FactBase, SCHEMA, screen_all

MAX_UPLOAD_BYTES = 128 * 1024 * 1024  # 单文件/请求体硬上限

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    sha256 TEXT PRIMARY KEY, doc_type TEXT, status TEXT, parser TEXT,
    counts_json TEXT, metadata_json TEXT, source_type TEXT,
    first_ref TEXT, imported_at TEXT, extract_version TEXT);
-- 覆盖率事实表：每次输入出现（含重复/拒收/ZIP 条目）各计一行
CREATE TABLE IF NOT EXISTS source_refs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT, ref TEXT, status TEXT, reason TEXT, seen_at TEXT);
CREATE INDEX IF NOT EXISTS idx_refs_sha ON source_refs(sha256);
CREATE TABLE IF NOT EXISTS evidence_store(
    evidence_id TEXT PRIMARY KEY, sha256 TEXT, ordinal INTEGER,
    kind TEXT, locator_display TEXT, locator_json TEXT,
    quote TEXT, source_type TEXT, ocr_used INTEGER NOT NULL DEFAULT 0,
    page_no INTEGER, page_status TEXT, page_error TEXT, page_note TEXT,
    extract_version TEXT);
CREATE INDEX IF NOT EXISTS idx_evidence_sha ON evidence_store(sha256);
CREATE TABLE IF NOT EXISTS candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT, field TEXT, value_json TEXT, evidence_id TEXT,
    locator_json TEXT, locator_display TEXT, note TEXT,
    companion_json TEXT);
CREATE TABLE IF NOT EXISTS confirmations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    field TEXT NOT NULL, value TEXT, evidence_id TEXT,
    source_role TEXT DEFAULT 'unknown',
    original_candidate TEXT, action TEXT, confirmed_at TEXT,
    UNIQUE(event_id, lot_id, bidder_id, field));
CREATE TABLE IF NOT EXISTS findings_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT, findings_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class Workbench:
    def __init__(self, root: str):
        # ponytail: 单工作台串行访问共享连接；后台任务队列在需要并发 OCR 时再引入。
        self._lock = threading.RLock()
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(os.path.join(self.root, "files"), exist_ok=True)
        self.db_path = os.path.join(self.root, "biaojing.sqlite3")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """守卫式迁移：CREATE TABLE IF NOT EXISTS 不会为已存在的旧表补列，
        旧版工作区（无 companion_json / source_role）打开时按需 ALTER。"""
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(candidates)")}
        if cols and "companion_json" not in cols:
            self.conn.execute(
                "ALTER TABLE candidates ADD COLUMN companion_json TEXT")
        conf_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(confirmations)")}
        if conf_cols and "source_role" not in conf_cols:
            self.conn.execute(
                "ALTER TABLE confirmations ADD COLUMN source_role TEXT"
                " DEFAULT 'unknown'")
        evidence_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(evidence_store)")}
        if evidence_cols and "ocr_used" not in evidence_cols:
            self.conn.execute(
                "ALTER TABLE evidence_store ADD COLUMN ocr_used INTEGER"
                " NOT NULL DEFAULT 0")
        for col, sql_type in (("page_no", "INTEGER"), ("page_status", "TEXT"),
                              ("page_error", "TEXT"), ("page_note", "TEXT"),
                              ("extract_version", "TEXT")):
            if evidence_cols and col not in evidence_cols:
                self.conn.execute(
                    f"ALTER TABLE evidence_store ADD COLUMN {col} {sql_type}")
        source_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(sources)")}
        if source_cols and "extract_version" not in source_cols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN extract_version TEXT")
        if evidence_cols and "page_no" in {r[1] for r in self.conn.execute(
                "PRAGMA table_info(evidence_store)")}:
            for row in self.conn.execute(
                    "SELECT evidence_id, kind, locator_display, quote, ocr_used,"
                    " page_no, page_status, extract_version FROM evidence_store"
                    " WHERE extract_version IS NULL OR (kind='pdf_page' AND"
                    " (page_no IS NULL OR page_status IS NULL))").fetchall():
                if row["kind"] != "pdf_page":
                    if not row["extract_version"]:
                        self.conn.execute(
                            "UPDATE evidence_store SET extract_version=?"
                            " WHERE evidence_id=?",
                            ("legacy（导入时未记录解析版本）", row["evidence_id"]))
                    continue
                page_no = row["page_no"]
                if page_no is None:
                    match = re.fullmatch(r"page\s+(\d+)",
                                         row["locator_display"] or "", re.I)
                    page_no = int(match.group(1)) if match else None
                page_status = row["page_status"] or (
                    "ocr" if row["ocr_used"] else
                    "text" if (row["quote"] or "").strip() else "pending_ocr")
                self.conn.execute(
                    "UPDATE evidence_store SET page_no=?, page_status=?,"
                    " page_error=COALESCE(page_error, ?),"
                    " extract_version=COALESCE(extract_version, ?)"
                    " WHERE evidence_id=?",
                    (page_no, page_status,
                     "历史导入未保留页级失败原因" if page_status == "pending_ocr" else None,
                     "legacy（导入时未记录解析版本）", row["evidence_id"]))
        self.conn.execute(
            "UPDATE sources SET extract_version=COALESCE(extract_version,"
            " 'legacy（导入时未记录解析版本）') WHERE extract_version IS NULL")

    @_locked
    def close(self):
        self.conn.close()

    # ------------------------------------------------------------ 上传/解析

    def _persist_bytes(self, data: bytes) -> str:
        """原始字节按 SHA-256 哈希路径只读保存，返回 sha256。"""
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        file_path = os.path.join(self.root, "files", sha[:2], sha)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        if not os.path.exists(file_path):
            with open(file_path, "wb") as f:
                f.write(data)
        return sha

    @_locked
    def ingest_bytes(self, name: str, data: bytes,
                     source_type: str = "unknown", progress=None,
                     cancelled=None) -> dict:
        """单个文件字节 → 哈希存证 + P1 解析 + 证据 ID + 候选。"""
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        ref = f"{name}（{len(data)} 字节）"
        now = _now()
        first = self.conn.execute(
            "SELECT sha256 FROM sources WHERE sha256=?", (sha,)).fetchone()
        occurrence_status = "duplicate" if first else None  # 首现状态由解析决定
        self.conn.execute(
            "INSERT INTO source_refs(sha256, ref, status, reason, seen_at)"
            " VALUES(?,?,?,?,?)",
            (sha, ref, occurrence_status or "pending_parse",
             # 首次出现无重复原因（空）；重复 SHA 才说明与先前来源同字节
             "与先前来源字节相同" if first else None, now))
        if first:
            self.conn.commit()
            return {"sha256": sha, "status": "duplicate", "ref": ref,
                    "duplicate_of_ref": self.conn.execute(
                        "SELECT first_ref FROM sources WHERE sha256=?",
                        (sha,)).fetchone()[0]}
        # 原始字节只读保存（哈希路径，绝不使用客户端文件名）
        sha = self._persist_bytes(data)
        # P1 解析（复用公开入口）。origin.path 必须用原始文件名——
        # 类型判定按扩展名+magic，ref 字符串带字节后缀会破坏判定
        from . import cli as p1_cli
        rec = p1_cli._process_one(
            display=ref, origin={"kind": "filesystem", "path": name},
            data=data, error=None, source_type=source_type,
            max_evidence=20000, max_scan_rows=50000, max_scan_cells=2_000_000,
            max_zip_entries=1000, max_zip_total_bytes=512 * 1024 * 1024,
            seen_sha={}, progress=progress, cancelled=cancelled)
        doc_type = rec.get("doc_type", "unknown")
        status = rec.get("extract_status", "failed")
        from . import docx_parser, pdf_parser
        extract_version = (
            pdf_parser.EXTRACTION_VERSION if doc_type == "pdf" else
            docx_parser.EXTRACTION_VERSION if doc_type == "docx" else
            rec.get("parser") or "P1 未记录")
        self.conn.execute(
            "INSERT INTO sources(sha256, doc_type, status, parser, counts_json,"
            " metadata_json, source_type, first_ref, imported_at, extract_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sha, doc_type, status, rec.get("parser"),
             json.dumps(rec.get("counts", {}), ensure_ascii=False),
             json.dumps(rec.get("metadata", {}), ensure_ascii=False),
             source_type, ref, now, extract_version))
        if doc_type not in ("zip",) and status not in ("failed", "unknown"):
            tagged = candidates.assign_evidence_ids(sha, rec.get("evidence", []))
            for i, e in enumerate(tagged, start=1):
                p2_loc = candidates.to_p2_locator(e)
                quote = str(e.get("text") if e.get("text") is not None
                            else e.get("value") if e.get("value") is not None
                            else "")
                self.conn.execute(
                    "INSERT OR REPLACE INTO evidence_store"
                    " (evidence_id, sha256, ordinal, kind, locator_display,"
                    " locator_json, quote, source_type, ocr_used, page_no,"
                    " page_status, page_error, page_note, extract_version)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e["evidence_id"], sha, i, e.get("kind"),
                     e.get("locator", ""),
                     json.dumps(p2_loc, ensure_ascii=False),
                     quote, source_type, int(bool(e.get("ocr_used", False))),
                     e.get("page"), e.get("page_status"), e.get("page_error"),
                     e.get("page_note"),
                     extract_version))
            cands = candidates.extract_candidates(sha, doc_type, tagged)
            for c in cands:
                self.conn.execute(
                    "INSERT INTO candidates(sha256, field, value_json,"
                    " evidence_id, locator_json, locator_display, note,"
                    " companion_json) VALUES(?,?,?,?,?,?,?,?)",
                    (sha, c["field"],
                     json.dumps(c["value"], ensure_ascii=False, default=str),
                     c["evidence_id"],
                     json.dumps(c["locator"], ensure_ascii=False),
                     c.get("locator_display", ""), c.get("note", ""),
                     json.dumps(c.get("companion"), ensure_ascii=False)
                     if c.get("companion") else None))
        else:
            cands = []
        # 该次出现的状态 = 主解析状态（duplicate 出现已在前面标记）
        reason = "\n".join(str(x) for x in [rec.get("error"), *rec.get("notes", [])] if x)
        self.conn.execute(
            "UPDATE source_refs SET status=?, reason=? WHERE sha256=?"
            " AND status='pending_parse'", (status, reason or None, sha))
        self.conn.commit()
        return {"sha256": sha, "status": status, "ref": ref,
                "doc_type": doc_type, "candidates": len(cands),
                "counts": rec.get("counts", {}),
                "cancelled": bool(rec.get("cancelled", False))}

    @_locked
    def retry_ocr(self, sha256: str, progress=None, cancelled=None) -> dict:
        """只重试该 PDF 尚待 OCR 的页；原件、确认项及既有证据 ID 不变。"""
        import hashlib

        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("非法 SHA-256")
        source = self.conn.execute(
            "SELECT doc_type FROM sources WHERE sha256=?", (sha256,)).fetchone()
        if source is None:
            raise ValueError("工作区中不存在该文件")
        if source["doc_type"] != "pdf":
            raise ValueError("按页重试 OCR 仅支持 PDF")
        pending = self.conn.execute(
            "SELECT page_no FROM evidence_store WHERE sha256=?"
            " AND page_status='pending_ocr' ORDER BY page_no", (sha256,)).fetchall()
        if not pending:
            return {"ok": True, "retried_pages": 0, "pages_pending_ocr": 0,
                    "message": "没有待重试页"}
        if any(r["page_no"] is None for r in pending):
            raise ValueError("待重试页面缺少物理页码，无法安全重试")
        pages = {r["page_no"] for r in pending}
        data = self.original_bytes(sha256)
        if data is None or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("哈希路径中的原件缺失或 SHA-256 不匹配，已停止重试")

        from . import pdf_parser
        result = pdf_parser.parse(data, ocr_pages=pages, progress=progress,
                                  cancelled=cancelled)
        tagged = candidates.assign_evidence_ids(sha256, result.get("evidence", []))
        updated = {e["page"]: e for e in tagged if e.get("page") in pages}
        if updated.keys() != pages:
            raise ValueError("解析结果缺少待重试页，未更新工作台")
        new_candidates = candidates.extract_candidates(
            sha256, "pdf", [e for e in updated.values() if (e.get("text") or "").strip()])
        with self.conn:
            for ev in updated.values():
                self.conn.execute(
                    "UPDATE evidence_store SET quote=?, ocr_used=?, page_status=?,"
                    " page_error=?, page_note=?, extract_version=?"
                    " WHERE sha256=? AND page_no=?",
                    (ev.get("text", ""), int(bool(ev.get("ocr_used"))),
                     ev.get("page_status"), ev.get("page_error"),
                     ev.get("page_note"),
                     pdf_parser.EXTRACTION_VERSION, sha256, ev["page"]))
            for c in new_candidates:
                self.conn.execute(
                    "INSERT INTO candidates(sha256, field, value_json, evidence_id,"
                    " locator_json, locator_display, note, companion_json)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sha256, c["field"],
                     json.dumps(c["value"], ensure_ascii=False, default=str),
                     c["evidence_id"], json.dumps(c["locator"], ensure_ascii=False),
                     c.get("locator_display", ""), c.get("note", ""),
                     json.dumps(c.get("companion"), ensure_ascii=False)
                     if c.get("companion") else None))
            statuses = [r["page_status"] or "pending_ocr" for r in self.conn.execute(
                "SELECT page_status FROM evidence_store WHERE sha256=?"
                " AND kind='pdf_page'", (sha256,))]
            counts = {"pages_total": len(statuses),
                      "pages_text": statuses.count("text"),
                      "pages_ocr": statuses.count("ocr"),
                      "pages_pending_ocr": statuses.count("pending_ocr"),
                      "pages_blank": statuses.count("blank")}
            readable = counts["pages_text"] + counts["pages_ocr"]
            if not statuses or (not readable and counts["pages_blank"] == len(statuses)):
                status = "failed"
            elif readable == len(statuses):
                status = "success"
            elif not readable and counts["pages_pending_ocr"] == len(statuses):
                status = "pending_ocr"
            else:
                status = "partial"

            ref = self.conn.execute(
                "SELECT id, reason FROM source_refs WHERE sha256=?"
                " AND status IN ('partial','pending_ocr') ORDER BY id LIMIT 1",
                (sha256,)).fetchone()
            reason_lines = []
            if ref and ref["reason"]:
                for line in ref["reason"].splitlines():
                    match = re.match(r"^第\s*(\d+)\s*页", line)
                    if match and int(match.group(1)) in pages:
                        continue
                    if line.startswith(("扫描页文字由本机 OCR", "本机未安装 Tesseract",
                                        "本机 Tesseract")):
                        continue
                    reason_lines.append(line)
            for ev in updated.values():
                if ev["page_status"] == "pending_ocr":
                    reason_lines.append(
                        f"第 {ev['page']} 页 {ev.get('page_error') or '仍待 OCR'}，仍待 OCR")
                elif ev.get("page_note"):
                    reason_lines.append(f"第 {ev['page']} 页 {ev['page_note']}")
            if counts["pages_ocr"]:
                reason_lines.append("扫描页文字由本机 OCR 机器识别，须对照原件人工复核")
            reason = "\n".join(dict.fromkeys(line for line in reason_lines if line)) or None
            self.conn.execute(
                "UPDATE sources SET status=?, counts_json=?, extract_version=?"
                " WHERE sha256=?",
                (status, json.dumps(counts, ensure_ascii=False),
                 pdf_parser.EXTRACTION_VERSION, sha256))
            if ref:
                self.conn.execute(
                    "UPDATE source_refs SET status=?, reason=? WHERE id=?",
                    (status, reason, ref["id"]))
        processed = result.get("pages_processed", len(pages))
        return {"ok": True, "sha256": sha256, "status": status,
                "retried_pages": sum(page <= processed for page in pages),
                "pages_pending_ocr": counts["pages_pending_ocr"],
                "counts": counts,
                "cancelled": bool(result.get("cancelled", False))}

    @_locked
    def ingest_zip(self, name: str, data: bytes,
                   source_type: str = "unknown") -> list[dict]:
        """ZIP：容器本体按哈希存证并记归档级 occurrence；同 SHA 重复上传
        记 duplicate 但仍展开条目（条目各成 duplicate）；展开失败把该归档
        occurrence 原地更新为 failed（不留 archived+failed 双行）；
        条目与拒收逐条记录。归档行保留调用方提供的 source_type。"""
        import io
        import zipfile

        from . import zip_container
        results = []
        # 归档本体先按 SHA-256 哈希路径只读存证，并记一条归档级 occurrence
        sha = self._persist_bytes(data)
        now = _now()
        existing = self.conn.execute(
            "SELECT sha256 FROM sources WHERE sha256=?", (sha,)).fetchone()
        arch_status = "duplicate" if existing else "archived"
        self.conn.execute(
            "INSERT INTO source_refs(sha256, ref, status, reason, seen_at)"
            " VALUES(?,?,?,?,?)",
            (sha, name, arch_status,
             "与先前 ZIP 字节相同" if existing
             else "ZIP 容器原件已按哈希存证", now))
        if not existing:
            self.conn.execute(
                "INSERT INTO sources(sha256, doc_type, status, source_type,"
                " first_ref, imported_at) VALUES(?,?,?,?,?,?)",
                (sha, "zip", "archived", source_type, name, now))
        self.conn.commit()
        results.append({"sha256": sha, "status": arch_status, "ref": name})
        try:
            expanded = zip_container.expand(
                data, max_entries=1000, max_total_uncompressed=512 * 1024 * 1024)
        except Exception as exc:
            # 展开失败：归档 occurrence 原地转 failed，不留双行
            self.conn.execute(
                "UPDATE source_refs SET status='failed', reason=? WHERE"
                " sha256=? AND status IN ('archived','duplicate')",
                (f"ZIP 无法展开：{exc}", sha))
            self.conn.execute(
                "UPDATE sources SET status='failed' WHERE sha256=?", (sha,))
            self.conn.commit()
            results.append({"sha256": sha, "status": "failed", "ref": name,
                            "error": str(exc)})
            return results
        for entry in expanded["entries"]:
            results.append(self.ingest_bytes(f"{name}::{entry['name']}",
                                             entry["data"], source_type))
        for rej in expanded["rejected"]:
            ref = f"{name}::{rej['name']}"
            self.conn.execute(
                "INSERT INTO source_refs(sha256, ref, status, reason, seen_at)"
                " VALUES(?,?,?,?,?)",
                (None, ref, "rejected", f"ZIP 条目被拒收：{rej['reason']}",
                 _now()))
            results.append({"ref": ref, "status": "rejected",
                            "reason": rej["reason"]})
        self.conn.commit()
        return results

    @_locked
    def record_rejected(self, name: str, reason: str) -> None:
        """超限/拒收的上传尝试进入可见覆盖率（带原因）。长度双保险截断
        （HTTP 层与存储层各防一次）。"""
        self.conn.execute(
            "INSERT INTO source_refs(sha256, ref, status, reason, seen_at)"
            " VALUES(?,?,?,?,?)",
            (None, str(name)[:512], "rejected", str(reason)[:300], _now()))
        self.conn.commit()

    # ------------------------------------------------------------ 覆盖率

    @_locked
    def coverage(self) -> dict:
        """覆盖率按**输入出现次数**统计（source_refs 每次出现一行，
        含重复出现、ZIP 条目与被拒收的请求）。"""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM source_refs GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total_refs = self.conn.execute(
            "SELECT COUNT(*) AS n FROM source_refs").fetchone()["n"]
        return {
            "total_input_occurrences": total_refs,
            "unique_files": self.conn.execute(
                "SELECT COUNT(*) AS n FROM sources").fetchone()["n"],
            "by_status": by_status,
            "status_order": ["success", "partial", "failed", "pending_ocr",
                             "pending_convert", "duplicate", "rejected",
                             "archived", "unknown"],
        }

    @_locked
    def evidence_usable(self, evidence_id: str) -> bool:
        """证据可解析且 locator 符合 P2 typed 契约（确认/更正前置校验）。"""
        from .rules import _locator_valid

        row = self.conn.execute(
            "SELECT locator_json FROM evidence_store WHERE evidence_id=?",
            (evidence_id,)).fetchone()
        if row is None:
            return False
        try:
            loc = json.loads(row["locator_json"])
        except ValueError:
            return False
        return _locator_valid(loc)

    @_locked
    def refs_for_sha(self, sha256: str) -> list[str]:
        """该 SHA-256 的全部来源引用（含重复出现）。"""
        return [r["ref"] for r in self.conn.execute(
            "SELECT ref FROM source_refs WHERE sha256=? ORDER BY id",
            (sha256,))]

    @_locked
    def original_bytes(self, sha256: str) -> bytes | None:
        path = os.path.join(self.root, "files", sha256[:2], sha256)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    @_locked
    def display_name_for(self, sha256: str) -> str | None:
        row = self.conn.execute(
            "SELECT first_ref FROM sources WHERE sha256=?",
            (sha256,)).fetchone()
        if not row:
            return None
        name = re.sub(r"（\d+ 字节）$", "", row["first_ref"])
        return name.replace("\\", "/").rsplit("::", 1)[-1].rsplit("/", 1)[-1]

    @_locked
    def evidence_by_id(self, evidence_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM evidence_store WHERE evidence_id=?",
            (evidence_id,)).fetchone()
        if row is None:
            return None
        return {
            "evidence_id": row["evidence_id"], "sha256": row["sha256"],
            "kind": row["kind"], "locator_display": row["locator_display"],
            "quote": row["quote"], "source_type": row["source_type"],
            "ocr_used": bool(row["ocr_used"]),
            "page_no": row["page_no"], "page_status": row["page_status"],
            "page_error": row["page_error"], "page_note": row["page_note"],
            "extract_version": row["extract_version"],
        }

    @_locked
    def state(self) -> dict:
        """全量状态：覆盖率（按输入出现次数）+ 来源/候选/确认/最近筛查。"""
        cov = self.coverage()
        sources = [dict(r) for r in self.conn.execute(
            "SELECT sha256, doc_type, status, parser, counts_json,"
            " source_type, first_ref, extract_version"
            " FROM sources ORDER BY imported_at")]
        # 来源行：每次输入出现一行（含 duplicate/rejected），重复可见
        source_rows = [dict(r) for r in self.conn.execute(
            "SELECT r.ref, r.status, r.reason, s.doc_type, s.parser,"
            " s.sha256, s.counts_json, s.extract_version"
            " FROM source_refs r LEFT JOIN sources s ON s.sha256 = r.sha256"
            " ORDER BY r.id")]
        cands = [dict(r) for r in self.conn.execute(
            "SELECT id, sha256, field, value_json, evidence_id,"
            " locator_display, note, companion_json FROM candidates ORDER BY id")]
        for c in cands:
            c["value"] = json.loads(c.pop("value_json"))
            comp = c.pop("companion_json")
            c["companion"] = json.loads(comp) if comp else None
        confirms = [dict(r) for r in self.conn.execute(
            "SELECT event_id, lot_id, bidder_id, field, value, evidence_id,"
            " original_candidate, action, confirmed_at FROM confirmations"
            " ORDER BY id")]
        last_findings = self.conn.execute(
            "SELECT findings_json FROM findings_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        findings = json.loads(last_findings["findings_json"]) \
            if last_findings else None
        return {
            "product": "标镜", "product_name": "标镜",
            "coverage": cov, "sources": sources, "source_rows": source_rows,
            "candidates": cands, "confirmations": confirms,
            "findings": findings,
        }

    # ------------------------------------------------------------ 确认字段

    @_locked
    def confirm_field(self, event_id: str, lot_id: str, bidder_id: str,
                      field: str, value, evidence_id: str | None,
                      action: str = "confirm",
                      original_candidate: str | None = None,
                      source_role: str = "unknown") -> dict:
        """确认/更正/标 unknown 一个字段。

        校验（全部通过才写库，否则拒绝且不留任何行）：
          - action 只接受 confirm/correct/unknown；
          - 确认/更正的 evidence_id 必须可解析且 locator 符合 P2
            typed 契约（复用 rules._locator_valid）；
          - total_price 只接受有限数值且排除布尔值；
          - tax_included 只接受布尔值（unknown 走 unknown 流程）；
          - 联系人来源角色显式选择（默认 unknown）：只有显式 "bidder"
            参与 R002——确认电话号码数值不等于确认其来源角色。
        更正保留原始候选值。
        """
        if not all(isinstance(x, str) and x.strip()
                   for x in (event_id, lot_id, bidder_id, field)):
            return {"ok": False, "error": "event_id/lot_id/bidder_id/field 必填"}
        if action not in ("confirm", "correct", "unknown"):
            return {"ok": False, "error": f"未知操作：{action!r}"
                    "（只接受 confirm/correct/unknown）"}
        if source_role not in ("unknown", "bidder", "tenderer", "agency",
                               "platform", "public_service"):
            return {"ok": False, "error": f"来源角色非法：{source_role!r}"}
        if evidence_id is not None and not isinstance(evidence_id, str):
            return {"ok": False, "error": "evidence_id 必须是字符串"}
        if original_candidate is not None and not isinstance(original_candidate, str):
            return {"ok": False, "error": "original_candidate 必须是字符串"}
        if action in ("confirm", "correct"):
            if evidence_id is None or not self.evidence_usable(evidence_id):
                return {"ok": False,
                        "error": f"证据 {evidence_id!r} 缺失或定位无效："
                                 "缺证据不得确认参与规则的字段"}
        if action == "unknown":
            value = "unknown"
            evidence_id = None
        if field == "total_price" and action in ("confirm", "correct"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value != value or value in (float("inf"), float("-inf")):
                return {"ok": False,
                        "error": "total_price 只接受有限数值（布尔与非数值"
                                 "一律拒绝，unknown 用 unknown 操作表达）"}
        if field == "tax_included" and action in ("confirm", "correct") \
                and not isinstance(value, bool):
            return {"ok": False,
                    "error": "tax_included 只接受布尔值（unknown 用 unknown"
                             " 操作表达）"}
        self.conn.execute(
            "INSERT INTO confirmations(event_id, lot_id, bidder_id, field,"
            " value, evidence_id, source_role, original_candidate, action,"
            " confirmed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(event_id, lot_id, bidder_id, field) DO UPDATE SET"
            " value=excluded.value, evidence_id=excluded.evidence_id,"
            " source_role=excluded.source_role,"
            " original_candidate=excluded.original_candidate,"
            " action=excluded.action, confirmed_at=excluded.confirmed_at",
            (event_id, lot_id, bidder_id, field,
             json.dumps(value, ensure_ascii=False, default=str)
             if not isinstance(value, str) else value,
             evidence_id, source_role, original_candidate, action, _now()))
        self.conn.commit()
        return {"ok": True}

    # ------------------------------------------------------------ 组装与筛查

    @_locked
    def build_p2_facts(self) -> dict:
        """从确认字段组装 P2 schema 归一化事实（含证据注册表）。"""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM confirmations ORDER BY id")]
        events = {}
        evidence = {}

        def ev_json(eid):
            # 共享证据注册表：role 不随单一联系人的来源归属改变——
            # 同一证据可支持多个字段；归属保真在 contact.source_role
            info = self.evidence_by_id(eid) if eid else None
            if info is None:
                return None
            if eid not in evidence:
                evidence[eid] = {
                    "evidence_id": eid, "file_sha256": info["sha256"],
                    "source_type": info["source_type"] or "unknown",
                    "role": "unknown", "quote": info["quote"],
                    "locator": self._p2_locator(eid),
                }
            return eid

        def val(raw):
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return raw

        for r in rows:
            event_id, lot_id, bidder_id = r["event_id"], r["lot_id"], r["bidder_id"]
            event = events.setdefault(event_id, {
                "event_id": event_id, "project_id": event_id,
                "event_name": event_id,
                "lots": {}})
            lot = event["lots"].setdefault(lot_id, {"lot_id": lot_id,
                                                    "lot_name": lot_id,
                                                    "bids": {}})
            bid = lot["bids"].setdefault(bidder_id, {
                "bidder_id": bidder_id, "bidder_name": bidder_id,
                "role": "bidder", "evidence": ev_json(r["evidence_id"]) or None,
                "outcome": {"status": "unknown"}})
            field, value = r["field"], val(r["value"])
            ev_id = ev_json(r["evidence_id"])

            def bind(**kw):
                for k, v in kw.items():
                    if v is not None:
                        bid[k] = v

            if field == "bidder_name":
                bind(bidder_name=str(value),
                     bidder_name_evidence=ev_id)
            elif field == "total_price":
                # 保留原类型，让 FactBase 在筛查入口拒绝损坏/旧版非法值，
                # 不把坏数据静默折成 unknown。
                bind(total_price=value, total_price_evidence=ev_id)
            elif field == "currency":
                cur = str(value)
                bind(currency="CNY" if "人民币" in cur or cur.upper() == "CNY"
                     else cur)
            elif field == "tax_included":
                bind(tax_included=None if value == "unknown" else value)
            elif field == "uscc":
                bind(uscc=str(value), uscc_evidence=ev_id)
            elif field == "outcome_result":
                st = "known" if str(value) in ("won", "lost") else "unknown"
                bid["outcome"] = {"status": st,
                                  "result": str(value) if st == "known" else None}
            elif field in ("contact_phone", "contact_email", "bank_account"):
                # 来源角色由操作人确认时显式选择（默认 unknown）：
                # 只有显式 "bidder" 才会参与 R002 交叉，代理/平台/公共
                # 电话不因确认数值而误报。账户映射为 kind="account"，
                # 同样纳入 R002 账户交叉筛查
                contacts = bid.setdefault("contacts", [])
                kind = {"contact_phone": "phone",
                        "contact_email": "email",
                        "bank_account": "account"}[field]
                contacts.append({"kind": kind, "value": str(value),
                                 "source_role": r.get("source_role")
                                 or "unknown",
                                 "evidence": ev_json(ev_id)})
            elif field in ("person_manager", "person_tech",
                           "authorize_rep", "legal_rep"):
                persons = bid.setdefault("persons", [])
                persons.append({"person_id": f"{bidder_id}:{field}:{value}",
                                "name": str(value), "id_number": "unknown",
                                "role": field, "evidence": ev_id})
            # 其余字段（project_name/code、lot_name、id_number 等）当前仅
            # 留档展示，不映射进 P2 规则事实

        out_events = []
        for event_id in sorted(events):
            ev = events[event_id]
            lots_json = []
            for lot_id in sorted(ev["lots"]):
                lot = ev["lots"][lot_id]
                lot["bids"] = [lot["bids"][k] for k in sorted(lot["bids"])]
                lots_json.append(lot)
            out_events.append({"event_id": event_id,
                               "project_id": ev["project_id"],
                               "event_name": ev["event_name"],
                               "lots": lots_json})
        return {"schema": SCHEMA, "events": out_events,
                "evidence": [evidence[k] for k in sorted(evidence)]}

    def _p2_locator(self, evidence_id: str) -> dict:
        row = self.conn.execute(
            "SELECT locator_json FROM evidence_store WHERE evidence_id=?",
            (evidence_id,)).fetchone()
        try:
            loc = json.loads(row["locator_json"]) if row else None
        except ValueError:
            loc = None
        return loc if isinstance(loc, dict) else {"kind": "unknown"}

    # ------------------------------------------------------------ 筛查

    @_locked
    def run_screen(self) -> dict:
        """内存 FactBase 筛查（finding 随当前确认事实变化）+ P2 store
        事件累计（同确认集幂等、内容变化冲突可见）。"""
        facts = self.build_p2_facts()
        outcome = screen_all(FactBase.from_dict(facts))
        # 复用 P2 store.connect 做 schema 初始化（裸连接会缺表）
        p2_conn = p2_store.connect(os.path.join(self.root, "p2_events.sqlite3"))
        try:
            # 事件累计：复用 P2 store.import_batch（同确认集幂等）
            import_result = p2_store.import_batch(facts, p2_conn)
            event_count = p2_store.event_count(p2_conn)
        finally:
            p2_conn.close()
        now = _now()
        self.conn.execute(
            "INSERT INTO findings_log(run_at, findings_json) VALUES(?,?)",
            (now, json.dumps(outcome["findings"], ensure_ascii=False)))
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES('last_screen_at', ?)", (now,))
        self.conn.commit()
        return {
            "findings": outcome["findings"],
            "summary": outcome["summary"],
            "excluded_facts": outcome["excluded_facts"],
            "unresolved_evidence": outcome["unresolved_evidence"],
            "import": {**import_result.as_dict(),
                       "event_count_after": event_count},
            "disclaimer": outcome["disclaimer"],
        }

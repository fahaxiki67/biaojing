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
import tempfile
import threading

from . import candidates
from . import store as p2_store
from .rules import FactBase, RULE_VERSION, SCHEMA, screen_all

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
    companion_json TEXT,
    candidate_meta_json TEXT);
CREATE TABLE IF NOT EXISTS confirmations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    field TEXT NOT NULL, value TEXT, evidence_id TEXT,
    source_role TEXT DEFAULT 'unknown',
    original_candidate TEXT, action TEXT, confirmed_at TEXT,
    person_id TEXT, evidence_version INTEGER,
    UNIQUE(event_id, lot_id, bidder_id, field));
CREATE TABLE IF NOT EXISTS confirmation_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    field TEXT NOT NULL, old_value TEXT, new_value TEXT,
    old_evidence_id TEXT, evidence_id TEXT, action TEXT NOT NULL,
    original_candidate TEXT, person_id TEXT, evidence_version INTEGER,
    actor TEXT NOT NULL DEFAULT '本机用户', changed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_confirmation_history_scope
    ON confirmation_history(event_id, lot_id, bidder_id, field, id);
CREATE TABLE IF NOT EXISTS contact_facts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    field TEXT NOT NULL, value TEXT NOT NULL, evidence_id TEXT,
    source_role TEXT NOT NULL DEFAULT 'unknown', original_candidate TEXT,
    action TEXT NOT NULL, confirmed_at TEXT NOT NULL,
    UNIQUE(event_id, lot_id, bidder_id, field, evidence_id));
CREATE TABLE IF NOT EXISTS person_facts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    field TEXT NOT NULL, value TEXT NOT NULL, evidence_id TEXT,
    person_id TEXT, original_candidate TEXT, action TEXT NOT NULL,
    evidence_version INTEGER, confirmed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_person_facts_scope
    ON person_facts(event_id, lot_id, bidder_id, field, id);
CREATE TABLE IF NOT EXISTS file_bindings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    sha256 TEXT NOT NULL, context TEXT NOT NULL DEFAULT 'bid_document',
    source_type TEXT NOT NULL DEFAULT 'unknown', declared_owner_id TEXT,
    declared_uscc TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(event_id, lot_id, bidder_id, sha256));
CREATE TABLE IF NOT EXISTS file_binding_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    sha256 TEXT NOT NULL, old_value TEXT, new_value TEXT NOT NULL,
    changed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL, version INTEGER NOT NULL,
    quote TEXT NOT NULL, ocr_used INTEGER NOT NULL DEFAULT 0,
    page_status TEXT, page_error TEXT, page_note TEXT,
    extract_version TEXT, created_at TEXT NOT NULL,
    UNIQUE(evidence_id, version));
CREATE TABLE IF NOT EXISTS screen_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
    rules_version TEXT NOT NULL, facts_sha256 TEXT NOT NULL,
    facts_json TEXT NOT NULL, outcome_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed', error TEXT);
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
        if cols and "candidate_meta_json" not in cols:
            self.conn.execute(
                "ALTER TABLE candidates ADD COLUMN candidate_meta_json TEXT")
        conf_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(confirmations)")}
        if conf_cols and "source_role" not in conf_cols:
            self.conn.execute(
                "ALTER TABLE confirmations ADD COLUMN source_role TEXT"
                " DEFAULT 'unknown'")
        for col, sql_type in (("person_id", "TEXT"),
                              ("evidence_version", "INTEGER")):
            if conf_cols and col not in conf_cols:
                self.conn.execute(
                    f"ALTER TABLE confirmations ADD COLUMN {col} {sql_type}")
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
        run_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(screen_runs)")}
        if run_cols and "status" not in run_cols:
            self.conn.execute(
                "ALTER TABLE screen_runs ADD COLUMN status TEXT NOT NULL"
                " DEFAULT 'completed'")
        if run_cols and "error" not in run_cols:
            self.conn.execute("ALTER TABLE screen_runs ADD COLUMN error TEXT")
        self.conn.execute(
            "INSERT OR IGNORE INTO evidence_versions"
            " (evidence_id, version, quote, ocr_used, page_status, page_error,"
            " page_note, extract_version, created_at)"
            " SELECT evidence_id, 1, quote, ocr_used, page_status, page_error,"
            " page_note, extract_version, ? FROM evidence_store",
            (_now(),))
        migrated = self.conn.execute(
            "SELECT value FROM meta WHERE key='contact_facts_migrated'").fetchone()
        if not migrated:
            self.conn.execute(
                "INSERT OR IGNORE INTO contact_facts"
                " (event_id, lot_id, bidder_id, field, value, evidence_id,"
                " source_role, original_candidate, action, confirmed_at)"
                " SELECT event_id, lot_id, bidder_id, field, value, evidence_id,"
                " COALESCE(source_role, 'unknown'), original_candidate,"
                " COALESCE(action, 'confirm'), COALESCE(confirmed_at, ? )"
                " FROM confirmations WHERE field IN"
                " ('contact_phone','contact_email','bank_account')"
                " AND evidence_id IS NOT NULL",
                (_now(),))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value)"
                " VALUES('contact_facts_migrated','1')")
        people_migrated = self.conn.execute(
            "SELECT value FROM meta WHERE key='person_facts_migrated'").fetchone()
        if not people_migrated:
            self.conn.execute(
                "INSERT INTO person_facts"
                " (event_id,lot_id,bidder_id,field,value,evidence_id,person_id,"
                " original_candidate,action,evidence_version,confirmed_at)"
                " SELECT event_id,lot_id,bidder_id,field,value,evidence_id,person_id,"
                " original_candidate,COALESCE(action,'confirm'),evidence_version,"
                " COALESCE(confirmed_at,?) FROM confirmations WHERE field IN"
                " ('person_manager','person_tech','authorize_rep','legal_rep','id_number')",
                (_now(),))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value)"
                " VALUES('person_facts_migrated','1')")

    @_locked
    def close(self):
        self.conn.close()

    # ------------------------------------------------------------ 上传/解析

    def _persist_bytes(self, data: bytes) -> str:
        """原件原子写入哈希路径；已存在文件必须重新校验。"""
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        file_path = os.path.join(self.root, "files", sha[:2], sha)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        if os.path.exists(file_path):
            with open(file_path, "rb") as existing:
                if hashlib.sha256(existing.read()).hexdigest() != sha:
                    raise ValueError("哈希路径原件完整性校验失败，已停止覆盖")
            return sha
        fd, temp_path = tempfile.mkstemp(prefix=".biaojing-original-",
                                         dir=os.path.dirname(file_path))
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            with open(temp_path, "rb") as check:
                if hashlib.sha256(check.read()).hexdigest() != sha:
                    raise ValueError("原件写入后 SHA-256 校验失败")
            os.replace(temp_path, file_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
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
        from . import docx_parser, pdf_parser, xlsx_parser
        extract_version = (
            pdf_parser.EXTRACTION_VERSION if doc_type == "pdf" else
            docx_parser.EXTRACTION_VERSION if doc_type == "docx" else
            xlsx_parser.EXTRACTION_VERSION if doc_type == "xlsx" else
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
                if e.get("formula") is not None:
                    cached = e.get("cached_value")
                    cached_text = ("未知（文件未提供缓存）"
                                   if cached in (None, "unknown")
                                   else str(cached))
                    quote = (f"公式：{e['formula']}\n缓存值：{cached_text}"
                             "（Excel 可能尚未重新计算）")
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
                self.conn.execute(
                    "INSERT OR IGNORE INTO evidence_versions"
                    " (evidence_id, version, quote, ocr_used, page_status,"
                    " page_error, page_note, extract_version, created_at)"
                    " VALUES(?,1,?,?,?,?,?,?,?)",
                    (e["evidence_id"], quote,
                     int(bool(e.get("ocr_used", False))), e.get("page_status"),
                     e.get("page_error"), e.get("page_note"), extract_version,
                     now))
            cands = candidates.extract_candidates(sha, doc_type, tagged)
            for c in cands:
                self.conn.execute(
                    "INSERT INTO candidates(sha256, field, value_json,"
                    " evidence_id, locator_json, locator_display, note,"
                    " companion_json, candidate_meta_json)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (sha, c["field"],
                     json.dumps(c["value"], ensure_ascii=False, default=str),
                     c["evidence_id"],
                     json.dumps(c["locator"], ensure_ascii=False),
                     c.get("locator_display", ""), c.get("note", ""),
                     json.dumps(c.get("companion"), ensure_ascii=False)
                     if c.get("companion") else None,
                     json.dumps({k: c[k] for k in ("currency_hint", "evidence_ids")
                                 if c.get(k) is not None}, ensure_ascii=False)
                     if c.get("currency_hint") is not None or c.get("evidence_ids")
                     else None))
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
                version = self.conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM evidence_versions"
                    " WHERE evidence_id=?", (ev["evidence_id"],)).fetchone()[0]
                self.conn.execute(
                    "INSERT INTO evidence_versions"
                    " (evidence_id,version,quote,ocr_used,page_status,page_error,"
                    " page_note,extract_version,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (ev["evidence_id"], version, ev.get("text", ""),
                     int(bool(ev.get("ocr_used"))), ev.get("page_status"),
                     ev.get("page_error"), ev.get("page_note"),
                     pdf_parser.EXTRACTION_VERSION, _now()))
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
                    " locator_json, locator_display, note, companion_json,"
                    " candidate_meta_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (sha256, c["field"],
                     json.dumps(c["value"], ensure_ascii=False, default=str),
                     c["evidence_id"], json.dumps(c["locator"], ensure_ascii=False),
                     c.get("locator_display", ""), c.get("note", ""),
                     json.dumps(c.get("companion"), ensure_ascii=False)
                     if c.get("companion") else None,
                     json.dumps({k: c[k] for k in ("currency_hint", "evidence_ids")
                                 if c.get(k) is not None}, ensure_ascii=False)
                     if c.get("currency_hint") is not None or c.get("evidence_ids")
                     else None))
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
                   source_type: str = "unknown", progress=None,
                   cancelled=None) -> list[dict]:
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
        entries = expanded["entries"]
        for index, entry in enumerate(entries, start=1):
            if cancelled and cancelled():
                results.append({"status": "cancelled", "ref": name,
                                "error": f"已处理 {index - 1}/{len(entries)} 个归档成员"})
                break
            if progress:
                progress(index, len(entries), "解析归档成员", "processing")
            results.append(self.ingest_bytes(
                f"{name}::{entry['name']}", entry["data"], source_type,
                progress=lambda page, total, stage, status, i=index: progress(
                    i, len(entries), f"{entry['name']}：{stage}", status)
                if progress else None,
                cancelled=cancelled))
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
        import hashlib

        path = os.path.join(self.root, "files", sha256[:2], sha256)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("原件 SHA-256 校验失败，拒绝下载或继续处理")
        return data

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
            "version": self.conn.execute(
                "SELECT MAX(version) FROM evidence_versions"
                " WHERE evidence_id=?", (evidence_id,)).fetchone()[0] or 1,
        }

    @_locked
    def export_last_screen(self) -> dict | None:
        """最近一次筛查的完整导出（D-04）：facts/outcome 以库内原始
        JSON 字符串原样返回，便于对 facts_sha256 逐字节现场重算复核。"""
        row = self.conn.execute(
            "SELECT id,run_at,rules_version,facts_sha256,facts_json,"
            "outcome_json,status,error FROM screen_runs"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        return {"id": row["id"], "run_at": row["run_at"],
                "rules_version": row["rules_version"],
                "facts_sha256": row["facts_sha256"],
                "facts_json": row["facts_json"],
                "outcome_json": row["outcome_json"],
                "status": row["status"], "error": row["error"]}

    def state(self, candidate_offset: int = 0,
              candidate_limit: int = 200, source_offset: int = 0,
              source_limit: int = 100) -> dict:
        """返回工作台摘要及一页来源和候选，避免大批次全量回传。"""
        if type(candidate_offset) is not int or candidate_offset < 0:
            raise ValueError("candidate_offset 须为非负整数")
        if type(candidate_limit) is not int or not 1 <= candidate_limit <= 500:
            raise ValueError("candidate_limit 须为 1 到 500")
        if type(source_offset) is not int or source_offset < 0:
            raise ValueError("source_offset 须为非负整数")
        if type(source_limit) is not int or not 1 <= source_limit <= 500:
            raise ValueError("source_limit 须为 1 到 500")
        cov = self.coverage()
        # 来源行：每次输入出现一行（含 duplicate/rejected），重复可见
        source_total = cov["total_input_occurrences"]
        source_rows = [dict(r) for r in self.conn.execute(
            "SELECT r.ref, r.status, r.reason, s.doc_type, s.parser,"
            " s.sha256, s.counts_json, s.extract_version, s.source_type"
            " FROM source_refs r LEFT JOIN sources s ON s.sha256 = r.sha256"
            " ORDER BY r.id LIMIT ? OFFSET ?", (source_limit, source_offset))]
        candidate_total = self.conn.execute(
            "SELECT COUNT(*) FROM candidates").fetchone()[0]
        cands = [dict(r) for r in self.conn.execute(
            "SELECT id, sha256, field, value_json, evidence_id,"
            " locator_display, note, companion_json, candidate_meta_json"
            " FROM candidates ORDER BY id LIMIT ? OFFSET ?",
            (candidate_limit, candidate_offset))]
        for c in cands:
            c["value"] = json.loads(c.pop("value_json"))
            comp = c.pop("companion_json")
            c["companion"] = json.loads(comp) if comp else None
            meta = c.pop("candidate_meta_json")
            c.update(json.loads(meta) if meta else {})
        confirmation_count = sum(self.conn.execute(
            "SELECT COUNT(*) FROM " + table + where).fetchone()[0]
            for table, where in (
                ("confirmations", " WHERE field NOT IN"
                 " ('contact_phone','contact_email','bank_account','person_manager',"
                 " 'person_tech','authorize_rep','legal_rep','id_number')"),
                ("contact_facts", " WHERE action IN ('confirm','correct','unknown')"),
                ("person_facts", " WHERE action IN ('confirm','correct','unknown')"),
            ))
        history_total = self.conn.execute(
            "SELECT COUNT(*) FROM confirmation_history").fetchone()[0]
        history = [dict(r) for r in self.conn.execute(
            "SELECT event_id, lot_id, bidder_id, field, old_value, new_value,"
            " old_evidence_id, evidence_id, action, original_candidate,"
            " person_id, evidence_version, actor, changed_at"
            " FROM confirmation_history ORDER BY id DESC LIMIT 100")]
        binding_history_total = self.conn.execute(
            "SELECT COUNT(*) FROM file_binding_history").fetchone()[0]
        binding_history = [dict(r) for r in self.conn.execute(
            "SELECT h.event_id,h.lot_id,h.bidder_id,h.sha256,h.old_value,"
            " h.new_value,h.changed_at,(SELECT e.evidence_id FROM evidence_store e"
            " WHERE e.sha256=h.sha256 ORDER BY e.ordinal LIMIT 1) AS evidence_id"
            " FROM file_binding_history h ORDER BY h.id DESC LIMIT 100")]
        last_findings = self.conn.execute(
            "SELECT findings_json FROM findings_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        findings = json.loads(last_findings["findings_json"]) \
            if last_findings else None
        last_run = self.conn.execute(
            "SELECT run_at,rules_version,facts_sha256,outcome_json,status,error"
            " FROM screen_runs ORDER BY id DESC LIMIT 1").fetchone()
        last_outcome = json.loads(last_run["outcome_json"]) if last_run else {}
        last_screen_run = ({
            "run_at": last_run["run_at"],
            "rules_version": last_run["rules_version"],
            "facts_sha256": last_run["facts_sha256"],
            "status": last_run["status"], "error": last_run["error"],
            "rule_statuses": last_outcome.get("rule_statuses", {}),
            "import": last_outcome.get("import"),
        } if last_run else {"status": "not_run"})
        return {
            "product": "标镜", "product_name": "标镜",
            "coverage": cov, "source_rows": source_rows,
            "source_total": source_total, "source_offset": source_offset,
            "source_limit": source_limit,
            "source_has_more": source_offset + len(source_rows) < source_total,
            "candidates": cands, "confirmation_count": confirmation_count,
            "candidate_total": candidate_total,
            "candidate_offset": candidate_offset,
            "candidate_limit": candidate_limit,
            "candidate_has_more": candidate_offset + len(cands) < candidate_total,
            "confirmation_history": history,
            "confirmation_history_total": history_total,
            "file_binding_history": binding_history,
            "file_binding_history_total": binding_history_total,
            "findings": findings,
            "last_screen_run": last_screen_run,
        }

    # ------------------------------------------------------------ 确认字段

    @_locked
    def confirm_field(self, event_id: str, lot_id: str, bidder_id: str,
                      field: str, value, evidence_id: str | None,
                      action: str = "confirm",
                      original_candidate: str | None = None,
                      source_role: str = "unknown",
                      person_id: str | None = None,
                      reviewer_type: str | None = None) -> dict:
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
        if person_id is not None and (not isinstance(person_id, str)
                                      or len(person_id.strip()) > 128):
            return {"ok": False, "error": "person_id 必须是 128 字以内的字符串"}
        if action in ("confirm", "correct"):
            if evidence_id is None or not self.evidence_usable(evidence_id):
                return {"ok": False,
                        "error": f"证据 {evidence_id!r} 缺失或定位无效："
                                 "缺证据不得确认参与规则的字段"}
        if action == "unknown":
            value = "unknown"
            evidence_id = None
        if field == "total_price":
            return {"ok": False,
                    "error": "总报价须通过原子金额确认接口，同时确认单位、币种和税口径"}
        if field == "tax_included" and action in ("confirm", "correct") \
                and not isinstance(value, bool):
            return {"ok": False,
                    "error": "tax_included 只接受布尔值（unknown 用 unknown"
                             " 操作表达）"}
        if field == "price_lines" and action in ("confirm", "correct"):
            if not isinstance(value, list) or not value:
                return {"ok": False, "error": "清单报价须为非空行列表"}
            from .money import parse_amount
            for index, line in enumerate(value, start=1):
                if not isinstance(line, dict):
                    return {"ok": False, "error": f"第 {index} 条清单行格式错误"}
                line_evidence = line.get("evidence")
                if not self.evidence_usable(line_evidence):
                    return {"ok": False,
                            "error": f"第 {index} 条清单行缺少有效单价证据"}
                price = line.get("unit_price")
                if price is not None:
                    # 精度边界（复核六轮）：SQLite bid_price_lines.unit_price
                    # 为 REAL。字符串单价仅当 float 最短 repr 能精确还原原
                    # 十进制（Decimal(str(float(d))) == d，如 0.29、100）才
                    # 接受；逐值判定、非位数上限——更长但可精确往返的值
                    # 同样通过。超出范围显式拒绝，绝不静默舍入。
                    from decimal import Decimal as _decimal
                    try:
                        as_decimal = _decimal(str(price).strip())
                        round_trip = _decimal(str(float(as_decimal)))
                    except (ValueError, ArithmeticError):
                        # InvalidOperation 属 ArithmeticError：'abc' 等接口
                        # 信任边界上的非法字符串走结构化失败而非 500
                        # （复核七轮）
                        return {"ok": False,
                                "error": (f"第 {index} 条单价 {price!r} "
                                          "不是有效十进制数")}
                    if round_trip != as_decimal:
                        return {"ok": False,
                                "error": (f"第 {index} 条单价 {price!r} 无法"
                                          "以浮点精确往返表示（逐值校验，"
                                          "非位数上限），为避免静默舍入已"
                                          "拒绝；请按原件核对金额后重新确认")}
                    if parse_amount(str(price), "yuan")["status"] != "normalized":
                        return {"ok": False, "error": f"第 {index} 条单价不是有限金额"}
                tax = line.get("tax_included")
                if tax is not None and type(tax) is not bool:
                    return {"ok": False, "error": f"第 {index} 条税口径只能为布尔值或 unknown"}
                field_evidence = line.get("field_evidence") or {}
                if not isinstance(field_evidence, dict) or any(
                        not self.evidence_usable(eid)
                        for eid in field_evidence.values() if eid):
                    return {"ok": False, "error": f"第 {index} 条清单行字段证据无效"}
        if field == "outcome_result" and action in ("confirm", "correct"):
            text = str(value).strip().casefold()
            if any(x in text for x in ("未中标", "未中", "落标", "未获得", "否", "lost")):
                value = "lost"
            elif any(x in text for x in ("中标", "已中标", "是", "won")):
                value = "won"
            else:
                return {"ok": False, "error": "中标结果须明确为中标或未中标"}
        if reviewer_type not in (None, "", "agent", "test"):
            # D-03（任务口径）：机器代理与人工复核写入必须可区分；
            # 未知归属拒绝而非静默当人工（静默会误记操作者）
            return {"ok": False,
                    "error": "reviewer_type 只接受 agent/test（缺省为人工）"}
        actor = {"agent": "agent", "test": "test"}.get(reviewer_type) \
            or "本机用户"
        if field in ("contact_phone", "contact_email", "bank_account"):
            self._save_contact(event_id, lot_id, bidder_id, field, value,
                               evidence_id, source_role, original_candidate,
                               action, actor=actor)
        elif field in ("person_manager", "person_tech", "authorize_rep",
                       "legal_rep", "id_number"):
            self._save_person(event_id, lot_id, bidder_id, field, value,
                              evidence_id, person_id, original_candidate,
                              action, actor=actor)
        else:
            with self.conn:
                self._write_confirmation(
                    event_id, lot_id, bidder_id, field, value, evidence_id,
                    source_role, original_candidate, action, person_id,
                    actor=actor)
        return {"ok": True}

    def _write_confirmation(self, event_id, lot_id, bidder_id, field, value,
                            evidence_id, source_role, original_candidate,
                            action, person_id=None, actor="本机用户"):
        """在调用方事务中追加历史并更新当前确认值。"""
        old = self.conn.execute(
            "SELECT value, evidence_id FROM confirmations"
            " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?",
            (event_id, lot_id, bidder_id, field)).fetchone()
        version = None
        if evidence_id:
            version_row = self.conn.execute(
                "SELECT MAX(version) FROM evidence_versions WHERE evidence_id=?",
                (evidence_id,)).fetchone()
            version = version_row[0] if version_row else None
        encoded = (json.dumps(str(value), ensure_ascii=False)
                   if field == "total_price" else
                   value if isinstance(value, str) else
                   json.dumps(value, ensure_ascii=False, default=str))
        now = _now()
        self.conn.execute(
            "INSERT INTO confirmation_history"
            " (event_id,lot_id,bidder_id,field,old_value,new_value,"
            " old_evidence_id,evidence_id,action,original_candidate,person_id,"
            " evidence_version,actor,changed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, lot_id, bidder_id, field,
             old["value"] if old else None, encoded,
             old["evidence_id"] if old else None, evidence_id, action,
             original_candidate, person_id, version, actor, now))
        self.conn.execute(
            "INSERT INTO confirmations(event_id,lot_id,bidder_id,field,value,"
            " evidence_id,source_role,original_candidate,action,confirmed_at,"
            " person_id,evidence_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(event_id,lot_id,bidder_id,field) DO UPDATE SET"
            " value=excluded.value,evidence_id=excluded.evidence_id,"
            " source_role=excluded.source_role,"
            " original_candidate=excluded.original_candidate,"
            " action=excluded.action,confirmed_at=excluded.confirmed_at,"
            " person_id=excluded.person_id,evidence_version=excluded.evidence_version",
            (event_id, lot_id, bidder_id, field, encoded, evidence_id,
             source_role, original_candidate, action, now, person_id, version))

    def _save_contact(self, event_id, lot_id, bidder_id, field, value,
                      evidence_id, source_role, original_candidate, action,
                      actor="本机用户"):
        """按来源证据分别保存联系方式，避免后一个号码覆盖前一个。"""
        old = self.conn.execute(
            "SELECT value,evidence_id FROM contact_facts"
            " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?"
            " AND evidence_id IS ? ORDER BY id DESC LIMIT 1",
            (event_id, lot_id, bidder_id, field, evidence_id)).fetchone()
        now = _now()
        with self.conn:
            if action == "unknown":
                active = [dict(row) for row in self.conn.execute(
                    "SELECT value,evidence_id FROM contact_facts"
                    " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?"
                    " AND action IN ('confirm','correct')",
                    (event_id, lot_id, bidder_id, field))]
                self.conn.execute(
                    "INSERT INTO confirmation_history"
                    " (event_id,lot_id,bidder_id,field,old_value,new_value,"
                    " evidence_id,action,original_candidate,actor,changed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, field,
                     json.dumps(active, ensure_ascii=False), '"unknown"', None,
                     action, original_candidate, actor, now))
                self.conn.execute(
                    "UPDATE contact_facts SET action='superseded'"
                    " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?"
                    " AND action IN ('confirm','correct','unknown')",
                    (event_id, lot_id, bidder_id, field))
                current = self.conn.execute(
                    "SELECT id FROM contact_facts WHERE event_id=? AND lot_id=?"
                    " AND bidder_id=? AND field=? AND evidence_id IS NULL"
                    " ORDER BY id DESC LIMIT 1",
                    (event_id, lot_id, bidder_id, field)).fetchone()
                if current:
                    self.conn.execute(
                        "UPDATE contact_facts SET value='unknown',source_role='unknown',"
                        " original_candidate=?,action='unknown',confirmed_at=?"
                        " WHERE id=?",
                        (original_candidate, now, current["id"]))
                else:
                    self.conn.execute(
                        "INSERT INTO contact_facts"
                        " (event_id,lot_id,bidder_id,field,value,evidence_id,source_role,"
                        " original_candidate,action,confirmed_at)"
                        " VALUES(?,?,?,?,?,NULL,'unknown',?,'unknown',?)",
                        (event_id, lot_id, bidder_id, field, "unknown",
                         original_candidate, now))
                return
            self.conn.execute(
                "UPDATE contact_facts SET action='superseded'"
                " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?"
                " AND evidence_id IS NULL AND action='unknown'",
                (event_id, lot_id, bidder_id, field))
            self.conn.execute(
                "INSERT INTO confirmation_history"
                " (event_id,lot_id,bidder_id,field,old_value,new_value,"
                " old_evidence_id,evidence_id,action,original_candidate,actor,changed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, lot_id, bidder_id, field,
                 old["value"] if old else None,
                 value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                 old["evidence_id"] if old else None, evidence_id, action,
                 original_candidate, actor, now))
            self.conn.execute(
                "INSERT INTO contact_facts"
                " (event_id,lot_id,bidder_id,field,value,evidence_id,source_role,"
                " original_candidate,action,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_id,lot_id,bidder_id,field,evidence_id)"
                " DO UPDATE SET value=excluded.value,source_role=excluded.source_role,"
                " original_candidate=excluded.original_candidate,"
                " action=excluded.action,confirmed_at=excluded.confirmed_at",
                (event_id, lot_id, bidder_id, field,
                 value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                 evidence_id, source_role, original_candidate, action, now))

    def _save_person(self, event_id, lot_id, bidder_id, field, value,
                     evidence_id, person_id, original_candidate, action,
                     actor="本机用户"):
        """保存多名人员及独立人工身份 ID；同名不会自动合并。"""
        now = _now()
        encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        pid = person_id.strip() if isinstance(person_id, str) and person_id.strip() else None
        version = None
        if evidence_id:
            row = self.conn.execute(
                "SELECT MAX(version) FROM evidence_versions WHERE evidence_id=?",
                (evidence_id,)).fetchone()
            version = row[0] if row else None
        with self.conn:
            if action == "unknown":
                active = [dict(row) for row in self.conn.execute(
                    "SELECT value,evidence_id,person_id FROM person_facts"
                    " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?"
                    " AND action IN ('confirm','correct')",
                    (event_id, lot_id, bidder_id, field))]
                self.conn.execute(
                    "INSERT INTO confirmation_history"
                    " (event_id,lot_id,bidder_id,field,old_value,new_value,"
                    " action,original_candidate,actor,changed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, field,
                     json.dumps(active, ensure_ascii=False), '"unknown"',
                     action, original_candidate, actor, now))
                self.conn.execute(
                    "UPDATE person_facts SET action='superseded'"
                    " WHERE event_id=? AND lot_id=? AND bidder_id=? AND field=?"
                    " AND action IN ('confirm','correct','unknown')",
                    (event_id, lot_id, bidder_id, field))
                current = self.conn.execute(
                    "SELECT id FROM person_facts WHERE event_id=? AND lot_id=?"
                    " AND bidder_id=? AND field=? AND evidence_id IS NULL"
                    " AND person_id IS NULL ORDER BY id DESC LIMIT 1",
                    (event_id, lot_id, bidder_id, field)).fetchone()
                if current:
                    self.conn.execute(
                        "UPDATE person_facts SET value='unknown',action='unknown',"
                        " original_candidate=?,confirmed_at=? WHERE id=?",
                        (original_candidate, now, current["id"]))
                else:
                    self.conn.execute(
                        "INSERT INTO person_facts"
                        " (event_id,lot_id,bidder_id,field,value,evidence_id,person_id,"
                        " original_candidate,action,evidence_version,confirmed_at)"
                        " VALUES(?,?,?,?,?,NULL,NULL,?,'unknown',NULL,?)",
                        (event_id, lot_id, bidder_id, field, '"unknown"',
                         original_candidate, now))
                return
            previous = self.conn.execute(
                "SELECT id,value,evidence_id FROM person_facts WHERE event_id=?"
                " AND lot_id=? AND bidder_id=? AND field=? AND evidence_id IS ?"
                " AND person_id IS ? ORDER BY id DESC LIMIT 1",
                (event_id, lot_id, bidder_id, field, evidence_id, pid)).fetchone()
            self.conn.execute(
                "INSERT INTO confirmation_history"
                " (event_id,lot_id,bidder_id,field,old_value,new_value,"
                " old_evidence_id,evidence_id,action,original_candidate,person_id,"
                " evidence_version,actor,changed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, lot_id, bidder_id, field,
                 previous["value"] if previous else None, encoded,
                 previous["evidence_id"] if previous else None, evidence_id,
                 action, original_candidate, pid, version, actor, now))
            if previous:
                self.conn.execute(
                    "UPDATE person_facts SET value=?,original_candidate=?,action=?,"
                    " evidence_version=?,confirmed_at=? WHERE id=?",
                    (encoded, original_candidate, action, version, now,
                     previous["id"]))
            else:
                self.conn.execute(
                    "INSERT INTO person_facts"
                    " (event_id,lot_id,bidder_id,field,value,evidence_id,person_id,"
                    " original_candidate,action,evidence_version,confirmed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, field, encoded, evidence_id,
                     pid, original_candidate, action, version, now))

    def confirm_amount(self, event_id: str, lot_id: str, bidder_id: str,
                       raw_value, unit: str, currency: str,
                       tax_included, evidence_id: str | None,
                       action: str = "confirm",
                       original_candidate: str | None = None,
                       reviewer_type: str | None = None) -> dict:
        """金额、来源单位、币种和税口径在同一 SQLite 事务中确认。"""
        from .money import normalize_confirmed_amount

        if not all(isinstance(x, str) and x.strip()
                   for x in (event_id, lot_id, bidder_id)):
            return {"ok": False, "error": "event_id/lot_id/bidder_id 必填"}
        if action not in ("confirm", "correct", "unknown"):
            return {"ok": False, "error": "金额操作只接受 confirm/correct/unknown"}
        if reviewer_type not in (None, "", "agent", "test"):
            return {"ok": False,
                    "error": "reviewer_type 只接受 agent/test（缺省为人工）"}
        actor = {"agent": "agent", "test": "test"}.get(reviewer_type) \
            or "本机用户"
        if action == "unknown":
            values = (("total_price", "unknown"), ("amount_unit", "unknown"),
                      ("currency", "unknown"), ("tax_included", "unknown"))
            evidence_id = None
        else:
            if evidence_id is None or not self.evidence_usable(evidence_id):
                return {"ok": False, "error": "总报价缺少可回指的有效证据"}
            currency = str(currency or "unknown").strip().upper()
            if currency == "USD":
                return {"ok": False,
                        "error": "当前版本不做外币汇率换算，请核实后标为 unknown"}
            if currency not in ("CNY", "UNKNOWN"):
                return {"ok": False, "error": "当前版本只支持人民币金额筛查"}
            try:
                amount = normalize_confirmed_amount(raw_value, unit)
            except (ValueError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
            currency = currency.lower() if currency == "UNKNOWN" else currency
            if not (type(tax_included) is bool
                    or tax_included in ("unknown", None)):
                return {"ok": False, "error": "税口径须为含税、不含税或 unknown"}
            tax_value = "unknown" if tax_included in ("unknown", None) else tax_included
            values = (("total_price", amount), ("amount_unit", unit),
                      ("currency", currency), ("tax_included", tax_value))
        if original_candidate is not None and not isinstance(original_candidate, str):
            return {"ok": False, "error": "original_candidate 必须是字符串"}
        with self.conn:
            for field, value in values:
                self._write_confirmation(
                    event_id, lot_id, bidder_id, field, value, evidence_id,
                    "unknown", original_candidate, action, actor=actor)
        return {"ok": True}

    @_locked
    def bind_file(self, event_id: str, lot_id: str, bidder_id: str,
                  sha256: str, context: str, source_type: str,
                  declared_owner_id: str | None = None,
                  declared_uscc: str | None = None) -> dict:
        """人工绑定来源文件至投标人，并保留文件属性供 R001/R006 复核。"""
        allowed_contexts = {
            "bid_document", "tenderer_document", "legal_performance",
            "joint_venture_reference", "other",
        }
        if not all(isinstance(x, str) and x.strip()
                   for x in (event_id, lot_id, bidder_id)):
            return {"ok": False, "error": "event_id/lot_id/bidder_id 必填"}
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            return {"ok": False, "error": "文件 SHA-256 格式错误"}
        if context not in allowed_contexts:
            return {"ok": False, "error": "文件用途不在允许范围内"}
        if not isinstance(source_type, str) or not source_type.strip() \
                or len(source_type) > 64:
            return {"ok": False, "error": "来源类型必填且不得超过 64 字"}
        evidence = self.conn.execute(
            "SELECT evidence_id FROM evidence_store WHERE sha256=?"
            " ORDER BY ordinal LIMIT 1", (sha256,)).fetchone()
        if evidence is None or not self.evidence_usable(evidence["evidence_id"]):
            return {"ok": False, "error": "该文件没有可回指的有效证据"}
        owner = str(declared_owner_id).strip() if declared_owner_id else None
        uscc = str(declared_uscc).strip() if declared_uscc else None
        now = _now()
        old = self.conn.execute(
            "SELECT context,source_type,declared_owner_id,declared_uscc"
            " FROM file_bindings WHERE event_id=? AND lot_id=? AND bidder_id=?"
            " AND sha256=?",
            (event_id, lot_id, bidder_id, sha256)).fetchone()
        old_value = dict(old) if old else None
        new_value = {"context": context, "source_type": source_type.strip(),
                     "declared_owner_id": owner, "declared_uscc": uscc}
        if old_value == new_value:
            return {"ok": True, "unchanged": True}
        with self.conn:
            self.conn.execute(
                "INSERT INTO file_binding_history"
                " (event_id,lot_id,bidder_id,sha256,old_value,new_value,changed_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (event_id, lot_id, bidder_id, sha256,
                 json.dumps(old_value, ensure_ascii=False) if old_value else None,
                 json.dumps(new_value, ensure_ascii=False), now))
            self.conn.execute(
                "INSERT INTO file_bindings"
                " (event_id,lot_id,bidder_id,sha256,context,source_type,"
                " declared_owner_id,declared_uscc,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_id,lot_id,bidder_id,sha256) DO UPDATE SET"
                " context=excluded.context,source_type=excluded.source_type,"
                " declared_owner_id=excluded.declared_owner_id,"
                " declared_uscc=excluded.declared_uscc,updated_at=excluded.updated_at",
                (event_id, lot_id, bidder_id, sha256, context,
                 source_type.strip(), owner, uscc, now, now))
        return {"ok": True}

    # ------------------------------------------------------------ 组装与筛查

    @_locked
    def build_p2_facts(self) -> dict:
        """从确认字段组装 P2 schema 归一化事实（含证据注册表）。"""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM confirmations WHERE field NOT IN"
            " ('contact_phone','contact_email','bank_account','person_manager',"
            " 'person_tech','authorize_rep','legal_rep','id_number') ORDER BY id")]
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

        person_rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM person_facts WHERE action IN ('confirm','correct')"
            " ORDER BY id")]
        person_ids = {}
        for r in person_rows:
            if r["field"] == "id_number" and r.get("person_id"):
                id_number = val(r["value"])
                if id_number != "unknown":
                    person_ids[(r["event_id"], r["lot_id"], r["bidder_id"],
                                r["person_id"])] = id_number

        def get_bid(event_id, lot_id, bidder_id):
            event = events.setdefault(event_id, {
                "event_id": event_id, "project_id": event_id,
                "event_name": event_id, "lots": {}})
            lot = event["lots"].setdefault(lot_id, {
                "lot_id": lot_id, "lot_name": lot_id, "bids": {}})
            return lot["bids"].setdefault(bidder_id, {
                "bidder_id": bidder_id, "bidder_name": bidder_id,
                "role": "bidder", "outcome": {"status": "unknown"}})

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
                                  "result": str(value) if st == "known" else None,
                                  **({"evidence": ev_id} if ev_id else {})}
            elif field == "price_lines" and isinstance(value, list):
                # 共享根因修复（复核五/六轮）：确认值里的字符串单价在此
                # 归一为 float（仅接受确认入口已验证可精确往返的值），
                # 否则事件导入按"unit_price 非数值"整事件拒绝；同时把
                # 每一行的单价证据与字段证据注册进 facts 证据注册表——
                # 只登记确认行顶层证据曾让第 2 行起全部"证据缺失/无效"。
                from decimal import Decimal as _decimal
                normalized_lines = []
                for line in value:
                    if isinstance(line, dict) and isinstance(
                            line.get("unit_price"), str):
                        try:
                            as_decimal = _decimal(line["unit_price"].strip())
                            as_float = float(as_decimal)
                        except (ValueError, ArithmeticError):
                            as_float = None
                        if as_float is not None \
                                and _decimal(str(as_float)) == as_decimal:
                            line = {**line, "unit_price": as_float}
                    normalized_lines.append(line)
                for line in normalized_lines:
                    ev_json(line.get("evidence"))
                    for field_eid in (line.get("field_evidence")
                                      or {}).values():
                        if field_eid:
                            ev_json(field_eid)
                bid.setdefault("price_lines", []).extend(normalized_lines)
            # 其余字段（project_name/code、lot_name 等）当前仅
            # 留档展示，不映射进 P2 规则事实

        for r in person_rows:
            if r["field"] not in ("person_manager", "person_tech",
                                  "authorize_rep", "legal_rep"):
                continue
            value = val(r["value"])
            if value == "unknown":
                continue
            bid = get_bid(r["event_id"], r["lot_id"], r["bidder_id"])
            ev_id = ev_json(r["evidence_id"])
            pid = r.get("person_id") or (
                f"unverified:{r['bidder_id']}:{r['field']}:{ev_id or value}")
            bid.setdefault("persons", []).append({
                "person_id": pid, "name": str(value),
                "id_number": str(person_ids.get((r["event_id"], r["lot_id"],
                                                  r["bidder_id"], pid), "unknown")),
                "role": r["field"], "evidence": ev_id})
        for r in person_rows:
            if r["field"] != "id_number" or not r.get("person_id"):
                continue
            value = val(r["value"])
            bid = get_bid(r["event_id"], r["lot_id"], r["bidder_id"])
            if not any(p["person_id"] == r["person_id"]
                       for p in bid.get("persons", [])):
                bid.setdefault("persons", []).append({
                    "person_id": r["person_id"], "name": "unknown",
                    "id_number": str(value), "role": "identity_reference",
                    "evidence": ev_json(r["evidence_id"])})

        # 多值联系方式按每条证据独立保留；重复确认一条来源会更新该条，不覆盖同主体的其他号码。
        for r in self.conn.execute("SELECT * FROM contact_facts ORDER BY id"):
            if r["action"] not in ("confirm", "correct") or r["value"] == "unknown":
                continue
            bid = get_bid(r["event_id"], r["lot_id"], r["bidder_id"])
            kind = {"contact_phone": "phone", "contact_email": "email",
                    "bank_account": "account"}[r["field"]]
            bid.setdefault("contacts", []).append({
                "kind": kind, "value": r["value"],
                "source_role": r["source_role"] or "unknown",
                "evidence": ev_json(r["evidence_id"]),
            })

        # 文件必须先由人工绑定到事件/标段/投标人，才进入主体混用和元数据筛查。
        for binding in self.conn.execute("SELECT * FROM file_bindings ORDER BY id"):
            bid = get_bid(binding["event_id"], binding["lot_id"],
                          binding["bidder_id"])
            src = self.conn.execute(
                "SELECT first_ref,metadata_json FROM sources WHERE sha256=?",
                (binding["sha256"],)).fetchone()
            evrow = self.conn.execute(
                "SELECT evidence_id FROM evidence_store WHERE sha256=?"
                " ORDER BY ordinal LIMIT 1", (binding["sha256"],)).fetchone()
            if src is None or evrow is None:
                continue
            try:
                metadata = json.loads(src["metadata_json"] or "{}")
            except ValueError:
                metadata = {}
            metadata = {
                "producer": metadata.get("producer") or "unknown",
                "creation_date": (metadata.get("creation_date")
                                  or metadata.get("creationDate")
                                  or metadata.get("created") or "unknown"),
            }
            bid.setdefault("files", []).append({
                "file_ref": self.display_name_for(binding["sha256"]) or src["first_ref"],
                "sha256": binding["sha256"],
                "source_type": binding["source_type"],
                "context": binding["context"],
                "declared_owner_id": binding["declared_owner_id"],
                "declared_uscc": binding["declared_uscc"],
                "metadata": metadata,
                "evidence": ev_json(evrow["evidence_id"]),
            })

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
        """运行规则并追加不可变事实快照、规则状态与结果。"""
        import hashlib

        facts = self.build_p2_facts()
        facts_json = json.dumps(facts, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"))
        facts_sha256 = hashlib.sha256(facts_json.encode("utf-8")).hexdigest()
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
        result = {**outcome,
                  "import": {**import_result.as_dict(),
                             "event_count_after": event_count}}
        with self.conn:
            self.conn.execute(
                "INSERT INTO findings_log(run_at, findings_json) VALUES(?,?)",
                (now, json.dumps(outcome["findings"], ensure_ascii=False)))
            self.conn.execute(
                "INSERT INTO screen_runs(run_at,rules_version,facts_sha256,"
                "facts_json,outcome_json,status,error) VALUES(?,?,?,?,?,'completed',NULL)",
                (now, RULE_VERSION, facts_sha256, facts_json,
                 json.dumps(result, ensure_ascii=False, default=str)))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES('last_screen_at', ?)", (now,))
        return result

    @_locked
    def record_screen_failure(self, error: str) -> None:
        """保留筛查失败状态，避免旧结果被误认为本次成功结果。"""
        import hashlib

        now = _now()
        facts_json = "{}"
        outcome = {"run_status": "failed", "rule_statuses": {},
                   "error": str(error)[:1000]}
        with self.conn:
            self.conn.execute(
                "INSERT INTO screen_runs(run_at,rules_version,facts_sha256,"
                "facts_json,outcome_json,status,error) VALUES(?,?,?,?,?,'failed',?)",
                (now, RULE_VERSION,
                 hashlib.sha256(facts_json.encode("utf-8")).hexdigest(),
                 facts_json, json.dumps(outcome, ensure_ascii=False),
                 str(error)[:1000]))

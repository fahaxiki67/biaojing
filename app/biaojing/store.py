# -*- coding: utf-8 -*-
"""标镜 P2 SQLite 事件累计：幂等导入、冲突拒绝、全量读回。

口径（任务书）：
  - 同一 event_id 重复导入必须幂等：同内容跳过，不同内容记冲突并**拒绝
    该事件**（保留既有数据），绝不静默覆盖。
  - lot_id / bidder_id 只在其事件/标段作用域内唯一；所有标段级子表主键
    均以 (event_id, lot_id, …) 开头，不同事件的同名牌段互不覆盖。
  - 文件 SHA 相同只表示字节相同，不表示事件相同；同一文件可在多事件/多
    主体下各占一行（保留各自来源引用）。
  - 同一事件的新文件版本（不同 SHA）不增加事件计数——事件计数始终按
    去重后的 event_id 数。
  - evidence 表是证据注册表：evidence_id 全局唯一，同 ID 不同内容为冲突。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from collections import defaultdict

from .rules import FactBase, SCHEMA

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects(
    project_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS events(
    event_id TEXT PRIMARY KEY, project_id TEXT, event_name TEXT,
    content_hash TEXT NOT NULL, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lots(
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, lot_name TEXT,
    PRIMARY KEY(event_id, lot_id));
CREATE TABLE IF NOT EXISTS bidders(
    bidder_id TEXT PRIMARY KEY, display_name TEXT);
CREATE TABLE IF NOT EXISTS bids(
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    role TEXT, uscc TEXT, uscc_evidence TEXT,
    total_price REAL, currency TEXT, tax_included INTEGER,
    total_price_evidence TEXT,
    outcome_status TEXT, outcome_result TEXT,
    bid_evidence TEXT, bid_hash TEXT NOT NULL,
    PRIMARY KEY(event_id, lot_id, bidder_id));
CREATE TABLE IF NOT EXISTS bid_contacts(
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    kind TEXT, value TEXT, source_role TEXT, evidence_id TEXT,
    PRIMARY KEY(event_id, lot_id, bidder_id, kind, value, source_role,
                evidence_id));
CREATE TABLE IF NOT EXISTS bid_persons(
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    person_id TEXT, name TEXT, id_number TEXT, role TEXT, evidence_id TEXT,
    PRIMARY KEY(event_id, lot_id, bidder_id, person_id, evidence_id));
CREATE TABLE IF NOT EXISTS bid_price_lines(
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    seq INTEGER NOT NULL, item_code TEXT, item_name TEXT, spec TEXT,
    unit TEXT, qty REAL, unit_price REAL, currency TEXT,
    tax_included INTEGER, evidence_id TEXT,
    PRIMARY KEY(event_id, lot_id, bidder_id, seq));
CREATE TABLE IF NOT EXISTS bid_files(
    event_id TEXT NOT NULL, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL, file_ref TEXT, source_type TEXT,
    context TEXT, declared_owner_id TEXT, declared_uscc TEXT,
    metadata_json TEXT, evidence_id TEXT,
    PRIMARY KEY(event_id, lot_id, bidder_id, file_sha256, file_ref));
CREATE TABLE IF NOT EXISTS evidence(
    evidence_id TEXT PRIMARY KEY, file_sha256 TEXT, source_type TEXT,
    role TEXT, quote TEXT, locator_json TEXT);
CREATE TABLE IF NOT EXISTS conflicts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, key TEXT NOT NULL, detail TEXT NOT NULL,
    detected_at TEXT NOT NULL);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA_SQL)
    return conn


def _stable_hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


class ImportResult:
    def __init__(self):
        self.events_inserted = 0
        self.events_skipped_idempotent = 0
        self.conflicts = []  # [{kind, key, detail}]

    def as_dict(self) -> dict:
        return {
            "events_inserted": self.events_inserted,
            "events_skipped_idempotent": self.events_skipped_idempotent,
            "conflicts": self.conflicts,
            "total_conflicts": len(self.conflicts),
        }

    @property
    def total_conflicts(self) -> int:
        return len(self.conflicts)


def _record_conflict(conn: sqlite3.Connection, kind: str, key: str,
                     detail: str, result: ImportResult, now: str) -> None:
    conn.execute(
        "INSERT INTO conflicts(kind, key, detail, detected_at) VALUES(?,?,?,?)",
        (kind, key, detail, now))
    result.conflicts.append({"kind": kind, "key": key, "detail": detail})


def _iter_event_evidence_ids(ev: dict):
    """收集事件内引用的全部 evidence_id（含嵌套子记录）。"""
    for b in _iter_event_bids(ev):
        for key in ("evidence", "total_price_evidence", "uscc_evidence"):
            if b.get(key):
                yield b[key]
        for c in b.get("contacts", []):
            if c.get("evidence"):
                yield c["evidence"]
        for p in b.get("persons", []):
            if p.get("evidence"):
                yield p["evidence"]
        for line in b.get("price_lines", []):
            if line.get("evidence"):
                yield line["evidence"]
        for f in b.get("files", []):
            if f.get("evidence"):
                yield f["evidence"]


def _iter_event_bids(ev: dict):
    for lot in ev.get("lots", []):
        for b in lot.get("bids", []):
            yield b


def _validate_event_children(ev: dict) -> None:
    """子记录严格校验：无效子记录抛 ValueError，由调用方整事件回滚。

    （unknown 语义用字符串 "unknown" 表达；None 表示未归一化，拒绝。）
    """
    # 与直接 FactBase / P3 筛查共用同一金额和税口径校验。
    FactBase.from_dict({"schema": SCHEMA, "events": [ev], "evidence": []})
    for lot in ev.get("lots", []):
        for b in lot.get("bids", []):
            for c in b.get("contacts", []):
                if c.get("value") is None:
                    raise ValueError(
                        f"联系人 {c.get('kind')} 的 value 为 None（未归一化），"
                        "应显式为 'unknown' 或具体值")
            for p in b.get("persons", []):
                if not p.get("person_id"):
                    raise ValueError("person 记录缺少 person_id（稳定 ID 必填）")
            for line in b.get("price_lines", []):
                up = line.get("unit_price")
                if up is not None and not isinstance(up, (int, float)):
                    raise ValueError(
                        f"清单行 {line.get('item_code')} 的 unit_price 非数值：{up!r}")


def _tax_from_db(value):
    if value is None:
        return None
    if type(value) is int and value in (0, 1):
        return value == 1
    raise ValueError(f"SQLite tax_included 存在非法值：{value!r}")


def import_batch(data: dict, conn: sqlite3.Connection) -> ImportResult:
    """把归一化事实 JSON 幂等导入 SQLite。

    原子性：每事件一个 SAVEPOINT，任何子记录无效（抛 ValueError/异常）
    即整事件回滚，不留下 event 与部分子行。
    证据冲突：同 evidence_id 内容与库内不一致时，旧证据不覆盖，且**引用
    该证据的事件整事件拒绝导入**（否则读回时证据指向会被错配）。
    """
    if data.get("schema") != SCHEMA:
        raise ValueError(f"schema 须为 {SCHEMA}，收到 {data.get('schema')!r}")
    result = ImportResult()
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    # Phase 1：证据注册表。同 ID 不同内容 → 记冲突、保留旧值，并记录冲突
    # 集合供事件导入阶段拒绝引用方。
    conflicted_evidence: set[str] = set()
    for ev_raw in data.get("evidence", []):
        eid = ev_raw.get("evidence_id")
        if not eid:
            continue
        row = (ev_raw.get("file_sha256", "unknown"),
               ev_raw.get("source_type", "unknown"),
               ev_raw.get("role", "unknown"),
               ev_raw.get("quote", ""),
               json.dumps(ev_raw.get("locator", {}), ensure_ascii=False))
        existing = conn.execute(
            "SELECT file_sha256, source_type, role, quote, locator_json"
            " FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?)", (eid, *row))
        elif tuple(existing) != row:
            conflicted_evidence.add(eid)
            if not any(c["kind"] == "evidence_id_conflict" and c["key"] == eid
                       for c in result.conflicts):
                _record_conflict(
                    conn, "evidence_id_conflict", eid,
                    f"同 evidence_id 内容不一致：库内保留 {existing}，"
                    f"拒绝写入 {row}（引用它的本批事件将被整体拒绝）",
                    result, now)

    # Phase 2：事件（每事件 SAVEPOINT 原子导入）
    for ev in data.get("events", []):
        event_id = ev.get("event_id")
        if not event_id:
            _record_conflict(conn, "missing_event_id", "<无>",
                             "事件缺少 event_id，拒绝导入", result, now)
            continue
        content_hash = _stable_hash(ev)
        existing = conn.execute(
            "SELECT content_hash FROM events WHERE event_id=?",
            (event_id,)).fetchone()
        if existing is not None:
            if existing[0] == content_hash:
                result.events_skipped_idempotent += 1
                continue
            _record_conflict(
                conn, "event_id_conflict", event_id,
                f"同 event_id 内容不同（既有 hash {existing[0][:12]}…，"
                f"新 hash {content_hash[:12]}…），拒绝覆盖，保留既有数据",
                result, now)
            continue
        bad_refs = sorted({e for e in set(_iter_event_evidence_ids(ev))
                           if e in conflicted_evidence})
        if bad_refs:
            _record_conflict(
                conn, "event_rejected_stale_evidence", event_id,
                f"事件引用了内容冲突的证据 {bad_refs}，整事件拒绝导入"
                "（保留库内既有证据与数据）",
                result, now)
            continue
        sp = "sp_" + content_hash[:16]
        conn.execute(f"SAVEPOINT {sp}")
        try:
            _validate_event_children(ev)
            _insert_event(conn, ev, now, content_hash, result)
        except Exception as exc:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            _record_conflict(
                conn, "event_import_error", event_id,
                f"子记录无效，整事件回滚（不留部分行）："
                f"{type(exc).__name__}: {exc}",
                result, now)
            continue
        conn.execute(f"RELEASE {sp}")
        result.events_inserted += 1
    conn.commit()
    return result


def _insert_event(conn: sqlite3.Connection, ev: dict, now: str,
                  content_hash: str, result: ImportResult) -> None:
    event_id = ev["event_id"]
    conn.execute(
        "INSERT INTO events VALUES(?,?,?,?,?)",
        (event_id, ev.get("project_id"), ev.get("event_name", ""),
         content_hash, now))

    for lot in ev.get("lots", []):
        lot_id = lot.get("lot_id")
        conn.execute(
            "INSERT OR REPLACE INTO lots VALUES(?,?,?)",
            (event_id, lot_id, lot.get("lot_name", "")))
        for b in lot.get("bids", []):
            bidder_id = b.get("bidder_id")
            # 名称仅作显示（主数据）；uscc/uscc_evidence 属于 bid occurrence
            # 事实，随 (event, lot, bidder) 存取——稳定 ID 跨事件 USCC 变更
            # 不得被全局主数据静默吞掉
            existing_bidder = conn.execute(
                "SELECT display_name FROM bidders WHERE bidder_id=?",
                (bidder_id,)).fetchone()
            if existing_bidder is None:
                conn.execute(
                    "INSERT INTO bidders VALUES(?,?)",
                    (bidder_id, b.get("bidder_name", "")))
            elif existing_bidder[0] != b.get("bidder_name", ""):
                _record_conflict(
                    conn, "bidder_masterdata_conflict", bidder_id,
                    f"同 bidder_id 显示名不一致：既有 {existing_bidder[0]!r}，"
                    f"本事件为 {b.get('bidder_name', '')!r}（名称仅作显示，"
                    "两条记录均保留），首次值继续用于既有事件",
                    result, now)
            outcome = b.get("outcome") or {}
            bid_row = (
                event_id, lot_id, bidder_id,
                b.get("role", "bidder"),
                b.get("uscc"), b.get("uscc_evidence"),
                b.get("total_price"), b.get("currency"),
                int(b.get("tax_included")) if b.get("tax_included") is not None else None,
                b.get("total_price_evidence"),
                outcome.get("status", "unknown"), outcome.get("result"),
                b.get("evidence"), _stable_hash(b))
            existing_bid = conn.execute(
                "SELECT bid_hash FROM bids WHERE event_id=? AND lot_id=?"
                " AND bidder_id=?",
                (event_id, lot_id, bidder_id)).fetchone()
            if existing_bid is None:
                conn.execute(
                    "INSERT INTO bids VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    bid_row)
            else:
                raise ValueError(
                    f"标段 {lot_id} 投标人 {bidder_id} 的 bid 已存在（不应发生于新事件）")
            for c in b.get("contacts", []):
                conn.execute(
                    "INSERT OR IGNORE INTO bid_contacts VALUES(?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, c.get("kind", "unknown"),
                     str(c.get("value")), c.get("source_role", "unknown"),
                     c.get("evidence")))
            for p in b.get("persons", []):
                conn.execute(
                    "INSERT OR IGNORE INTO bid_persons VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, p["person_id"],
                     p.get("name", ""), p.get("id_number", "unknown"),
                     p.get("role", "unknown"), p.get("evidence")))
            for seq, line in enumerate(b.get("price_lines", []), start=1):
                conn.execute(
                    "INSERT OR IGNORE INTO bid_price_lines"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, seq,
                     line.get("item_code"), line.get("item_name"),
                     line.get("spec"), line.get("unit"), line.get("qty"),
                     line.get("unit_price"), line.get("currency"),
                     int(line.get("tax_included")) if line.get("tax_included") is not None else None,
                     line.get("evidence")))
            for f in b.get("files", []):
                # 文件 SHA 相同 ≠ 事件相同：同字节文件在不同事件/主体各占一行
                conn.execute(
                    "INSERT OR IGNORE INTO bid_files VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, lot_id, bidder_id, f.get("sha256", "unknown"),
                     f.get("file_ref", "unknown"),
                     f.get("source_type", "unknown"),
                     f.get("context", "bid_document"),
                     f.get("declared_owner_id"), f.get("declared_uscc"),
                     json.dumps(f.get("metadata") or {}, ensure_ascii=False),
                     f.get("evidence")))


def load_factbase(conn: sqlite3.Connection) -> FactBase:
    """从 SQLite 读回全量累计事实（与导入 JSON 同构），供规则引擎使用。"""
    data = {"schema": SCHEMA, "evidence": [], "events": []}
    for row in conn.execute(
            "SELECT evidence_id, file_sha256, source_type, role, quote,"
            " locator_json FROM evidence"):
        data["evidence"].append({
            "evidence_id": row[0], "file_sha256": row[1],
            "source_type": row[2], "role": row[3], "quote": row[4],
            "locator": json.loads(row[5])})
    lots = defaultdict(list)
    for row in conn.execute(
            "SELECT event_id, lot_id, lot_name FROM lots ORDER BY lot_id"):
        lots[row[0]].append((row[1], row[2]))
    event_names, project_ids = {}, {}
    for row in conn.execute(
            "SELECT event_id, event_name, project_id FROM events"):
        event_names[row[0]] = row[1]
        project_ids[row[0]] = row[2]
    bids = defaultdict(list)
    for row in conn.execute(
            "SELECT event_id, lot_id, bidder_id, role, uscc, uscc_evidence,"
            " total_price, currency, tax_included, total_price_evidence,"
            " outcome_status, outcome_result, bid_evidence"
            " FROM bids ORDER BY bidder_id"):
        bids[(row[0], row[1])].append(row)
    contacts = defaultdict(list)
    for row in conn.execute(
            "SELECT event_id, lot_id, bidder_id, kind, value, source_role,"
            " evidence_id FROM bid_contacts"):
        contacts[(row[0], row[1], row[2])].append(row)
    persons = defaultdict(list)
    for row in conn.execute(
            "SELECT event_id, lot_id, bidder_id, person_id, name, id_number,"
            " role, evidence_id FROM bid_persons"):
        persons[(row[0], row[1], row[2])].append(row)
    lines = defaultdict(list)
    for row in conn.execute(
            "SELECT event_id, lot_id, bidder_id, seq, item_code, item_name,"
            " spec, unit, qty, unit_price, currency, tax_included, evidence_id"
            " FROM bid_price_lines ORDER BY seq"):
        lines[(row[0], row[1], row[2])].append(row)
    files = defaultdict(list)
    for row in conn.execute(
            "SELECT event_id, lot_id, bidder_id, file_sha256, file_ref,"
            " source_type, context, declared_owner_id, declared_uscc,"
            " metadata_json, evidence_id FROM bid_files"):
        files[(row[0], row[1], row[2])].append(row)
    bidder_rows = {r[0]: r for r in conn.execute(
        "SELECT bidder_id, display_name FROM bidders")}

    for event_id in sorted(lots):
        lots_json = []
        for lot_id, lot_name in lots[event_id]:
            bids_json = []
            for (bidder_id, role, uscc, uscc_ev, price, currency, tax, tpe,
                 status, result, bid_ev) in [
                    (r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9],
                     r[10], r[11], r[12])
                    for r in bids.get((event_id, lot_id), [])]:
                b_row = bidder_rows.get(bidder_id)
                bids_json.append({
                    "bidder_id": bidder_id,
                    "bidder_name": b_row[1] if b_row else "unknown",
                    "role": role,
                    # uscc/uscc_evidence 属于 bid occurrence 事实，
                    # 按事件读回，不从全局主数据回填
                    "uscc": uscc,
                    "uscc_evidence": uscc_ev,
                    "total_price": price, "currency": currency,
                    "tax_included": _tax_from_db(tax),
                    "total_price_evidence": tpe,
                    "outcome": {"status": status, "result": result},
                    "evidence": bid_ev,
                    "contacts": [
                        {"kind": k, "value": v, "source_role": sr,
                         "evidence": e}
                        for (_, _, _, k, v, sr, e)
                        in contacts.get((event_id, lot_id, bidder_id), [])],
                    "persons": [
                        {"person_id": pid, "name": n, "id_number": i,
                         "role": r, "evidence": e}
                        for (_, _, _, pid, n, i, r, e)
                        in persons.get((event_id, lot_id, bidder_id), [])],
                    "price_lines": [
                        {"item_code": ic, "item_name": iN, "spec": sp,
                         "unit": u, "qty": q, "unit_price": up,
                         "currency": cur,
                         "tax_included": _tax_from_db(tx),
                         "evidence": e}
                        for (_, _, _, _, ic, iN, sp, u, q, up, cur, tx, e)
                        in lines.get((event_id, lot_id, bidder_id), [])],
                    "files": [
                        {"file_ref": fr, "sha256": sha, "source_type": st,
                         "context": ctx, "declared_owner_id": do,
                         "declared_uscc": du,
                         "metadata": json.loads(mj), "evidence": e}
                        for (_, _, _, sha, fr, st, ctx, do, du, mj, e)
                        in files.get((event_id, lot_id, bidder_id), [])],
                })
            lots_json.append({"lot_id": lot_id, "lot_name": lot_name,
                              "bids": bids_json})
        data["events"].append({
            "event_id": event_id, "project_id": project_ids.get(event_id),
            "event_name": event_names.get(event_id, ""), "lots": lots_json})
    return FactBase.from_dict(data)


def event_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def conflicts(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"kind": r[0], "key": r[1], "detail": r[2], "detected_at": r[3]}
        for r in conn.execute(
            "SELECT kind, key, detail, detected_at FROM conflicts"
            " ORDER BY id")]

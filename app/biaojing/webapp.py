# -*- coding: utf-8 -*-
"""标镜 P3 本地工作台：标准库 http.server，仅监听 127.0.0.1。

安全边界：
  - 仅绑定 127.0.0.1；请求体大小硬上限（超限/chunked 在读取前拒绝）。
  - 上传/变更 POST 校验 Host 与同源 Origin，阻断跨站对 localhost 的滥用。
  - 原始字节按 SHA-256 哈希路径存储，客户端文件名仅作来源引用。
页面与资源全部内嵌本地，无 CDN/外部请求；显示名统一「标镜」。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import workspace as ws_mod
from .ui_page import INDEX_HTML

MAX_BODY = ws_mod.MAX_UPLOAD_BYTES
LOCAL_HOST = "127.0.0.1"
LONG_PDF_ASYNC_PAGES = 3


def _docx_has_media(data: bytes) -> bool:
    """安全预检 DOCX 包后，仅按 media 成员名决定是否走后台 OCR。"""
    from . import cli, zip_container

    if zip_container.guard_ooxml(
            data, cli.DEFAULT_MAX_ZIP_ENTRIES,
            cli.DEFAULT_MAX_ZIP_TOTAL_BYTES) is not None:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return any(not info.is_dir() and
                       info.filename.startswith("word/media/")
                       for info in archive.infolist())
    except Exception:
        return False


def workspace_guard(root):
    """用独立 SQLite 锁阻止同工作区双开；进程退出后操作系统自动释放。"""
    os.makedirs(root, exist_ok=True)
    guard = sqlite3.connect(os.path.join(root, 'instance_guard.sqlite3'), timeout=0)
    try:
        guard.execute('BEGIN EXCLUSIVE')
    except sqlite3.Error:
        guard.close()
        raise
    return guard


class WorkbenchServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, workbench: "ws_mod.Workbench"):
        self.workbench = workbench
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        super().__init__(address, Handler)

    def start_job(self, kind, work):
        with self._jobs_lock:
            if any(job["status"] in ("queued", "running")
                   for job in self._jobs.values()):
                raise RuntimeError("当前已有长任务在运行，请完成或取消后再试")
            job_id = uuid.uuid4().hex
            job = {"job_id": job_id, "kind": kind, "status": "running",
                   "page": 0, "total_pages": 0, "stage": "准备中",
                   "page_status": None, "cancel_requested": False,
                   "result": None, "error": None,
                   "_cancel": threading.Event()}
            self._jobs[job_id] = job
            terminal = [key for key, value in self._jobs.items()
                        if value["status"] not in ("queued", "running")]
            for key in terminal[:-32]:
                del self._jobs[key]
            threading.Thread(target=self._run_job, args=(job_id, work),
                             daemon=True).start()
            return {key: value for key, value in job.items()
                    if key != "_cancel"}

    def _run_job(self, job_id, work):
        def progress(page, total, stage, page_status):
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job:
                    job.update(page=page, total_pages=total, stage=stage,
                               page_status=page_status)
        try:
            cancel_event = self._jobs[job_id]["_cancel"]
            result = work(progress, cancel_event)
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job:
                    job["result"] = result
                    job["status"] = ("cancelled" if result.get("cancelled")
                                     else "completed")
                    job["stage"] = "已取消，未处理页面已保留" if result.get("cancelled") else "已完成"
        except Exception as exc:
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    job["stage"] = "处理失败"

    def get_job(self, job_id):
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return ({key: value for key, value in job.items() if key != "_cancel"}
                    if job else None)

    def cancel_job(self, job_id):
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job["status"] in ("queued", "running"):
                job["_cancel"].set()
                job["cancel_requested"] = True
                job["stage"] = "取消已请求；正在完成当前页"
            return {key: value for key, value in job.items() if key != "_cancel"}


class Handler(BaseHTTPRequestHandler):
    server_version = "BiaojingWorkbench/0.1"

    # ---------------------------------------------------------- 安全检查

    def _security_reject(self, mutating: bool) -> tuple[int, str] | None:
        # Host 与 Origin 都做 URL 解析后的精确 hostname+port 匹配，
        # 拒绝任何前缀相似域（如 127.0.0.1.evil.com）与缺失 Origin。
        # 返回 None 放行；(code, 中文详情) 则拒绝——详情走 UTF-8 JSON 体，
        # HTTP reason 保持 ASCII（latin-1 编码限制）。
        from urllib.parse import urlparse

        actual_port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        try:
            hp = urlparse("http://" + host)
            host_ok = (hp.hostname == LOCAL_HOST
                       and hp.port == actual_port)
        except ValueError:
            host_ok = False
        if not host_ok:
            return 403, (f"Host 非法：{host!r}"
                         f"（仅允许 127.0.0.1:{actual_port}）")
        if mutating:
            origin = self.headers.get("Origin")
            if not origin:
                return 403, "缺少 Origin 头，拒绝变更请求"
            try:
                op = urlparse(origin)
                origin_ok = (op.scheme == "http"
                             and op.hostname == LOCAL_HOST
                             and op.port == actual_port)
            except ValueError:
                origin_ok = False
            if not origin_ok:
                return 403, f"Origin 非同源：{origin!r}，拒绝"
        return None

    def _reject(self, code: int, detail: str):
        """HTTP reason 保持 ASCII；中文拒绝详情放 UTF-8 JSON 体。"""
        body = json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, record_reject_to=None, reject_name: str = "") -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self._reject(411, "拒绝 chunked 请求体")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reject(400, "Content-Length 非法")
            return None
        if length <= 0:
            # 空上传同样进入覆盖率（带原因的拒收记录），分母不漏该
            # 提交；安全拒绝语义不变：不为空文件产出 artifact/哈希/证据
            if record_reject_to is not None:
                record_reject_to(reject_name,
                                 f"请求体为空（{length} 字节），拒收")
            self._reject(400, "缺少请求体")
            return None
        if length > MAX_BODY:
            # 超限拒收进入工作台覆盖率（带原因），可见而非静默丢弃
            if record_reject_to is not None:
                record_reject_to(reject_name,
                                 f"请求体 {length} 字节超过上限 {MAX_BODY}")
            self._reject(413, f"请求体 {length} 字节超过上限 {MAX_BODY}")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._reject(400, "请求体不完整，请重新上传")
            return None
        return body

    def _read_json(self):
        body = self._read_body()
        if body is None:
            return None
        try:
            req = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            self._reject(400, "请求不是有效的 UTF-8 JSON")
            return None
        if not isinstance(req, dict):
            self._reject(400, "请求必须是 JSON 对象")
            return None
        return req

    # ---------------------------------------------------------- 工具

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",
                         "application/json; charset=utf-8")
        # 禁缓存：防止 Chrome 用旧 HTML/旧 JS（曾导致修复在盘上而
        # 浏览器仍执行旧上传逻辑）
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 安静模式（避免刷屏）
        pass

    # ---------------------------------------------------------- 路由

    def do_GET(self):
        reject = self._security_reject(mutating=False)
        if reject:
            self._reject(*reject)
            return
        wb = self.server.workbench
        if self.path in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
            return
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            qs = parse_qs(parsed.query)
            try:
                offset = int(qs.get("candidate_offset", ["0"])[0])
                limit = int(qs.get("candidate_limit", ["200"])[0])
                source_offset = int(qs.get("source_offset", ["0"])[0])
                source_limit = int(qs.get("source_limit", ["100"])[0])
                self._send_json(wb.state(offset, limit, source_offset,
                                         source_limit))
            except (TypeError, ValueError) as exc:
                self._reject(400, f"工作台分页参数无效：{exc}")
            return
        if self.path == "/api/about":
            from . import AUTHOR, PRODUCT_NAME, VERSION
            from .updater import repository_name
            self._send_json({
                "product_name": PRODUCT_NAME, "author": AUTHOR,
                "version": VERSION,
                "updates_enabled": bool(repository_name()),
            })
            return
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.removeprefix("/api/jobs/")
            job = self.server.get_job(job_id)
            if job is None:
                self._reject(404, "长任务不存在或已过期")
            else:
                self._send_json(job)
            return
        if self.path.startswith("/api/evidence/"):
            eid = self.path.rsplit("/", 1)[-1]
            info = wb.evidence_by_id(eid)
            if info is None:
                self._send_json({"error": f"证据 {eid!r} 不存在"}, 404)
            else:
                info["refs"] = wb.refs_for_sha(info["sha256"])
                self._send_json(info)
            return
        if self.path.startswith("/api/download/"):
            sha = self.path.rsplit("/", 1)[-1].lower()
            # 只接受库中存在的 64 位十六进制 SHA-256；按工作区固定哈希路径
            # 读取，绝不接受文件系统路径参数（路径穿越不可达）
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                self._reject(400, "非法 SHA-256（须为 64 位十六进制）")
                return
            try:
                data = wb.original_bytes(sha)
            except ValueError as exc:
                self._reject(409, str(exc))
                return
            if data is None:
                self._reject(404, "工作区中不存在该 SHA-256 的原件")
                return
            # HTTP 头按 latin-1 编码：文件名 ASCII fallback（sha）为主，
            # UTF-8 显示名走 RFC 5987 filename*
            from urllib.parse import quote as _q
            display_name = wb.display_name_for(sha) or sha
            safe_name = "".join(
                c if (c.isascii() and (c.isalnum() or c in "._-")) else "_"
                for c in display_name) or (sha + ".bin")
            utf8_name = _q(display_name, safe="")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{safe_name}"; '
                f"filename*=UTF-8''{utf8_name}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        reject = self._security_reject(mutating=True)
        if reject:
            self._reject(*reject)
            return
        wb = self.server.workbench
        from urllib.parse import urlparse
        route = urlparse(self.path).path  # /api/upload?name=... 的 query 单独读
        if route == "/api/upload":
            from urllib.parse import parse_qs
            from . import pdf_parser

            # parse_qs 已完成一次 URL 解码；不再额外 unquote，避免把合法
            # 文件名中的字面 %2F 二次改写成路径分隔符
            qs = parse_qs(urlparse(self.path).query)
            name = qs.get("name", ["未命名"])[0][:512]
            source_type = qs.get("source_type", ["unknown"])[0][:64]
            body = self._read_body(
                record_reject_to=wb.record_rejected, reject_name=name)
            if body is None:
                return
            if name.lower().endswith(".zip"):
                try:
                    def ingest_zip_job(progress, stop):
                        results = wb.ingest_zip(name, body, source_type,
                                                progress=progress,
                                                cancelled=stop.is_set)
                        return {"results": results,
                                "cancelled": stop.is_set()}
                    job = self.server.start_job(
                        "zip_upload", ingest_zip_job)
                except RuntimeError as exc:
                    self._reject(409, str(exc))
                    return
                self._send_json({"ok": True, "job_id": job["job_id"]})
            elif name.lower().endswith(".docx") and _docx_has_media(body):
                try:
                    job = self.server.start_job(
                        "docx_upload",
                        lambda progress, stop: wb.ingest_bytes(
                            name, body, source_type, progress=progress,
                            cancelled=stop.is_set))
                except RuntimeError as exc:
                    self._reject(409, str(exc))
                    return
                self._send_json({"ok": True, "job_id": job["job_id"]})
            elif (name.lower().endswith(".pdf")
                  and (pdf_parser.page_count(body) or 0) >= LONG_PDF_ASYNC_PAGES):
                try:
                    job = self.server.start_job(
                        "pdf_upload",
                        lambda progress, stop: wb.ingest_bytes(
                            name, body, source_type, progress=progress,
                            cancelled=stop.is_set))
                except RuntimeError as exc:
                    self._reject(409, str(exc))
                    return
                self._send_json({"ok": True, "job_id": job["job_id"]})
            else:
                self._send_json({"ok": True,
                                 "result": wb.ingest_bytes(name, body,
                                                           source_type)})
            return
        if route == "/api/confirm":
            req = self._read_json()
            if req is None:
                return
            self._send_json(wb.confirm_field(
                req.get("event_id"), req.get("lot_id"), req.get("bidder_id"),
                req.get("field"), req.get("value"),
                req.get("evidence_id"), req.get("action", "confirm"),
                req.get("original_candidate"),
                req.get("source_role", "unknown"),
                req.get("person_id"),
                reviewer_type=req.get("reviewer_type")))
            return
        if route == "/api/confirm_amount":
            req = self._read_json()
            if req is None:
                return
            self._send_json(wb.confirm_amount(
                req.get("event_id"), req.get("lot_id"), req.get("bidder_id"),
                req.get("raw_value"), req.get("unit"), req.get("currency"),
                req.get("tax_included"), req.get("evidence_id"),
                req.get("action", "confirm"), req.get("original_candidate"),
                reviewer_type=req.get("reviewer_type")))
            return
        if route == "/api/bind_file":
            req = self._read_json()
            if req is None:
                return
            self._send_json(wb.bind_file(
                req.get("event_id"), req.get("lot_id"), req.get("bidder_id"),
                req.get("sha256"), req.get("context"), req.get("source_type"),
                req.get("declared_owner_id"), req.get("declared_uscc")))
            return
        if route == "/api/reject_record":
            # 页面枚举/超限拒收上报：只收小型 JSON 记录，复用 record_rejected
            req = self._read_json()
            if req is None:
                return
            name = str(req.get("name", ""))[:512]
            reason = str(req.get("reason", ""))[:300]
            wb.record_rejected(name or "未命名", reason or "页面拒收上报")
            self._send_json({"ok": True})
            return
        if route == "/api/update/check":
            if self._read_json() is None:
                return
            from .updater import check_and_stage_update
            try:
                self._send_json(check_and_stage_update())
            except Exception as exc:
                self._reject(502, f"更新检查失败：{exc}")
            return
        if route.startswith("/api/jobs/") and route.endswith("/cancel"):
            if self._read_json() is None:
                return
            job_id = route.removeprefix("/api/jobs/").removesuffix("/cancel")
            job = self.server.cancel_job(job_id)
            if job is None:
                self._reject(404, "长任务不存在或已过期")
            else:
                self._send_json(job)
            return
        if route == "/api/retry_ocr":
            req = self._read_json()
            if req is None:
                return
            sha = req.get("sha256")
            if not isinstance(sha, str) or len(sha) != 64 or any(
                    c not in "0123456789abcdef" for c in sha):
                self._reject(400, "非法 SHA-256（须为 64 位小写十六进制）")
                return
            try:
                job = self.server.start_job(
                    "ocr_retry",
                    lambda progress, stop: wb.retry_ocr(
                        sha, progress=progress, cancelled=stop.is_set))
            except RuntimeError as exc:
                self._reject(409, str(exc))
                return
            self._send_json({"ok": True, "job_id": job["job_id"]})
            return
        if route == "/api/screen":
            body = self._read_body()
            if body is None:
                return
            try:
                result = wb.run_screen()
            except ValueError as exc:
                wb.record_screen_failure(str(exc))
                self._send_json({"run_status": "failed",
                                 "error": f"确认数据无法筛查：{exc}"}, 400)
                return
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                wb.record_screen_failure(error)
                self._send_json({"run_status": "failed",
                                 "error": "筛查执行失败，失败状态已记录"}, 500)
                return
            self._send_json(result)
            return
        self.send_error(404)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="标镜",
        description="标镜（python -m biaojing.webapp）P3 本地工作台："
                    "批量拖入资料 → 解析覆盖率 → 证据回溯 → 字段确认 → "
                    "规则筛查。仅监听 127.0.0.1，数据保存在本地工作区。",
    )
    ap.add_argument("--workspace", default="biaojing_workspace",
                    help="工作区目录（默认 ./biaojing_workspace）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open-browser", action="store_true", help="启动后打开本机浏览器")
    args = ap.parse_args(argv)
    if not 0 <= args.port <= 65535:
        ap.error("端口须在 0 到 65535 之间")

    try:
        guard = workspace_guard(args.workspace)
    except sqlite3.Error as exc:
        print(f"工作区无法独占打开（可能已在另一个标镜窗口中使用）：{exc}", file=sys.stderr)
        return 1
    try:
        wb = ws_mod.Workbench(args.workspace)
    except Exception:
        guard.close()
        raise
    try:
        server = WorkbenchServer((LOCAL_HOST, args.port), wb)
    except OSError as exc:
        wb.close()
        guard.close()
        print(f"标镜无法启动：{exc}。可用 --port 0 自动选择空闲端口。", file=sys.stderr)
        return 1
    url = f"http://{LOCAL_HOST}:{server.server_port}/"
    print(f"标镜工作台已启动：{url}"
          f"（工作区 {os.path.abspath(args.workspace)}，Ctrl+C 退出）",
          file=sys.stderr)
    try:
        if args.open_browser:
            import webbrowser
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        wb.close()
        guard.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

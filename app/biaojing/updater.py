# -*- coding: utf-8 -*-
"""基于 GitHub Releases 的源码版更新器；只替换 app/biaojing。"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

from . import VERSION

# 环境变量仅用于本地验证或发行版覆盖。
GITHUB_REPOSITORY = "fahaxiki67/biaojing"
ASSET_PREFIX = "biaojing-source-"
REQUEST_TIMEOUT = 5
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
_UPDATE_LOCK = threading.Lock()


class UpdateError(RuntimeError):
    pass


def repository_name(value: str | None = None) -> str:
    repo = (value if value is not None else
            os.environ.get("BIAOJING_GITHUB_REPOSITORY", GITHUB_REPOSITORY)).strip()
    if not repo:
        return ""
    if (len(repo) > 200 or repo.count("/") != 1
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or any(part in (".", "..") for part in repo.split("/"))):
        raise UpdateError("更新仓库配置必须是公开 GitHub 仓库的 owner/repo")
    return repo


def version_key(value: str) -> tuple[int, ...]:
    text = str(value).strip().removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", text):
        raise UpdateError(f"版本号格式无法识别：{value}")
    return tuple(int(part) for part in text.split("."))


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _request(url: str, limit: int) -> tuple[bytes, str]:
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Biaojing-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            data = response.read(limit + 1)
            final_url = response.geturl()
    except Exception as exc:
        raise UpdateError(f"连接 GitHub 失败：{exc}") from exc
    if len(data) > limit:
        raise UpdateError("GitHub 返回的数据超过安全上限")
    return data, final_url


def _latest_release(repo: str) -> dict:
    data, _ = _request(
        f"https://api.github.com/repos/{repo}/releases/latest", 1024 * 1024)
    try:
        release = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("GitHub 返回的版本信息无法读取") from exc
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise UpdateError("GitHub 返回的版本信息不完整")
    return release


def _pending(root: Path) -> dict | None:
    path = root / ".biaojing-update-staging" / "pending.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _rejected_update(root: Path) -> dict | None:
    try:
        value = json.loads((root / ".biaojing-update-rejected.json").read_text(
            encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _check_archive_name(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise UpdateError("更新包包含非法路径")
    path = PurePosixPath(name)
    if (path.is_absolute() or any(part in ("", ".", "..") for part in path.parts)
            or len(path.parts) < 3 or path.parts[:2] != ("app", "biaojing")):
        raise UpdateError("更新包包含程序目录以外的文件")
    return path


def _extract_update(archive: bytes, destination: Path, expected_version: str):
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("GitHub 更新包不是有效 ZIP") from exc
    total = 0
    count = 0
    saw_init = False
    seen = set()
    with bundle:
        for info in bundle.infolist():
            count += 1
            if count > MAX_ARCHIVE_FILES:
                raise UpdateError("更新包文件数量超过安全上限")
            raw_name = info.filename.rstrip("/") if info.is_dir() else info.filename
            member = PurePosixPath(raw_name)
            if info.is_dir():
                if (member.is_absolute()
                        or any(part in ("", ".", "..") for part in member.parts)
                        or not (member.parts == ("app",)
                                or member.parts[:2] == ("app", "biaojing"))):
                    raise UpdateError("更新包包含程序目录以外的文件")
            else:
                member = _check_archive_name(raw_name)
            if member.parts in seen:
                raise UpdateError("更新包包含重复路径")
            seen.add(member.parts)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UpdateError("更新包不允许包含符号链接")
            if info.is_dir():
                continue
            total += info.file_size
            if info.file_size > 16 * 1024 * 1024 or total > MAX_UNPACKED_BYTES:
                raise UpdateError("更新包解压内容超过安全上限")
            target = destination.joinpath(*member.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            copied = 0
            with bundle.open(info) as source, target.open("wb") as output:
                while True:
                    chunk = source.read(min(1024 * 1024,
                                            info.file_size - copied + 1))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > info.file_size:
                        raise UpdateError("更新包内容大小与目录记录不一致")
                    output.write(chunk)
            if copied != info.file_size:
                raise UpdateError("更新包文件未完整解压")
            if member.parts == ("app", "biaojing", "__init__.py"):
                saw_init = True
    package = destination / "app" / "biaojing"
    if not saw_init or _package_version(package) != version_key(expected_version):
        raise UpdateError("更新包版本与 GitHub Release 标签不一致")


def _package_version(package: Path) -> tuple[int, ...] | None:
    # 发行包生成器和更新器只接受固定版本常量，避免执行待安装代码来取版本。
    import ast
    try:
        tree = ast.parse((package / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "VERSION"
                for target in node.targets):
            try:
                return version_key(ast.literal_eval(node.value))
            except (ValueError, TypeError, UpdateError):
                return None
    return None


def _stage(root: Path, archive: bytes, expected_version: str):
    temp = Path(tempfile.mkdtemp(prefix=".biaojing-update-", dir=root))
    staging = root / ".biaojing-update-staging"
    try:
        _extract_update(archive, temp, expected_version)
        (temp / "pending.json").write_text(json.dumps({
            "version": expected_version,
        }), encoding="utf-8")
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        os.replace(temp, staging)
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def check_and_stage_update(repository: str | None = None) -> dict:
    """Check the latest stable release and stage its verified source bundle."""
    with _UPDATE_LOCK:
        repo = repository_name(repository)
        if not repo:
            return {"status": "unconfigured", "current_version": VERSION}
        root = _project_root()
        release = _latest_release(repo)
        tag = release["tag_name"]
        latest = version_key(tag)
        current = version_key(VERSION)
        rejected = _rejected_update(root)
        if rejected and rejected.get("version") == tag and latest > current:
            return {"status": "rejected", "current_version": VERSION,
                    "latest_version": tag,
                    "reason": rejected.get("reason", "兼容性检查失败")}
        pending = _pending(root)
        if pending and pending.get("version") == tag and latest > current:
            return {"status": "staged", "current_version": VERSION,
                    "latest_version": tag}
        if latest <= current:
            return {"status": "current", "current_version": VERSION,
                    "latest_version": tag}

        asset_name = f"{ASSET_PREFIX}{tag}.zip"
        assets = release.get("assets", [])
        asset = next((item for item in assets
                      if isinstance(item, dict) and item.get("name") == asset_name), None)
        if not asset:
            raise UpdateError(f"Release {tag} 缺少更新包 {asset_name}")
        digest = asset.get("digest")
        size = asset.get("size")
        url = asset.get("browser_download_url")
        if (not isinstance(digest, str)
                or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)
                or type(size) is not int or size < 1 or size > MAX_ARCHIVE_BYTES):
            raise UpdateError("GitHub 未提供有效 SHA-256 或更新包大小不安全")
        parsed = urlsplit(url or "")
        if (parsed.scheme != "https" or parsed.hostname != "github.com"
                or not parsed.path.startswith(f"/{repo}/releases/download/")):
            raise UpdateError("GitHub 更新包地址不符合预期")
        archive, final_url = _request(url, MAX_ARCHIVE_BYTES)
        final_host = (urlsplit(final_url).hostname or "").lower()
        if (final_host != "github.com"
                and not final_host.endswith(".githubusercontent.com")):
            raise UpdateError("更新包下载跳转到了未认可的主机")
        if len(archive) != size or hashlib.sha256(archive).hexdigest().lower() != digest[7:].lower():
            raise UpdateError("更新包 SHA-256 校验失败")
        _stage(root, archive, tag)
        return {"status": "staged", "current_version": VERSION,
                "latest_version": tag}


def apply_pending_update(project_root: str | Path | None = None) -> str | None:
    """Swap the package, import-check it, and restore the prior version on failure."""
    root = Path(project_root) if project_root is not None else _project_root()
    staging = root / ".biaojing-update-staging"
    pending = _pending(root)
    if not pending:
        return None
    tag = pending.get("version")
    try:
        version_key(tag)
    except (UpdateError, TypeError):
        raise UpdateError("待安装更新的版本记录损坏")
    source = staging / "app" / "biaojing"
    if _package_version(source) != version_key(tag):
        raise UpdateError("待安装更新包校验失败")
    target = root / "app" / "biaojing"
    backup = root / ".biaojing-update-backup"
    if not target.is_dir() or target.is_symlink():
        raise UpdateError("程序目录不存在或不是普通目录，已取消更新")
    if backup.is_symlink():
        backup.unlink()
    elif backup.exists():
        shutil.rmtree(backup)
    os.replace(target, backup)
    try:
        os.replace(source, target)
        modules = ("pdf_parser", "docx_parser", "xlsx_parser", "money",
                   "candidates", "rules", "workspace", "webapp", "cli",
                   "updater")
        missing = [name for name in modules
                   if not (target / f"{name}.py").is_file()]
        if missing:
            raise UpdateError("更新包缺少关键模块：" + ", ".join(missing))
        smoke = (
            "import importlib,pathlib,sys\n"
            "package=pathlib.Path(sys.argv[1]);"
            "sys.path.insert(0,str(package.parent));sys.dont_write_bytecode=True\n"
            "for p in package.rglob('*.py'):"
            "compile(p.read_text(encoding='utf-8'),str(p),'exec')\n"
            "for n in " + repr(modules) + ": importlib.import_module('biaojing.'+n)\n"
        )
        env = os.environ.copy()
        package_parent = str(root / "app")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (package_parent + os.pathsep + existing
                             if existing else package_parent)
        try:
            result = subprocess.run(
                [sys.executable, "-c", smoke, str(target)],
                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"更新兼容性检查未完成：{exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "未返回错误详情").strip()
            raise UpdateError("更新兼容性检查失败：" + detail[-1200:])
    except Exception as exc:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            os.replace(backup, target)
        reason = str(exc)[:1200]
        (root / ".biaojing-update-rejected.json").write_text(
            json.dumps({"version": tag, "reason": reason},
                       ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    rejected_path = root / ".biaojing-update-rejected.json"
    if rejected_path.is_file() and not rejected_path.is_symlink():
        rejected_path.unlink()
    return tag


def update_at_start(repository: str | None = None) -> str | None:
    """Apply a staged update, then automatically check and install a new release."""
    applied = apply_pending_update()
    if applied:
        return applied
    result = check_and_stage_update(repository)
    return apply_pending_update() if result["status"] == "staged" else None

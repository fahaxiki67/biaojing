# -*- coding: utf-8 -*-
"""输入展开：文件 / 目录 / zip -> 统一的待处理引用流。

引用显示格式（file 字段）：
  文件系统文件：绝对路径
  zip 内条目：  `<zip绝对路径>::<条目名>`
目录按排序遍历，不跟随符号链接（防链接越界）。
"""

from __future__ import annotations

import os

from . import zip_container


def iter_inputs(paths: list[str], max_entries: int, max_total_uncompressed: int):
    """yield (display_ref, origin, bytes_or_None, error_or_None)。

    zip 容器本身不产出文件记录，只产出其条目；容器读取失败/超限时产出
    一条以容器为对象的失败记录（bytes=None, error=原因）。
    """
    for raw in paths:
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.lexists(p):
            yield (p, {"kind": "filesystem", "path": p}, None,
                   "输入路径不存在")
            continue
        if os.path.islink(p):
            yield (p, {"kind": "filesystem", "path": p}, None,
                   "输入是符号链接，拒绝（防链接越界）")
            continue
        if os.path.isdir(p):
            yield from _walk_dir(p, max_entries, max_total_uncompressed)
        else:
            yield from _load_file(p, {}, max_entries, max_total_uncompressed)


def _walk_dir(root: str, max_entries: int, max_total_uncompressed: int):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                yield (full, {"kind": "filesystem", "path": full}, None,
                       "目录内符号链接，跳过（防链接越界）")
                continue
            yield from _load_file(full, {}, max_entries, max_total_uncompressed)


def _load_file(path: str, origin: dict, max_entries: int,
               max_total_uncompressed: int):
    origin = dict(origin)
    origin.setdefault("kind", "filesystem")
    origin.setdefault("path", path)
    display = path if origin["kind"] == "filesystem" else f"{origin['container']}::{origin['entry']}"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        yield (display, origin, None, f"读取失败：{exc}")
        return
    lower = path.lower()
    is_ooxml = lower.endswith((".docx", ".xlsx", ".xlsm"))
    if data[:4] == b"PK\x03\x04" and not is_ooxml:
        # .zip 及无扩展名/非常规扩展的 zip 容器一律按容器展开；
        # docx/xlsx 是文档不是容器，交给解析器（其内部 zip 由限额护栏保护）
        yield from _expand_zip(path, data, origin, max_entries, max_total_uncompressed)
        return
    yield (display, origin, data, None)


def _expand_zip(path: str, data: bytes, origin: dict,
                max_entries: int, max_total_uncompressed: int):
    try:
        result = zip_container.expand(data, max_entries, max_total_uncompressed)
    except zip_container.ZipLimitExceeded as exc:
        yield (path, {"kind": "filesystem", "path": path}, None, str(exc))
        return
    except zip_container.ZipBadArchive as exc:
        yield (path, {"kind": "filesystem", "path": path}, None, str(exc))
        return
    seen_names: dict[str, int] = {}
    for entry in result["entries"]:
        name = entry["name"]
        seen_names[name] = seen_names.get(name, 0) + 1
        display_name = name if seen_names[name] == 1 else f"{name}#{seen_names[name]}"
        entry_origin = {
            "kind": "zip_entry",
            "container": path,
            "entry": display_name,
        }
        display = f"{path}::{display_name}"
        yield (display, entry_origin, entry["data"], None)
    for rej in result["rejected"]:
        name = rej["name"]
        display = f"{path}::{name}"
        yield (display, {"kind": "zip_entry_rejected", "container": path, "entry": name},
               None, f"zip 条目被拒收：{rej['reason']}")

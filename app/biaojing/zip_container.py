# -*- coding: utf-8 -*-
"""ZIP 容器处理：全程内存读取，不落盘解压；显式限额与安全检查。

安全口径（总指导：防路径穿越、链接越界、解压膨胀、重名覆盖）：
  - 条目数超限或声明解压总量超限 -> 整包拒绝（未读取任何数据即拒绝）。
  - 条目名含 `..` 段、以 `/` 或盘符开头 -> 拒收该条目（记录于 rejected_entries）。
  - symlink 条目 -> 拒收（即使不落盘也显式拒绝，保持纪律一致）。
  - 加密条目 -> 拒收并注明原因。
  - 重名条目：zip 允许同名条目并存，读取时按原样逐条保留，输出引用加序号
    去重，绝不互相覆盖。
  - 嵌套 zip 条目不展开（P1 范围），逐条标记由上层处理。
"""

from __future__ import annotations

import io
import stat
import zipfile


class ZipLimitExceeded(Exception):
    """条目数或声明解压总量超过限额，整包拒绝。"""


class ZipBadArchive(Exception):
    """不是合法 zip 或结构性损坏。"""


def expand(data: bytes, max_entries: int, max_total_uncompressed: int) -> dict:
    """返回 {entries: [{name, data}], rejected: [{name, reason}]}。

    超限时抛 ZipLimitExceeded / ZipBadArchive，由上层整包记 failed。
    """
    try:
        # is_zipfile 预检：构造失败的对象在 GC 时会发出无害 stderr 噪音
        # （Python 3.14 zipfile.__del__），预检把明显损坏的字节挡在构造之前
        probe = io.BytesIO(data)
        if not zipfile.is_zipfile(probe):
            raise ZipBadArchive("无法作为 zip 打开：字节头或中央目录无 zip 特征")
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ZipBadArchive(f"无法作为 zip 打开：{type(exc).__name__}: {exc}") from exc

    with zf:
        infos = zf.infolist()
        real = [i for i in infos if not i.is_dir()]
        if len(real) > max_entries:
            raise ZipLimitExceeded(
                f"zip 内文件数 {len(real)} 超过限额 {max_entries}，整包拒绝"
            )
        declared = sum(i.file_size for i in real)
        if declared > max_total_uncompressed:
            raise ZipLimitExceeded(
                f"zip 声明解压总量 {declared} 字节超过限额 "
                f"{max_total_uncompressed} 字节（防解压膨胀），整包拒绝"
            )
        entries = []
        rejected = []
        for info in real:
            name = info.filename
            reason = _entry_reject_reason(name, info)
            if reason:
                rejected.append({"name": name, "reason": reason})
                continue
            try:
                data_entry = zf.read(info)
            except RuntimeError as exc:
                rejected.append({"name": name, "reason": f"加密条目无法读取：{exc}"})
                continue
            except Exception as exc:
                rejected.append({"name": name, "reason": f"条目读取失败：{type(exc).__name__}: {exc}"})
                continue
            if len(data_entry) != info.file_size:
                rejected.append({
                    "name": name,
                    "reason": f"实际解压字节数 {len(data_entry)} 与声明 {info.file_size} 不符，拒收",
                })
                continue
            entries.append({"name": name, "data": data_entry})
        return {"entries": entries, "rejected": rejected}


def guard_ooxml(data: bytes, max_entries: int,
                max_total_uncompressed: int) -> str | None:
    """docx/xlsx 本身是 zip 容器，解析前先做同样的限额检查。

    返回 None 表示通过；返回字符串为拒绝原因（调用方据此整文件记 failed）。
    只读中央目录的声明值，不实际解压任何成员。
    """
    try:
        probe = io.BytesIO(data)
        if not zipfile.is_zipfile(probe):
            return "OOXML 容器无法作为 zip 打开：字节头或中央目录无 zip 特征"
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        return f"OOXML 容器无法作为 zip 打开：{type(exc).__name__}: {exc}"
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > max_entries:
            return (f"OOXML 容器成员数 {len(infos)} 超过限额 {max_entries}，"
                    "拒绝解析（防解压膨胀/资源耗尽）")
        declared = sum(i.file_size for i in infos)
        if declared > max_total_uncompressed:
            return (f"OOXML 容器声明解压总量 {declared} 字节超过限额 "
                    f"{max_total_uncompressed} 字节，拒绝解析（防解压膨胀）")
    return None


def _entry_reject_reason(name: str, info: zipfile.ZipInfo) -> str | None:
    parts = name.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        return "路径含 `..` 段（防路径穿越），拒收"
    if name.startswith(("/", "\\")) or (len(name) >= 2 and name[1] == ":"):
        return "绝对路径或盘符开头（防越界），拒收"
    mode = (info.external_attr >> 16) & 0o170000
    if mode and stat.S_ISLNK(mode):
        return "符号链接条目（防链接越界），拒收"
    if info.flag_bits & 0x1:
        return "加密条目，本底座不处理口令，拒收"
    return None

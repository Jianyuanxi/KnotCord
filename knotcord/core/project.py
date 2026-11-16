"""项目键：CLAUDE_PROJECT_DIR 优先；否则向上找 .git 取那层目录名；找不到取 cwd 目录名。不调 git。

Claude Code 和 Codex 共用这一套规则（同一个文件夹两边得到同一个名字）。取名前先把路径写法理平：
Windows 的 \\\\?\\ 前缀、WSL 的 /mnt/c/ 写法、盘符和目录名的大小写（以磁盘上的实际写法为准；
路径不存在就原样用）。
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

_WSL_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")


def _name_of(p: Path) -> str:
    if p.name:
        return p.name
    # 盘符根目录：C:\ -> "C"
    return p.anchor.replace(":", "").replace("\\", "").replace("/", "") or "root"


def _normalize(path: str) -> str:
    """去掉 \\\\?\\ 前缀、把 WSL 的 /mnt/<盘>/ 换成盘符写法。只在 Windows 上动路径。"""
    if sys.platform != "win32" or not path:
        return path
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\") or path.startswith("//?/"):
        path = path[4:]
    m = _WSL_RE.match(path)
    if m:
        path = f"{m.group(1).upper()}:/{m.group(2) or ''}"
    return path


def _true_name(p: Path) -> str:
    """目录名取磁盘上的实际写法（大小写、8.3 短名展开成长名）；路径不存在或不在 Windows 上就原样用。
    只查这一层：GetLongPathNameW 对中间层的大小写不可靠（实测只修了部分层），FindFirstFileW 取到的是真名。"""
    name = _name_of(p)
    if sys.platform != "win32" or not p.name:
        return name.upper() if sys.platform == "win32" and len(name) == 1 else name  # 盘符根目录："c" 和 "C" 同一个
    try:
        import ctypes
        from ctypes import wintypes

        class _FindData(ctypes.Structure):
            _fields_ = [("attrs", wintypes.DWORD), ("ctime", wintypes.FILETIME), ("atime", wintypes.FILETIME),
                        ("wtime", wintypes.FILETIME), ("size_hi", wintypes.DWORD), ("size_lo", wintypes.DWORD),
                        ("r0", wintypes.DWORD), ("r1", wintypes.DWORD),
                        ("name", wintypes.WCHAR * 260), ("alt", wintypes.WCHAR * 14)]

        k32 = ctypes.windll.kernel32
        k32.FindFirstFileW.restype = ctypes.c_void_p
        k32.FindFirstFileW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_FindData)]
        k32.FindClose.argtypes = [ctypes.c_void_p]
        data = _FindData()
        h = k32.FindFirstFileW(str(p), ctypes.byref(data))
        if h and h != ctypes.c_void_p(-1).value:
            k32.FindClose(h)
            if data.name:
                return data.name
    except Exception:
        pass
    return name


def project_key(cwd: str, env: Mapping | None = None) -> str:
    env = os.environ if env is None else env
    forced = env.get("CLAUDE_PROJECT_DIR")
    if forced:
        return _true_name(Path(_normalize(forced)))
    start = Path(_normalize(cwd)) if cwd else Path.cwd()
    cur = start
    for _ in range(64):  # 防御：不会有这么深的目录
        try:
            if (cur / ".git").exists():  # 目录或文件（worktree）都算
                return _true_name(cur)
        except OSError:
            pass
        if cur.parent == cur:
            break
        cur = cur.parent
    return _true_name(start)

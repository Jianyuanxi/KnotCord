"""进程登记：server 和 drain 在 kv 里留一行 proc:<pid>，doctor 据此列出"现在活着的 KnotCord 进程"。

登记是"事实"不是"标志"（细则 6.4）：一行 = pid + 角色 + 最后心跳时间；是否活着现算 = 心跳够新且 pid 还在。
进程被强杀留下的行不影响任何功能，janitor 会清掉 pid 已不存在的行。

pid 存活检查只用 ctypes（OpenProcess + GetExitCodeProcess）。Windows 上不能用 os.kill(pid, 0)：
它会直接 TerminateProcess。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes as wt

from knotcord.core import db

PREFIX = "proc:"
BEAT_FRESH_S = 120  # 心跳超过这么久没更新，即使 pid 还在也不算活着（pid 可能被复用）

_QUERY_LIMITED = 0x1000
_STILL_ACTIVE = 259

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.GetExitCodeProcess.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.CloseHandle.restype = wt.BOOL


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":  # 只为在别的平台 import/测试时不崩；产品只跑在 Windows
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not h:
        # 拒绝访问说明进程在、只是我们没权限；其余（参数无效）说明不存在
        return ctypes.get_last_error() == 5
    try:
        code = wt.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def _key(pid: int) -> str:
    return f"{PREFIX}{pid}"


def register(conn, role: str) -> None:
    now = int(time.time())
    val = json.dumps({"pid": os.getpid(), "role": role, "started": now, "beat": now,
                      "parent": os.getppid()}, separators=(",", ":"))
    db.kv_set(conn, _key(os.getpid()), val)


def beat(conn, role: str) -> None:
    """更新心跳；行没了（被清掉）就重新登记。"""
    raw = db.kv_get(conn, _key(os.getpid()))
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        register(conn, role)
        return
    d["beat"] = int(time.time())
    db.kv_set(conn, _key(os.getpid()), json.dumps(d, separators=(",", ":")))


def unregister(conn) -> None:
    with db.write_tx(conn):
        conn.execute("DELETE FROM kv WHERE key=?", (_key(os.getpid()),))


def list_procs(conn, now: float | None = None) -> list[dict]:
    """全部登记行，附 alive（心跳新且 pid 在）。"""
    now = time.time() if now is None else now
    out = []
    for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE ?", (PREFIX + "%",)):
        try:
            d = json.loads(r["value"])
            pid = int(d["pid"])
        except (ValueError, KeyError, TypeError):
            continue
        d["alive"] = pid_alive(pid) and now - float(d.get("beat", 0)) <= BEAT_FRESH_S
        d["pid_exists"] = pid_alive(pid)
        out.append(d)
    out.sort(key=lambda x: x.get("started", 0))
    return out


def prune_dead(conn) -> int:
    """删掉 pid 已不存在的登记行。返回删除数。"""
    dead = [r["key"] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE ?", (PREFIX + "%",))
            if not _row_pid_alive(r["value"])]
    if dead:
        with db.write_tx(conn):
            conn.executemany("DELETE FROM kv WHERE key=?", [(k,) for k in dead])
    return len(dead)


def _row_pid_alive(value: str) -> bool:
    try:
        return pid_alive(int(json.loads(value)["pid"]))
    except (ValueError, KeyError, TypeError):
        return False

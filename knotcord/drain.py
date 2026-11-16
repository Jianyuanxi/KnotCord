"""一次性收尾进程（细则 1.5）：SessionEnd hook 用 pythonw 拉起，做完即退，最长 drain.max_minutes（10）分钟。

流程：抢租约 → 处理 pending → 对结束的会话（flush）和到期的会话提炼 → 清理 → 释放租约 → 退出。
拿不到租约：有活着的 server 持有（持有者进程还在）就在短暂宽限（grace_s）后直接退出；
持有者进程已经不在（被强杀留下的陈旧租约）就等到它过期，最多 wait_s。
自己带 KNOTCORD_INTERNAL=1（由 core.proc.child_env 设置），它和它的子进程都不会再触发 KnotCord 的 hook。
"""

from __future__ import annotations

import os
import threading
import time

from knotcord import janitor, procs, work
from knotcord.core import config, db, lease as _lease, log, proc

_log = log.get_logger("drain")

POLL_S = 2.0
GRACE_S = 6.0   # 持有者还活着时最多等这么久看它会不会释放（刚关的 server 正在走退出流程）
WAIT_S = 120.0  # 陈旧租约最多等这么久
HARD_EXIT_EXTRA_S = 15.0

# run() 的结果
LEASE_BUSY = "lease_busy"
DONE = "done"
TIMEOUT = "timeout"
NO_DB = "no_db"


def _owner_pid(owner: str) -> int:
    try:
        return int(owner.split(":", 1)[0])
    except (ValueError, IndexError):
        return 0


def _acquire(conn, lease, stop: threading.Event, grace_s: float, wait_s: float) -> bool:
    start = time.monotonic()
    while not stop.is_set():
        if lease.try_acquire():
            return True
        info = _lease.holder_info(conn)
        elapsed = time.monotonic() - start
        if info is not None and not info["expired"]:
            if procs.pid_alive(_owner_pid(info["owner"])):
                if elapsed >= grace_s:
                    return False  # 有活着的 server 在持有：它会处理积压
            elif elapsed >= wait_s:
                return False
        elif elapsed >= wait_s:
            return False
        stop.wait(0.1 if min(grace_s, wait_s) < POLL_S else POLL_S)  # 测试里缩小 grace_s/wait_s 时轮询也变密
    return False


def run(cfg=None, *, grace_s: float = GRACE_S, wait_s: float = WAIT_S, max_s: float | None = None,
        memroot: str | None = None) -> str:
    """返回 DONE / LEASE_BUSY / TIMEOUT / NO_DB。测试可以缩小 grace_s、wait_s、max_s。"""
    cfg = cfg or config.load()
    max_s = float(cfg.get("drain.max_minutes")) * 60 if max_s is None else max_s
    stop = threading.Event()
    timer = threading.Timer(max_s, stop.set)
    timer.daemon = True
    timer.start()
    conn = db.connect()
    lease = _lease.Lease(conn, ttl_s=float(cfg.get("lease.ttl_s")))
    got = False
    try:
        if db.is_newer_than_code(conn):
            _log.info("库结构比本进程新，drain 不动手")
            return LEASE_BUSY
        if not _acquire(conn, lease, stop, grace_s, wait_s):
            return TIMEOUT if stop.is_set() else LEASE_BUSY
        got = True
        procs.register(conn, "drain")
        _, ended = work.process_pending(conn)
        work.extract_pass(conn, cfg, lease, stop, flush_sids=sorted(ended))
        lease.renew()
        if not stop.is_set():
            janitor.run(conn, cfg, memroot=memroot)  # 自己看 kv 里的上次运行时间，不到间隔就跳过
        return TIMEOUT if stop.is_set() else DONE
    finally:
        timer.cancel()
        try:
            if got:
                procs.unregister(conn)
                lease.release()
        except Exception as e:
            _log.warning("drain 收尾失败: %s", e)
        proc.kill_all_jobs()
        conn.close()


def main() -> int:
    """入口：整个进程最长 max_s + 15 秒，到点硬退出（细则 1.5：自带 10 分钟定时器）。"""
    cfg = config.load()
    max_s = float(cfg.get("drain.max_minutes")) * 60
    hard = threading.Timer(max_s + HARD_EXIT_EXTRA_S, os._exit, (0,))
    hard.daemon = True
    hard.start()
    try:
        result = run(cfg, max_s=max_s)
        _log.info("drain 结束: %s", result)
    except Exception:
        _log.error("drain 失败", exc_info=True)
    return 0

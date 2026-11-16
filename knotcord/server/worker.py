"""worker 线程（细则 2.2）：每个 server 一个，串行做入库、提炼、清理。

- 队列项（mcp_tool hook 调 ingest 入队的）和 pending 表（command hook 写的）都在这里处理
- 启动时尝试抢租约；没抢到只做入库，每 lease.heartbeat_s 秒看一次：租约空了且有积压/清理到期就去抢
- 持有租约：续租、提炼到期的会话；启动 start_delay_s（60）秒后补做历史积压、跑清理
- 库结构版本比本进程认识的新：降级成只做检索（不入库、不提炼、不抢租约），细则 1.7
- 出错只记日志（日志自带去重）并指数退避，不让线程死掉；退出时自己回滚、释放租约、删本进程的临时目录
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time

from knotcord import janitor, procs, work
from knotcord.core import config, db, lease as _lease, log, paths
from knotcord.extract import backlog

_log = log.get_logger("worker")

DEFAULT_START_DELAY_S = 60
_BACKOFF_MAX_S = 60
_WAKE = object()


class Worker(threading.Thread):
    def __init__(self, stop_event: threading.Event, cfg=None, *, start_delay_s: float | None = None,
                 parent_pid: int | None = None) -> None:
        super().__init__(name="trace-worker", daemon=True)
        self.stop_event = stop_event
        self.cfg = cfg or config.load()
        self.start_delay_s = float(self.cfg.get("worker.start_delay_s", DEFAULT_START_DELAY_S)
                                   if start_delay_s is None else start_delay_s)
        self.parent_pid = parent_pid
        self.on_parent_gone = None  # 由 server 设置：发现父进程已不在时调用
        self._q: queue.Queue = queue.Queue()
        self.lease: _lease.Lease | None = None
        self.holding = False
        self.degraded = False
        self.ready = threading.Event()  # 连库、登记完成

    # 给主线程用：立刻返回
    def submit(self, sid: str, path: str | None = None, cwd: str | None = None, last: str | None = None) -> None:
        self._q.put((sid, path, cwd, last))

    def wake(self) -> None:
        """叫醒在等队列的 worker（退出时用，免得等满 1 秒的超时）。"""
        self._q.put(_WAKE)

    @property
    def lease_owner(self) -> str | None:
        return self.lease.owner if self.lease else None

    # ------------------------------------------------------------ 线程主体

    def run(self) -> None:
        conn = None
        try:
            conn = db.connect()
            self._loop(conn)
        except BaseException:
            _log.error("worker 异常退出", exc_info=True)
        finally:
            self._cleanup(conn)

    def _loop(self, conn) -> None:
        cfg, stop = self.cfg, self.stop_event
        hb = float(cfg.get("lease.heartbeat_s"))
        self.degraded = db.is_newer_than_code(conn)
        self.lease = _lease.Lease(conn, ttl_s=float(cfg.get("lease.ttl_s")))
        if self.degraded:
            _log.warning("库结构版本比本进程新，降级成只做检索")
        else:
            procs.register(conn, "server")
            self.holding = self.lease.try_acquire()
        self.ready.set()

        t0 = time.monotonic()
        last_beat = last_pending = last_extract = last_jan = t0
        backfilled = False
        ended: set[str] = set()
        fails = 0
        while not stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is _WAKE:
                continue
            if self.degraded:
                continue  # 队列项直接丢弃：transcript 还在磁盘上，新版进程会补读
            now = time.monotonic()
            try:
                if db.kv_get(conn, janitor.KEY_CORRUPT):
                    continue  # 细则 5.3：库损坏就停止一切写入
                did = False
                if item is not None:
                    sid, path, cwd, last = item
                    work.ingest_one(conn, sid, path, cwd, last)
                    did = True
                if did or now - last_pending >= 5:
                    _, e = work.process_pending(conn)
                    ended |= e
                    last_pending = now
                if now - last_beat >= hb:
                    last_beat = now
                    self._heartbeat(conn)
                if self.holding:
                    ready_bf = (not backfilled) and now - t0 >= self.start_delay_s
                    if did or ended or ready_bf or now - last_extract >= hb:
                        work.extract_pass(conn, cfg, self.lease, stop, flush_sids=sorted(ended),
                                          backfill=ready_bf)
                        ended.clear()
                        last_extract = now
                        backfilled = backfilled or ready_bf
                    if now - t0 >= self.start_delay_s and now - last_jan >= hb:
                        last_jan = now
                        if janitor.due(conn, cfg):
                            janitor.run(conn, cfg)
                fails = 0
            except Exception as e:
                fails += 1
                _log.warning("worker 一轮出错（第 %d 次连续）: %s: %s", fails, type(e).__name__, str(e)[:200])
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                stop.wait(min(_BACKOFF_MAX_S, 2 ** fails))

    def _heartbeat(self, conn) -> None:
        procs.beat(conn, "server")
        if self.holding:
            if not self.lease.renew():
                self.holding = False
                _log.warning("续租失败，租约已被别人接手")
        elif backlog.unbatched_stats(conn) or janitor.due(conn, self.cfg):
            self.holding = self.lease.try_acquire()
        if self.parent_pid and not procs.pid_alive(self.parent_pid):
            _log.warning("父进程 %s 已不在", self.parent_pid)
            if self.on_parent_gone:
                self.on_parent_gone()

    # ------------------------------------------------------------ 退出

    def _cleanup(self, conn) -> None:
        """退出流程里 worker 自己的部分：回滚没提交的事务 → 删本进程的临时目录 → 释放租约 → 关库。"""
        try:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
        except Exception:
            pass
        remove_own_tmp()
        if conn is not None:
            try:
                if self.lease is not None and self.holding:
                    self.lease.release()
                    self.holding = False
                procs.unregister(conn)
            except Exception as e:
                _log.warning("worker 退出时释放租约失败: %s", e)
            try:
                conn.close()
            except Exception:
                pass


def remove_own_tmp() -> None:
    """删 tmp 下以 -<本进程 pid> 结尾的批目录（提炼被中途中止时 finally 没来得及的兜底）。"""
    try:
        suffix = f"-{os.getpid()}"
        for p in paths.tmp_dir().iterdir():
            if p.name.endswith(suffix):
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass

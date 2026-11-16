"""stdio MCP server 兼 worker（细则 1.2、2.2、6.3）。

- 工具：search / timeline / get / usage / ingest。主线程（asyncio）只跑协议；检索在 to_thread 里用自己的连接；
  ingest 只入队、立即返回空串，读 transcript 在 worker 线程做。
- stdout 只许出现协议消息：启动时把真正的 fd 1 复制一份留给协议层，fd 1 本身指向空设备，
  sys.stdout 换成写日志的对象，任何依赖库乱 print 或 C 层写 stdout 都进不了协议流。
- 退出：stdin EOF / 父进程死亡 / SIGTERM、CTRL_BREAK 任一发生 → 置停止标志 → worker 自己收尾
  （回滚、删本进程临时目录、释放租约、关库）→ kill_all_jobs → 退出；3 秒硬期限，到点 os._exit(0)。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import threading
import time
from io import TextIOWrapper

from knotcord import __version__
from knotcord.core import config, db, lease as _lease, log, proc
from knotcord.server.worker import Worker, remove_own_tmp

_log = log.get_logger("server")

HARD_DEADLINE_S = 3.0
JOIN_S = 1.5  # 等 worker 自己收尾；之后无论如何都往下走

_PLACEHOLDER = re.compile(r"^\$\{[^}]*\}$")  # hook 的 ${xxx} 没被替换时原样传过来，当作没传


# ---------------------------------------------------------------- stdout 保护

class _LogStream:
    """替换 sys.stdout：写进来的东西按行进日志。"""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self) -> None:
        self._buf = ""

    def write(self, s) -> int:
        if isinstance(s, (bytes, bytearray)):
            s = bytes(s).decode("utf-8", "replace")
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                _log.info("[stdout] %s", line[:500])
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("stdout 已被 KnotCord 接管")

    @property
    def buffer(self):
        return self


def protect_stdout():
    """返回协议层专用的二进制输出流；之后 fd 1 和 sys.stdout 都不再通向真正的 stdout。"""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        proto = os.fdopen(os.dup(1), "wb", buffering=0)
    except OSError:
        proto = sys.stdout.buffer  # fd 1 本来就是坏的：退回，至少协议能走
    else:
        try:
            nul = os.open(os.devnull, os.O_WRONLY)
            os.dup2(nul, 1)
            os.close(nul)
        except OSError:
            pass
    sys.stdout = _LogStream()
    return proto


# ---------------------------------------------------------------- 工具实现（同步，在线程里跑）

_REL = re.compile(r"^(\d+)\s*([hdw])$")
_UNIT = {"h": 3600, "d": 86400, "w": 7 * 86400}


def parse_time(v, *, end: bool = False) -> int | None:
    """since/until：epoch 秒、'7d'/'12h'/'2w'（多久以前）、'2026-11-01'、'2026-11-01 08:30'。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    m = _REL.match(s.lower())
    if m:
        return int(time.time()) - int(m.group(1)) * _UNIT[m.group(2)]
    for fmt, whole_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            t = int(time.mktime(time.strptime(s, fmt)))
            return t + 86400 - 1 if (whole_day and end) else t
        except ValueError:
            continue
    raise ValueError(f"时间格式不对: {s}（可用 7d、12h、2026-11-01、2026-11-01 08:30 或 epoch 秒）")


def _open(readonly: bool):
    try:
        return db.connect(readonly=readonly)
    except FileNotFoundError:
        return None


def _current_project() -> str | None:
    try:
        from knotcord.core import project
        return project.project_key(os.getcwd())
    except Exception:
        return None


def tool_search(args: dict) -> str:
    from knotcord.search import api
    conn = _open(True)
    if conn is None:
        return "无结果"
    try:
        return api.search(
            conn, str(args.get("query") or ""), project=args.get("project") or None,
            current_project=_current_project(), since=parse_time(args.get("since")),
            until=parse_time(args.get("until"), end=True), limit=int(args.get("limit") or 10),
            include_superseded=bool(args.get("include_superseded", False)))
    finally:
        conn.close()


def tool_timeline(args: dict) -> str:
    from knotcord.search import api
    conn = _open(True)
    if conn is None:
        return "无结果"
    try:
        return api.timeline(conn, str(args.get("id") or ""), before=int(args.get("before", 3)),
                            after=int(args.get("after", 3)))
    finally:
        conn.close()


_SRC_OF = {"n": "note", "u": "note", "s": "summary", "p": "prompt", "m": "memfile"}
_ID = re.compile(r"^([nuspme])(\d+)$")


def tool_get(args: dict) -> str:
    """取全文，并给 docs 记 use_count（api.get 已加）和 last_used（这里补；api.get 只写 notes.last_used）。"""
    from knotcord.search import api
    conn = _open(False)  # 要写取用计数，所以是读写连接
    if conn is None:
        return "无结果"
    try:
        ids = args.get("ids")
        text = api.get(conn, ids if ids is not None else [], with_source=bool(args.get("with_source", False)))
        flat = [t for x in ([ids] if isinstance(ids, str) else (ids or [])) for t in re.split(r"[,\s]+", str(x)) if t]
        now = int(time.time())
        try:
            with db.write_tx(conn):
                for raw in flat:
                    m = _ID.match(raw.strip().lower())
                    if m and m.group(1) in _SRC_OF:
                        conn.execute("UPDATE docs SET last_used=? WHERE src=? AND src_id=?",
                                     (now, _SRC_OF[m.group(1)], int(m.group(2))))
        except Exception as e:
            _log.warning("写 docs.last_used 失败: %s", e)
        return text
    finally:
        conn.close()


def tool_usage(args: dict) -> str:
    from knotcord.extract import budget
    conn = _open(True)
    if conn is None:
        return "尚无数据库"
    try:
        return budget.usage_line(conn, config.load())
    finally:
        conn.close()


def _clean(v) -> str | None:
    if not isinstance(v, str) or not v.strip() or _PLACEHOLDER.match(v.strip()):
        return None
    return v


# ---------------------------------------------------------------- 工具声明

def _tools(types):
    s = {"type": "string"}
    return [
        types.Tool(
            name="search",
            description="搜记忆（KnotCord 笔记、总结、提示、内置记忆文件）。每条一行：编号 日期 项目 标题。"
                        "编号前缀 n 笔记 u 没做完 s 总结 p 提示 m 记忆文件。下一步用 timeline 或 get。",
            inputSchema={"type": "object", "properties": {
                "query": {**s, "description": "关键词，空格分隔（都要命中，结果少会放宽）"},
                "project": {**s, "description": "项目名，默认全部（当前项目加分）"},
                "since": {**s, "description": "起始时间：7d、12h、2026-11-01"},
                "until": {**s, "description": "截止时间"},
                "limit": {"type": "integer", "description": "条数，默认 10"},
                "include_superseded": {"type": "boolean", "description": "含已被取代的旧笔记"},
            }, "required": ["query"]}),
        types.Tool(
            name="timeline",
            description="看某条前后同一会话里的提示、笔记、操作。参数 id 是 search 返回的编号。",
            inputSchema={"type": "object", "properties": {
                "id": s, "before": {"type": "integer"}, "after": {"type": "integer"}}, "required": ["id"]}),
        types.Tool(
            name="get",
            description="按编号取全文（可批量）。m 开头的返回文件路径，正文自己 Read。with_source=true 再附原文精简版。",
            inputSchema={"type": "object", "properties": {
                "ids": {"anyOf": [s, {"type": "array", "items": s}]},
                "with_source": {"type": "boolean"}}, "required": ["ids"]}),
        types.Tool(
            name="usage",
            description="今天的提炼次数、花费、上限和积压。",
            inputSchema={"type": "object", "properties": {}}),
        types.Tool(
            name="ingest",
            description="内部用（hook 调）：通知读取某会话的 transcript 增量。立即返回。",
            inputSchema={"type": "object", "properties": {
                "session_id": s, "transcript_path": s, "cwd": s, "last": s}, "required": ["session_id"]}),
    ]


_SYNC = {"search": tool_search, "timeline": tool_timeline, "get": tool_get, "usage": tool_usage}


def build_server(worker: Worker):
    from mcp.server.lowlevel import Server
    import mcp.types as types

    server = Server("knotcord", version=__version__)
    tools = _tools(types)

    @server.list_tools()
    async def _list_tools():
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        arguments = arguments or {}
        if name == "ingest":  # 5 ms 内返回：只入队
            sid = _clean(arguments.get("session_id"))
            if sid:
                worker.submit(sid, _clean(arguments.get("transcript_path")), _clean(arguments.get("cwd")),
                              _clean(arguments.get("last")))
            return [types.TextContent(type="text", text="")]
        fn = _SYNC.get(name)
        if fn is None:
            raise ValueError(f"没有这个工具: {name}")
        text = await asyncio.to_thread(fn, arguments)
        return [types.TextContent(type="text", text=text)]

    return server


# ---------------------------------------------------------------- 退出流程

class Shutdown:
    """只执行一次的退出流程，最后 os._exit(0)。"""

    def __init__(self, stop: threading.Event, worker: Worker) -> None:
        self.stop = stop
        self.worker = worker
        self._lock = threading.Lock()
        self._started = False

    def __call__(self, reason: str) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        t = threading.Timer(HARD_DEADLINE_S, os._exit, (0,))  # 3 秒硬期限
        t.daemon = True
        t.start()
        try:
            _log.info("server 退出: %s", reason)
            self.stop.set()
            self.worker.wake()
            if self.worker.is_alive():
                # 先让 worker 自己看到 stop_event、中止正在跑的模型调用，再兜底杀 Job：
                # 直接杀 Job 会让 codex 以非零码退出，被误判成"通道不可用"而进入冷却
                self.worker.join(JOIN_S)
            proc.kill_all_jobs()
            if self.worker.is_alive():
                self._release_lease_fallback()
            remove_own_tmp()
        except Exception:
            _log.error("退出流程出错", exc_info=True)
        finally:
            os._exit(0)

    def _release_lease_fallback(self) -> None:
        owner = self.worker.lease_owner
        if not owner:
            return
        try:
            conn = db.connect(busy_timeout_ms=500, retry=False)
            try:
                _lease.Lease(conn, owner=owner).release()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("兜底释放租约失败: %s", e)


def _install_signals(shutdown: Shutdown) -> None:
    def handler(signum, frame):
        threading.Thread(target=shutdown, args=(f"收到信号 {signum}",), daemon=True).start()

    for name in ("SIGTERM", "SIGBREAK", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------- 入口

async def _serve(server, proto_out) -> None:
    import anyio
    from mcp.server.stdio import stdio_server
    out = anyio.wrap_file(TextIOWrapper(proto_out, encoding="utf-8", newline="\n", write_through=True))
    async with stdio_server(stdout=out) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    proto_out = protect_stdout()
    cfg = config.load()
    stop = threading.Event()
    worker = Worker(stop, cfg, parent_pid=os.getppid())
    shutdown = Shutdown(stop, worker)
    worker.on_parent_gone = lambda: shutdown("父进程已不在（心跳检查）")
    _install_signals(shutdown)
    try:
        proc.watch_parent(lambda: shutdown("父进程已退出"))
    except Exception as e:
        _log.warning("父进程看门狗没起来，退出只靠 stdin EOF: %s", e)
    worker.start()
    try:
        asyncio.run(_serve(build_server(worker), proto_out))
        shutdown("stdin 已关闭")
    except BaseException:
        _log.error("server 异常", exc_info=True)
        shutdown("异常")
    return 0

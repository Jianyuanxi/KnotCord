"""hook 统一入口（细则 6.3 的六条模板）。

用法：python -I -S -X utf8 <插件根>\\knotcord\\hooks\\entry.py <session-start|prompt|session-end|stop|pre-compact>
stdin 是 Claude Code 给的 JSON。任何情况下都 exit 0；只许 import 标准库和 knotcord.core / knotcord.inject。
"""

import os
import sys

# 模板第 1 条：第一行检查 KNOTCORD_INTERNAL（提炼子进程、drain 自己都带它，防止递归）
if __name__ == "__main__" and os.environ.get("KNOTCORD_INTERNAL"):
    os._exit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KILL_AFTER_S = 1.0          # 模板第 3 条：1 秒自杀定时器
DB_BUSY_MS = 400            # 写 pending 时撞锁最多等这么久；SessionStart 是 300
LAST_MSG_MAX = 20_000       # kv 里存的 last_assistant_message 上限


def _log_error(what: str) -> None:
    try:
        from knotcord.core import log
        log.get_logger("hook").error("%s", what, exc_info=True)
    except Exception:
        pass


def _read_input() -> dict:
    # 模板第 5 条：按字节读再按 UTF-8 解码，路径原样用
    import json
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _session_start(data: dict) -> None:
    if data.get("source") == "resume":  # 保住提示缓存：resume 不注入
        return
    from knotcord.core import db, project
    sid = data.get("session_id")
    proj = project.project_key(data.get("cwd") or "")
    try:
        conn = db.connect(readonly=True, busy_timeout_ms=300)
    except FileNotFoundError:
        return  # 库还没建：没有记忆，什么都不注入
    try:
        from knotcord import inject
        text = inject.read_for_hook(conn, proj, exclude_session=sid if isinstance(sid, str) else None)
    finally:
        conn.close()
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def _prompt(data: dict) -> None:
    """UserPromptSubmit：提问前自动召回。默认关闭——关着时只读配置文件就退出，不 import sqlite3、不连库。"""
    from knotcord.core import config
    cfg = config.load()
    if not cfg.get("recall.enabled", False):
        return
    from knotcord import recall
    text = recall.run(data, cfg)
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def _enqueue(data: dict, reason: str, *, keep_last_msg: bool = False) -> None:
    from knotcord.core import db
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    path = data.get("transcript_path")
    # retry=False：撞锁只等 busy_timeout（0.4 秒）一次，等不到就放弃，由外层记日志；
    # 带退避的重试会超过 hook 的 1 秒上限
    conn = db.connect(busy_timeout_ms=DB_BUSY_MS, retry=False)
    try:
        import time
        with db.write_tx(conn, retry=False):
            conn.execute(
                "INSERT INTO pending(session_id, client, transcript_path, reason, enqueued_at) VALUES(?,?,?,?,?)",
                (sid, "claude", path if isinstance(path, str) else None, reason, int(time.time())))
            msg = data.get("last_assistant_message")
            if keep_last_msg and isinstance(msg, str) and msg.strip():
                from knotcord.core.redact import redact
                if len(msg) > LAST_MSG_MAX:
                    msg = msg[:15_000] + "\n…\n" + msg[-5_000:]
                conn.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"last_msg:{sid}", redact(msg)))
    finally:
        conn.close()


def _drain_cmd() -> list:
    import json
    override = os.environ.get("KNOTCORD_DRAIN_CMD")  # 测试用：整体替换拉起的命令
    if override:
        cmd = json.loads(override)
        if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
            return cmd
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    return [exe, "-I", "-X", "utf8", os.path.join(ROOT, "knotcord", "boot.py"), "drain"]


def _session_end(data: dict) -> None:
    try:
        _enqueue(data, "end")
    except Exception:
        _log_error("session-end 写 pending 失败")
    try:  # 召回的会话内去重状态文件：会话结束就删（没开召回时文件不存在，unlink 失败即忽略）
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            from knotcord import recall
            recall.forget(sid)
    except Exception:
        _log_error("session-end 清理召回状态失败")
    from knotcord.core import proc
    proc.spawn_detached(_drain_cmd())  # 拉起后不等待；失败由外层记日志


def _stop(data: dict) -> None:
    _enqueue(data, "stop", keep_last_msg=True)  # mcp_tool hook 不可用时的备用路径


def _pre_compact(data: dict) -> None:
    _enqueue(data, "compact")


_EVENTS = {
    "session-start": _session_start,
    "prompt": _prompt,
    "session-end": _session_end,
    "stop": _stop,
    "pre-compact": _pre_compact,
}


def main(argv=None) -> int:
    if os.environ.get("KNOTCORD_INTERNAL"):
        return 0
    import threading
    timer = threading.Timer(KILL_AFTER_S, os._exit, (0,))
    timer.daemon = True
    timer.start()
    try:
        # 模板第 2 条：-I 会忽略编码环境变量，输出编码自己设
        try:
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # 不让 Windows 把 \n 写成 \r\n
        except Exception:
            pass
        argv = sys.argv[1:] if argv is None else argv
        handler = _EVENTS.get(argv[0]) if argv else None
        if handler is None:
            return 0
        handler(_read_input())
    except Exception:
        _log_error("hook %s 失败" % (argv[0] if argv else "?"))  # 模板第 4 条：只写日志，不让 Claude Code 看到报错
    return 0


if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)

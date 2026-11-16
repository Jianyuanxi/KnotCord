"""server / drain / cli 测试共用：真起 boot.py serve 子进程，手写 JSON-RPC（一行一条）。

所有子进程都带 KNOTCORD_HOME（临时目录）；config.toml 里 budget.daily_calls=0，保证绝不调模型。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT = ROOT / "knotcord" / "boot.py"
CREATE_NO_WINDOW = 0x08000000


def write_config(home: Path, *, daily_calls: int = 0, start_delay: float = 0) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f"[budget]\ndaily_calls = {daily_calls}\n[worker]\nstart_delay_s = {start_delay}\n", encoding="utf-8")


def child_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KNOTCORD_")}
    env["KNOTCORD_HOME"] = str(home)
    return env


def serve_cmd() -> list[str]:
    return [sys.executable, "-I", "-X", "utf8", str(BOOT), "serve"]


def pid_alive(pid: int) -> bool:
    from knotcord import procs
    return procs.pid_alive(pid)


def wait_until(pred, timeout: float, step: float = 0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def lease_holder_pid(home: Path) -> int | None:
    """当前未过期租约的持有者 pid；库不在、没人持有返回 None。"""
    from knotcord.core import db, lease
    old = os.environ.get("KNOTCORD_HOME")
    os.environ["KNOTCORD_HOME"] = str(home)
    try:
        try:
            conn = db.connect(readonly=True)
        except FileNotFoundError:
            return None
        try:
            info = lease.holder_info(conn)
        finally:
            conn.close()
    finally:
        if old is None:
            os.environ.pop("KNOTCORD_HOME", None)
        else:
            os.environ["KNOTCORD_HOME"] = old
    if info is None or info["expired"]:
        return None
    return int(info["owner"].split(":", 1)[0])


class Server:
    def __init__(self, home: Path, stderr_path: Path | None = None) -> None:
        self.home = home
        self.err = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(serve_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
                                  env=child_env(home), creationflags=CREATE_NO_WINDOW)
        self.lines: queue.Queue = queue.Queue()
        self.raw: list[bytes] = []
        threading.Thread(target=self._read, daemon=True).start()
        self._id = 0

    def _read(self) -> None:
        for line in self.p.stdout:
            self.raw.append(line)
            self.lines.put(line)
        self.lines.put(None)

    def send(self, obj: dict) -> None:
        self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def rpc(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self._id += 1
        rid = self._id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method} 没有响应")
            line = self.lines.get(timeout=left)
            if line is None:
                raise EOFError("server 已关闭 stdout")
            msg = json.loads(line)  # 协议流里出现非 JSON 行会在这里炸，正是要测的
            if msg.get("id") == rid:
                return msg

    def handshake(self) -> dict:
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "pytest", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def call(self, tool: str, args: dict | None = None, timeout: float = 20) -> str:
        r = self.rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        res = r["result"]
        assert not res.get("isError"), res
        return "".join(c.get("text", "") for c in res["content"])

    def close(self, wait: float = 10) -> float:
        """关 stdin 并等退出，返回耗时秒数；超时强杀并抛错。"""
        t0 = time.monotonic()
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.p.kill()
            raise
        return time.monotonic() - t0

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait(timeout=10)
        if self.err is not subprocess.DEVNULL:
            self.err.close()


def make_transcript(path: Path, sid: str, cwd: str, turns: int = 2, text: str = "字", t0: int = 1_789_000_000) -> None:
    """最小可被 ingest 读入的 transcript：每回合一条提示 + 一条回答。"""
    t = t0
    out = []
    for i in range(turns):
        for typ, role in (("user", "user"), ("assistant", "assistant")):
            t += 5
            msg = ({"role": "user", "content": f"{text * 40} 提示{i}"} if typ == "user" else
                   {"model": "claude-opus-5", "id": f"m{i}", "role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": f"{text * 40} 回答{i}"}]})
            out.append(json.dumps({
                "parentUuid": None, "isSidechain": False, "type": typ, "uuid": f"{sid}-{i}-{typ}",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t)), "cwd": cwd,
                "sessionId": sid, "message": msg}, ensure_ascii=False))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

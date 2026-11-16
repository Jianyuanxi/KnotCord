"""父进程被杀（Claude Code 崩溃或被强杀）时，server 自己退出、释放租约、不留孤儿。

stdin 管道的写端留在测试进程里，所以 server 永远不会读到 EOF：退出只能来自父进程看门狗（core.proc.watch_parent）。
"""

import json
import os
import subprocess
import sys
import time

import pytest

import srvharness as H
from knotcord.core import db

WRAPPER = """\
import json, subprocess, sys, time
cmd = json.loads(sys.argv[1])
p = subprocess.Popen(cmd, stdin=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
open(sys.argv[2], "w").write(str(p.pid))
time.sleep(600)
"""


def test_server_exits_after_parent_killed(trace_home, tmp_path):
    H.write_config(trace_home)
    wrap = tmp_path / "wrapper_main.py"
    wrap.write_text(WRAPPER, encoding="utf-8")
    pidfile = tmp_path / "server.pid"
    r, w = os.pipe()
    wrapper = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(wrap), json.dumps(H.serve_cmd()), str(pidfile)],
                               stdin=r, env=H.child_env(trace_home), creationflags=H.CREATE_NO_WINDOW)
    os.close(r)
    server_pid = None
    try:
        assert H.wait_until(pidfile.exists, 15), "wrapper 没有起 server"
        server_pid = int(pidfile.read_text())
        assert H.wait_until(lambda: H.lease_holder_pid(trace_home) == server_pid, 20), "server 没抢到租约"
        wrapper.kill()
        wrapper.wait(timeout=10)
        t0 = time.monotonic()
        assert H.wait_until(lambda: not H.pid_alive(server_pid), 10), "父进程死后 server 没有自己退出"
        took = time.monotonic() - t0
        assert took < 8, f"父进程死后 {took:.1f} 秒才退出"
        assert H.lease_holder_pid(trace_home) is None
        c = db.connect(readonly=True)
        try:
            assert c.execute("SELECT count(*) FROM kv WHERE key LIKE 'proc:%'").fetchone()[0] == 0
        finally:
            c.close()
    finally:
        os.close(w)
        if wrapper.poll() is None:
            wrapper.kill()
        if server_pid and H.pid_alive(server_pid):  # 失败时别留进程
            subprocess.run(["taskkill", "/F", "/PID", str(server_pid)], capture_output=True,
                           creationflags=H.CREATE_NO_WINDOW)

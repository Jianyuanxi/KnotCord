import ctypes
import os
import subprocess
import sys
import textwrap
import threading
import time
from ctypes import wintypes as wt

import pytest

from knotcord.core import proc

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="只支持 Windows")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
_k32.CloseHandle.argtypes = [wt.HANDLE]


def alive(pid: int) -> bool:
    h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False  # 打不开 = 不存在
    try:
        code = wt.DWORD()
        _k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def wait_dead(pids, secs=5.0) -> list[int]:
    end = time.time() + secs
    left = list(pids)
    while left and time.time() < end:
        left = [p for p in left if alive(p)]
        if left:
            time.sleep(0.1)
    return left


PARENT = textwrap.dedent("""
    import os, subprocess, sys, time
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1], "w") as f:
        f.write(f"{os.getpid()} {g.pid}")
    time.sleep(120)
""")


def _pids(path) -> list[int]:
    end = time.time() + 10
    while time.time() < end:
        try:
            text = path.read_text(encoding="utf-8")
            if len(text.split()) == 2:
                return [int(x) for x in text.split()]
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("子进程没写出 pid 文件")


def test_timeout_kills_child_and_grandchild(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    t0 = time.time()
    with pytest.raises(proc.ChildTimeout):
        proc.run_child([sys.executable, str(script), str(pidfile)], stdin_text=None, cwd=str(tmp_path),
                       env=proc.child_env(), timeout_s=3,
                       stdout_path=str(tmp_path / "o.txt"), stderr_path=str(tmp_path / "e.txt"))
    assert time.time() - t0 < 15
    pids = _pids(pidfile)
    assert wait_dead(pids) == [], "子进程或孙进程还活着"


def test_stop_event_kills_tree(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    stop = threading.Event()
    result = {}

    def go():
        result["rc"] = proc.run_child([sys.executable, str(script), str(pidfile)], stdin_text=None,
                                      cwd=str(tmp_path), env=proc.child_env(), timeout_s=60,
                                      stdout_path=str(tmp_path / "o.txt"), stderr_path=str(tmp_path / "e.txt"),
                                      stop_event=stop)

    th = threading.Thread(target=go)
    th.start()
    pids = _pids(pidfile)
    stop.set()
    th.join(10)
    assert not th.is_alive()
    assert result["rc"] == -1
    assert wait_dead(pids) == []


def test_kill_all_jobs(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    th = threading.Thread(target=lambda: proc.run_child(
        [sys.executable, str(script), str(pidfile)], stdin_text=None, cwd=str(tmp_path),
        env=proc.child_env(), timeout_s=60,
        stdout_path=str(tmp_path / "o.txt"), stderr_path=str(tmp_path / "e.txt")))
    th.start()
    pids = _pids(pidfile)
    proc.kill_all_jobs()
    th.join(10)
    assert not th.is_alive()
    assert wait_dead(pids) == []


def test_normal_run_stdin_stdout_files_and_ticks(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "TICK_S", 0.3)
    script = tmp_path / "echo.py"
    script.write_text(textwrap.dedent("""
        import sys, time
        sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
        data = sys.stdin.read()
        time.sleep(1.2)
        sys.stdout.write(data.upper()); sys.stderr.write("err!")
        sys.exit(7)
    """), encoding="utf-8")
    ticks = []
    big = "你好 hello " * 50000  # 远大于管道缓冲，验证写 stdin 不会卡死
    rc = proc.run_child([sys.executable, str(script)], stdin_text=big, cwd=str(tmp_path),
                        env=proc.child_env(), timeout_s=30,
                        stdout_path=str(tmp_path / "sub" / "o.txt"), stderr_path=str(tmp_path / "sub" / "e.txt"),
                        on_tick=lambda: ticks.append(1))
    assert rc == 7
    assert (tmp_path / "sub" / "o.txt").read_text(encoding="utf-8") == big.upper()
    assert (tmp_path / "sub" / "e.txt").read_text(encoding="utf-8") == "err!"
    assert len(ticks) >= 2


def test_on_tick_exception_kills_child(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "TICK_S", 0.2)
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"

    def bad():
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        proc.run_child([sys.executable, str(script), str(pidfile)], stdin_text=None, cwd=str(tmp_path),
                       env=proc.child_env(), timeout_s=60,
                       stdout_path=str(tmp_path / "o.txt"), stderr_path=str(tmp_path / "e.txt"), on_tick=bad)
    assert wait_dead(_pids(pidfile)) == []


def test_child_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CLAUDE_CODE_FOO", "x")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "x")
    monkeypatch.setenv("MY_SECRET", "x")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("CODEX_HOME", "C:/codex")
    env = proc.child_env({"DEEPSEEK_API_KEY": "k"})
    assert not [k for k in env if k.upper().startswith(("ANTHROPIC_", "CLAUDE_"))]
    assert env["KNOTCORD_INTERNAL"] == "1"
    assert "MY_SECRET" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:1"
    assert env["CODEX_HOME"] == "C:/codex"
    assert env["DEEPSEEK_API_KEY"] == "k"
    assert any(k.upper() == "SYSTEMROOT" for k in env)
    assert any(k.upper() == "PATH" for k in env)


def test_spawn_detached_runs_and_does_not_wait(tmp_path):
    marker = tmp_path / "marker.txt"
    script = tmp_path / "late.py"
    script.write_text(textwrap.dedent("""
        import os, sys, time
        time.sleep(1.5)
        open(sys.argv[1], "w", encoding="utf-8").write(os.environ.get("KNOTCORD_INTERNAL", "") + "|" + str(os.getpid()))
    """), encoding="utf-8")
    t0 = time.time()
    proc.spawn_detached([sys.executable, str(script), str(marker)])
    assert time.time() - t0 < 1.0  # 不等待
    end = time.time() + 15
    while time.time() < end and not marker.exists():
        time.sleep(0.1)
    assert marker.read_text(encoding="utf-8").startswith("1|")  # drain 自己带 KNOTCORD_INTERNAL=1


WATCH_CHILD = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from knotcord.core import proc
    marker = sys.argv[2]
    def dead():
        open(marker, "w").write("dead")
        os._exit(0)
    proc.watch_parent(dead)
    open(sys.argv[3], "w").write(str(os.getpid()))
    time.sleep(60)
""")
WATCH_PARENT = textwrap.dedent("""
    import subprocess, sys, time
    subprocess.Popen([sys.executable] + sys.argv[1:])
    time.sleep(60)
""")


def test_watch_parent_fires_when_parent_killed(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (tmp_path / "wc.py").write_text(WATCH_CHILD, encoding="utf-8")
    (tmp_path / "wp.py").write_text(WATCH_PARENT, encoding="utf-8")
    marker, pidfile = tmp_path / "marker.txt", tmp_path / "child.pid"
    parent = subprocess.Popen([sys.executable, str(tmp_path / "wp.py"), str(tmp_path / "wc.py"),
                               root, str(marker), str(pidfile)])
    try:
        end = time.time() + 15
        while time.time() < end and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists()
        time.sleep(0.3)  # 等 pidfile 内容写完
        child_pid = int(pidfile.read_text())
        assert not marker.exists()
        parent.kill()
        end = time.time() + 10
        while time.time() < end and not marker.exists():
            time.sleep(0.05)
        assert marker.exists() and marker.read_text() == "dead"
    finally:
        parent.kill()
        wait_dead([child_pid]) if "child_pid" in locals() else None

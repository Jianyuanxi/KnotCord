"""子进程与 Job Object（细则 1.2、1.4、1.5）。整个项目只有这里能起子进程。

只用 ctypes 和标准库。Windows 专用；其他平台 import 不报错，但调用会 NotImplementedError。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes as wt

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

TICK_S = 5.0  # run_child 等待期间 on_tick 的间隔（续租约用）

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_INFINITE = 0xFFFFFFFF
_ERROR_INVALID_PARAMETER = 87

_IS_WIN = sys.platform == "win32"


class ChildTimeout(Exception):
    """子进程超时，已终止整个 Job。"""


if _IS_WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class _EXT_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # 64 位下句柄是 8 字节，每个函数都要声明 argtypes/restype，否则会被截断
    _k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
    _k32.CreateJobObjectW.restype = wt.HANDLE
    _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
    _k32.SetInformationJobObject.restype = wt.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _k32.AssignProcessToJobObject.restype = wt.BOOL
    _k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    _k32.TerminateJobObject.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    _k32.WaitForSingleObject.restype = wt.DWORD
    _k32.GetCurrentProcess.argtypes = []
    _k32.GetCurrentProcess.restype = wt.HANDLE
    _k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                                     ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME)]
    _k32.GetProcessTimes.restype = wt.BOOL


def _need_win() -> None:
    if not _IS_WIN:
        raise NotImplementedError("core.proc 只支持 Windows")


# ---------------------------------------------------------------- Job Object

_jobs: set[int] = set()
_jobs_lock = threading.Lock()


def _new_job() -> int | None:
    h = _k32.CreateJobObjectW(None, None)
    if not h:
        return None
    info = _EXT_LIMIT()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32.SetInformationJobObject(h, _JobObjectExtendedLimitInformation,
                                        ctypes.byref(info), ctypes.sizeof(info)):
        _k32.CloseHandle(h)
        return None
    with _jobs_lock:
        _jobs.add(h)
    return h


def _assign(job: int, pid: int) -> bool:
    hp = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not hp:
        return False
    try:
        return bool(_k32.AssignProcessToJobObject(job, hp))
    finally:
        _k32.CloseHandle(hp)


def _terminate_job(job: int) -> None:
    _k32.TerminateJobObject(job, 1)


def _close_job(job: int) -> None:
    with _jobs_lock:
        if job not in _jobs:
            return
        _jobs.discard(job)
    _k32.CloseHandle(job)  # KILL_ON_JOB_CLOSE：还剩的孙进程在这里被一起带走


def kill_all_jobs() -> None:
    """进程退出流程里用：终止本进程创建的所有 Job。"""
    if not _IS_WIN:
        return
    with _jobs_lock:
        current = list(_jobs)
    for h in current:
        _terminate_job(h)


def _kill_tree(proc: subprocess.Popen, job: int | None) -> None:
    if job is not None:
        _terminate_job(job)
        return
    # 没能进 Job（极少见）：退回 taskkill /T
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], creationflags=CREATE_NO_WINDOW,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:
        proc.kill()


def run_child(args: list[str], *, stdin_text: str | None, cwd: str, env: dict, timeout_s: float,
              stdout_path: str, stderr_path: str, stop_event: threading.Event | None = None,
              on_tick: Callable[[], None] | None = None) -> int:
    """跑一个子进程并放进 Job Object，返回退出码。

    - stdout/stderr 写文件，不用管道（避免写满卡死）；stdin 写完立刻关闭。
    - 等待时每 TICK_S 秒调一次 on_tick（续租约）；on_tick 抛异常就终止 Job 并把异常抛出去。
    - 超时：终止整个 Job，抛 ChildTimeout。stop_event 置位：终止整个 Job，返回 -1。
    注意：进程是先创建、再放进 Job 的，两步之间有毫秒级窗口；子进程要先启动解释器才会派生孙进程，实际踩不到。
    """
    _need_win()
    os.makedirs(os.path.dirname(os.path.abspath(stdout_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(stderr_path)), exist_ok=True)
    fout = open(stdout_path, "wb")
    ferr = open(stderr_path, "wb")
    job = None
    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=fout, stderr=ferr, cwd=cwd, env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        job = _new_job()
        if job is not None and not _assign(job, proc.pid):
            _close_job(job)
            job = None
        if stdin_text is not None:
            data = stdin_text.encode("utf-8")

            def _feed() -> None:
                try:
                    proc.stdin.write(data)
                except OSError:
                    pass  # 子进程提前退出，管道已断
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            threading.Thread(target=_feed, daemon=True, name="knotcord-child-stdin").start()

        deadline = time.monotonic() + timeout_s
        last_tick = time.monotonic()
        while True:
            try:
                return proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if stop_event is not None and stop_event.is_set():
                _kill_tree(proc, job)
                _reap(proc)
                return -1
            if now >= deadline:
                _kill_tree(proc, job)
                _reap(proc)
                raise ChildTimeout(f"子进程超过 {timeout_s} 秒未结束，已终止")
            if on_tick is not None and now - last_tick >= TICK_S:
                last_tick = now
                try:
                    on_tick()
                except BaseException:
                    _kill_tree(proc, job)
                    _reap(proc)
                    raise
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc, job)
            _reap(proc)
        if job is not None:
            _close_job(job)
        fout.close()
        ferr.close()


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------- 环境变量

# 细则 1.4：白名单重建。不继承 ANTHROPIC_*、CLAUDE_*（防止悄悄记到用户订阅上）
_ENV_ALLOW = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "TEMP", "TMP", "CODEX_HOME", "KNOTCORD_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)
_ENV_DENY_PREFIX = ("ANTHROPIC_", "CLAUDE_")


def child_env(extra: dict | None = None) -> dict:
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku in _ENV_ALLOW and not ku.startswith(_ENV_DENY_PREFIX):
            env[k] = v
    env["KNOTCORD_INTERNAL"] = "1"
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------- 脱离父进程树

def spawn_detached(args: list[str], *, env: dict | None = None) -> None:
    """拉起一个脱离调用者的进程，不等待（细则 1.5）。不用 DETACHED_PROCESS：
    没有控制台的进程再起控制台程序，Windows 会给每个子进程弹窗。"""
    _need_win()
    if env is None:
        env = child_env()
    base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    last: Exception | None = None
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):  # BREAKAWAY 被拒就去掉再试一次
        try:
            subprocess.Popen(
                args, env=env, creationflags=flags, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except OSError as e:
            last = e
    assert last is not None
    raise last


# ---------------------------------------------------------------- 父进程看门狗

def _creation_time(handle) -> int:
    c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    if not _k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return 0
    return (c.dwHighDateTime << 32) | c.dwLowDateTime


def watch_parent(on_dead: Callable[[], None]) -> None:
    """起守护线程，对父进程句柄 WaitForSingleObject，父进程一死立刻调 on_dead。
    打不开句柄（权限不够）时静默返回，退出只靠 stdin EOF。"""
    _need_win()
    ppid = os.getppid()
    if ppid <= 0:
        return

    def _fire() -> None:
        try:
            on_dead()
        except Exception:
            pass

    h = _k32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, ppid)
    if not h:
        if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:  # 父进程早已不存在
            threading.Thread(target=_fire, daemon=True, name="knotcord-watch-parent").start()
        return
    # pid 可能被复用：真父进程一定比我早创建
    pc, mine = _creation_time(h), _creation_time(_k32.GetCurrentProcess())
    reused = bool(pc and mine and pc > mine)

    def _run() -> None:
        try:
            if not reused:
                _k32.WaitForSingleObject(h, _INFINITE)
        finally:
            _k32.CloseHandle(h)
        _fire()

    threading.Thread(target=_run, daemon=True, name="knotcord-watch-parent").start()

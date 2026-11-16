"""hooks/entry.py：真起子进程（python -I -S -X utf8），测输出、落库、拉起 drain、撞锁不卡、不加载重依赖。"""

import json
import os
import statistics
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from knotcord import inject
from knotcord.core import db, project

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "knotcord" / "hooks" / "entry.py"
CWD = "C:\\work\\demo"


def run_hook(event, payload=None, *, env=None, raw=None, timeout=10):
    e = dict(os.environ)
    e.pop("KNOTCORD_INTERNAL", None)
    e.update(env or {})
    data = raw if raw is not None else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(ENTRY), event], input=data,
                       capture_output=True, env=e, timeout=timeout)
    return r, time.perf_counter() - t0


def _seed(conn):
    proj = project.project_key(CWD)
    now = int(time.time())
    conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                 (proj, now, "decision", "选 sqlite 做存储，路径含中文：测试", "b"))
    conn.commit()
    return proj, inject.render(conn, proj)


def _pending(conn):
    return conn.execute("SELECT session_id, client, transcript_path, reason FROM pending ORDER BY rowid").fetchall()


def test_session_start_outputs_injection_utf8(conn):
    proj, text = _seed(conn)
    r, _ = run_hook("session-start", {"source": "startup", "session_id": "s-new", "cwd": CWD})
    assert r.returncode == 0 and r.stderr == b""
    out = r.stdout.decode("utf-8")
    assert out == text + "\n"
    assert "中文：测试" in out


def test_session_start_resume_injects_nothing(conn):
    _seed(conn)
    r, _ = run_hook("session-start", {"source": "resume", "session_id": "s", "cwd": CWD})
    assert r.returncode == 0 and r.stdout == b""


def test_session_start_without_db_injects_nothing_and_creates_nothing(trace_home):
    r, _ = run_hook("session-start", {"source": "startup", "session_id": "s", "cwd": CWD})
    assert r.returncode == 0 and r.stdout == b""
    assert not trace_home.exists()  # 只读连接，不顺手建库


def test_session_start_excludes_current_session_tail(conn):
    proj, _ = _seed(conn)
    now = int(time.time())
    for sid, body, seen in (("cur", "本会话", now), ("prev", "上个会话没提炼", now - 60)):
        conn.execute("INSERT INTO sessions(id, client, project, last_seen) VALUES(?,?,?,?)", (sid, "claude", proj, seen))
        conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, body) VALUES(?,?,?,?,?,?)",
                     (sid, proj, seen, "u-" + sid, "prompt", body))
    conn.commit()
    r, _ = run_hook("session-start", {"source": "startup", "session_id": "cur", "cwd": CWD})
    out = r.stdout.decode("utf-8")
    assert "上个会话没提炼" in out and "本会话" not in out


def test_trace_internal_does_nothing(conn):
    _seed(conn)
    for ev in ("session-start", "session-end", "stop", "pre-compact"):
        r, _ = run_hook(ev, {"source": "startup", "session_id": "s", "cwd": CWD, "transcript_path": "x"},
                        env={"KNOTCORD_INTERNAL": "1", "KNOTCORD_DRAIN_CMD": json.dumps([sys.executable, "-c", "raise SystemExit(7)"])})
        assert r.returncode == 0 and r.stdout == b""
    assert _pending(conn) == []


def test_session_end_writes_pending_and_starts_drain(conn, tmp_path):
    marker = tmp_path / "drain_started.txt"
    cmd = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    r, _ = run_hook("session-end", {"session_id": "s1", "transcript_path": "C:\\t\\s1.jsonl", "cwd": CWD},
                    env={"KNOTCORD_DRAIN_CMD": json.dumps(cmd)})
    assert r.returncode == 0 and r.stdout == b""
    assert [tuple(x) for x in _pending(conn)] == [("s1", "claude", "C:\\t\\s1.jsonl", "end")]
    deadline = time.time() + 8
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists()


def test_session_end_still_starts_drain_when_db_write_fails(conn, tmp_path):
    marker = tmp_path / "m2.txt"
    cmd = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        r, dt = run_hook("session-end", {"session_id": "s2", "transcript_path": "p"},
                         env={"KNOTCORD_DRAIN_CMD": json.dumps(cmd)})
    finally:
        blocker.rollback()
        blocker.close()
    assert r.returncode == 0
    assert _pending(conn) == []
    deadline = time.time() + 8
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    assert dt < 2.5


def test_stop_writes_pending_and_last_message(conn):
    secret = "sk-ant-api03-" + "Q" * 40
    r, _ = run_hook("stop", {"session_id": "s3", "transcript_path": "p3", "last_assistant_message": "完成了 " + secret})
    assert r.returncode == 0 and r.stdout == b""
    assert [tuple(x) for x in _pending(conn)] == [("s3", "claude", "p3", "stop")]
    msg = db.kv_get(conn, "last_msg:s3")
    assert msg.startswith("完成了") and "sk-ant" not in msg  # 入库前脱敏


def test_stop_without_message_writes_only_pending(conn):
    run_hook("stop", {"session_id": "s4", "transcript_path": "p4"})
    assert len(_pending(conn)) == 1 and db.kv_get(conn, "last_msg:s4") is None


def test_pre_compact_writes_pending(conn):
    r, _ = run_hook("pre-compact", {"session_id": "s5", "transcript_path": "p5"})
    assert r.returncode == 0
    assert [tuple(x) for x in _pending(conn)] == [("s5", "claude", "p5", "compact")]


def test_missing_session_id_writes_nothing(conn):
    run_hook("stop", {"transcript_path": "p"})
    assert _pending(conn) == []


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]", b"\xff\xfe\x00", b'{"session_id": 5}'])
def test_garbage_stdin_exits_zero_silently(conn, raw):
    for ev in ("session-start", "stop", "pre-compact"):
        r, _ = run_hook(ev, raw=raw)
        assert r.returncode == 0 and r.stdout == b""


def test_unknown_event_exits_zero():
    r, _ = run_hook("bogus", {})
    assert r.returncode == 0 and r.stdout == b""
    r, _ = run_hook("", {})
    assert r.returncode == 0


def test_session_start_not_blocked_by_write_lock(conn):
    proj, text = _seed(conn)
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        r, dt = run_hook("session-start", {"source": "startup", "session_id": "s", "cwd": CWD})
    finally:
        blocker.rollback()
        blocker.close()
    assert r.returncode == 0
    assert r.stdout.decode("utf-8") == text + "\n"  # WAL：读不被写锁挡住
    assert dt < 0.4


def test_stop_under_write_lock_gives_up_fast_and_exits_zero(conn):
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        r, dt = run_hook("stop", {"session_id": "s6", "transcript_path": "p"})
    finally:
        blocker.rollback()
        blocker.close()
    assert r.returncode == 0 and r.stdout == b""
    assert dt < 0.95  # 等锁 0.4 秒 + 进程启动，没撞上 1 秒自杀定时器


def test_session_start_median_latency(conn):
    _seed(conn)
    payload = {"source": "startup", "session_id": "s", "cwd": CWD}
    run_hook("session-start", payload)  # 预热磁盘缓存
    times = sorted(run_hook("session-start", payload)[1] for _ in range(20))
    med = statistics.median(times)
    print(f"\nSessionStart 20 次: 中位 {med * 1000:.0f} ms, 最慢 {times[-1] * 1000:.0f} ms")
    assert med < 0.3  # 目标 150 ms（本机实测 120 ms 左右）；留余量防机器忙


def test_hook_does_not_load_heavy_modules(conn, tmp_path):
    _seed(conn)
    script = tmp_path / "probe_mods.py"
    script.write_text(textwrap.dedent(f"""
        import sys, io
        sys.path.insert(0, {str(ROOT)!r})
        sys.stdin = io.TextIOWrapper(io.BytesIO(b'{{"source":"startup","session_id":"s","cwd":"C:\\\\\\\\work\\\\\\\\demo"}}'))
        from knotcord.hooks import entry
        entry.main(["session-start"])
        bad = [m for m in ("numpy", "onnxruntime", "mcp", "knotcord.extract", "knotcord.search", "knotcord.migrate")
               if m in sys.modules]
        print("BAD=" + ",".join(bad))
    """), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(script)], capture_output=True,
                       env=dict(os.environ), timeout=10)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert out.splitlines()[-1] == "BAD="

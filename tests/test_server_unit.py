"""server 里不需要起进程就能测的部分：时间解析、日志流、Worker 线程（直接在进程内跑）。"""

import threading
import time

import pytest

import srvharness as H
from knotcord.core import db, lease
from knotcord.server import main as sm
from knotcord.server.worker import Worker, remove_own_tmp


def test_parse_time_forms():
    now = time.time()
    assert sm.parse_time(None) is None and sm.parse_time("") is None
    assert sm.parse_time(1700000000) == 1700000000 and sm.parse_time("1700000000") == 1700000000
    assert abs(sm.parse_time("7d") - (now - 7 * 86400)) < 5
    assert abs(sm.parse_time("12h") - (now - 12 * 3600)) < 5
    assert abs(sm.parse_time("2w") - (now - 14 * 86400)) < 5
    a = sm.parse_time("2026-11-10")
    assert sm.parse_time("2026-11-10", end=True) == a + 86399
    assert sm.parse_time("2026-11-10 08:30") == a + 8 * 3600 + 30 * 60
    with pytest.raises(ValueError):
        sm.parse_time("昨天")


def test_clean_drops_placeholders():
    assert sm._clean("${session_id}") is None
    assert sm._clean("  ") is None and sm._clean(None) is None and sm._clean(5) is None
    assert sm._clean("abc") == "abc"
    assert sm._clean("${x} 加上别的") == "${x} 加上别的"  # 只有整串是占位符才忽略


def test_log_stream_swallows_writes(trace_home):
    s = sm._LogStream()
    assert s.write("hello\nworld") == 11
    s.flush()
    assert s.isatty() is False
    with pytest.raises(OSError):
        s.fileno()


def _worker(**kw):
    stop = threading.Event()
    w = Worker(stop, None, start_delay_s=0, **kw)
    return w, stop


def test_worker_takes_lease_ingests_and_cleans_up(trace_home, tmp_path):
    H.write_config(trace_home)
    from knotcord.core import config
    w = Worker(threading.Event(), config.load(), start_delay_s=0)
    cwd = str(tmp_path / "p")
    tp = tmp_path / "t.jsonl"
    H.make_transcript(tp, "s-w", cwd, turns=2)
    w.start()
    try:
        assert w.ready.wait(10) and w.holding
        w.submit("s-w", str(tp), cwd, "ok")
        c = db.connect(readonly=True)
        try:
            assert H.wait_until(lambda: c.execute("SELECT count(*) FROM events WHERE session_id='s-w'").fetchone()[0], 10)
            info = lease.holder_info(c)
            assert info and info["owner"] == w.lease_owner
        finally:
            c.close()
    finally:
        w.stop_event.set()
        w.wake()
        w.join(10)
    assert not w.is_alive()
    c = db.connect(readonly=True)
    try:
        assert lease.holder_info(c) is None
        assert c.execute("SELECT count(*) FROM kv WHERE key LIKE 'proc:%'").fetchone()[0] == 0
    finally:
        c.close()


def test_worker_without_lease_still_ingests(trace_home, tmp_path, conn):
    """别的进程持有租约时，worker 只做入库，不提炼。"""
    H.write_config(trace_home)
    other = lease.Lease(conn, owner="99999:x:y", ttl_s=300)
    assert other.try_acquire()
    from knotcord.core import config
    w = Worker(threading.Event(), config.load(), start_delay_s=0)
    cwd = str(tmp_path / "p")
    tp = tmp_path / "t.jsonl"
    H.make_transcript(tp, "s-n", cwd, turns=1)
    w.start()
    try:
        assert w.ready.wait(10)
        assert not w.holding
        w.submit("s-n", str(tp), cwd, None)
        assert H.wait_until(lambda: conn.execute("SELECT count(*) FROM events WHERE session_id='s-n'").fetchone()[0], 10)
    finally:
        w.stop_event.set()
        w.wake()
        w.join(10)
    assert lease.holder_info(conn)["owner"] == "99999:x:y"  # 没动别人的租约


def test_worker_degrades_when_schema_newer(trace_home, conn, tmp_path):
    conn.execute("PRAGMA user_version=999")
    from knotcord.core import config
    w = Worker(threading.Event(), config.load(), start_delay_s=0)
    w.start()
    try:
        assert w.ready.wait(10)
        assert w.degraded and not w.holding
        w.submit("s-x", str(tmp_path / "none.jsonl"), None, None)
        time.sleep(1.5)
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert lease.holder_info(conn) is None
    finally:
        w.stop_event.set()
        w.wake()
        w.join(10)


def test_remove_own_tmp_only_removes_own(trace_home):
    import os
    from knotcord.core import paths
    mine = paths.tmp_dir() / f"b1-{os.getpid()}"
    other = paths.tmp_dir() / "b2-1"
    mine.mkdir()
    (mine / "x").write_text("x")
    other.mkdir()
    remove_own_tmp()
    assert not mine.exists() and other.exists()

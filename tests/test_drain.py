"""drain：有活着的租约持有者就直接退出；没有就处理 pending、对结束的会话提炼、清理。模型通道全部用假实现。"""

import os
import subprocess
import sys
import time

import pytest

import srvharness as H
from test_extract_pipeline import FakeChannel, good
from knotcord import drain, procs
from knotcord.core import db, lease, paths
from knotcord.extract import budget, codex

DEAD_PID = 4_000_000


@pytest.fixture(autouse=True)
def _breaker():
    budget.reset_breaker()
    yield
    budget.reset_breaker()


def add_pending(conn, sid, path, reason="end"):
    conn.execute("INSERT INTO pending(session_id, client, transcript_path, reason, enqueued_at) VALUES(?,?,?,?,?)",
                 (sid, "claude", str(path), reason, int(time.time())))


def make_session(conn, tmp_path, sid="d1", reason="end", fresh=False):
    cwd = str(tmp_path / "proj")
    tp = tmp_path / f"{sid}.jsonl"
    H.make_transcript(tp, sid, cwd, turns=2, t0=int(time.time()) - 60 if fresh else 1_789_000_000)
    add_pending(conn, sid, tp, reason)
    return tp


def test_exits_when_live_holder_exists(trace_home, conn, tmp_path):
    make_session(conn, tmp_path)
    holder = lease.Lease(conn, owner=f"{os.getpid()}:x:y", ttl_s=300)  # 本进程活着：持有者在世
    assert holder.try_acquire()
    t0 = time.monotonic()
    assert drain.run(grace_s=0.3, wait_s=5, max_s=30) == drain.LEASE_BUSY
    assert time.monotonic() - t0 < 3
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1  # 没动
    assert lease.holder_info(conn)["owner"] == holder.owner  # 租约还是别人的
    assert conn.execute("SELECT count(*) FROM kv WHERE key LIKE 'proc:%'").fetchone()[0] == 0


def test_processes_pending_and_flushes_ended_session(trace_home, conn, tmp_path, monkeypatch):
    make_session(conn, tmp_path, "d1", reason="end")
    ch = FakeChannel(good())
    monkeypatch.setattr(codex, "call", ch)
    assert drain.run(grace_s=0.3, wait_s=5, max_s=60, memroot=str(tmp_path / "mem")) == drain.DONE
    assert len(ch.prompts) == 1  # 会话结束 -> flush，哪怕没到阈值
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    n, nb = conn.execute("SELECT count(*), sum(batch_id IS NOT NULL) FROM events WHERE session_id='d1'").fetchone()
    assert n >= 4 and nb == n
    assert conn.execute("SELECT count(*) FROM notes WHERE session_id='d1'").fetchone()[0] >= 1
    assert lease.holder_info(conn) is None  # 租约已释放
    assert conn.execute("SELECT count(*) FROM kv WHERE key LIKE 'proc:%'").fetchone()[0] == 0
    assert not any(paths.tmp_dir().iterdir())  # 批目录用完即删
    assert db.kv_get(conn, "janitor:last")  # 退出前跑了清理


def test_non_end_pending_is_ingested_but_not_flushed(trace_home, conn, tmp_path, monkeypatch):
    make_session(conn, tmp_path, "d2", reason="stop", fresh=True)  # 不是 end、刚有活动、没到阈值：不提炼
    ch = FakeChannel(good())
    monkeypatch.setattr(codex, "call", ch)
    assert drain.run(grace_s=0.3, wait_s=5, max_s=60, memroot=str(tmp_path / "mem")) == drain.DONE
    assert ch.prompts == []
    assert conn.execute("SELECT count(*) FROM events WHERE session_id='d2'").fetchone()[0] >= 4
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0


def test_waits_for_stale_lease_of_dead_process(trace_home, conn, tmp_path, monkeypatch):
    make_session(conn, tmp_path, "d3")
    monkeypatch.setattr(codex, "call", FakeChannel(good()))
    stale = lease.Lease(conn, owner=f"{DEAD_PID}:x:y", ttl_s=1.5)  # 持有者早已不在，1.5 秒后过期
    assert stale.try_acquire()
    t0 = time.monotonic()
    assert drain.run(grace_s=0.3, wait_s=15, max_s=60, memroot=str(tmp_path / "mem")) == drain.DONE
    took = time.monotonic() - t0
    assert 1.0 < took < 10  # 等到过期才接手，不是一直等
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0


def test_stale_lease_wait_is_bounded(trace_home, conn, tmp_path):
    make_session(conn, tmp_path, "d4")
    stale = lease.Lease(conn, owner=f"{DEAD_PID}:x:y", ttl_s=300)
    assert stale.try_acquire()
    t0 = time.monotonic()
    assert drain.run(grace_s=0.2, wait_s=0.6, max_s=60) == drain.LEASE_BUSY
    assert time.monotonic() - t0 < 5
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1


def test_degrades_when_schema_newer(trace_home, conn, tmp_path):
    make_session(conn, tmp_path, "d5")
    conn.execute("PRAGMA user_version=999")
    assert drain.run(grace_s=0.2, wait_s=0.5, max_s=30) == drain.LEASE_BUSY
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    assert lease.holder_info(conn) is None


def test_command_empty_db_exits_zero_quickly(trace_home):
    H.write_config(trace_home)
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(H.BOOT), "drain"], env=H.child_env(trace_home),
                       capture_output=True, timeout=60, creationflags=H.CREATE_NO_WINDOW)
    assert r.returncode == 0 and r.stdout == b""
    assert time.monotonic() - t0 < 30


def test_command_exits_on_live_lease(trace_home, conn, tmp_path):
    """真进程：持有者活着（用测试进程自己的 pid），drain 在宽限期（6 秒）后直接退出，不处理 pending。"""
    H.write_config(trace_home)
    make_session(conn, tmp_path, "d6")
    assert lease.Lease(conn, owner=f"{os.getpid()}:x:y", ttl_s=300).try_acquire()
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(H.BOOT), "drain"], env=H.child_env(trace_home),
                       capture_output=True, timeout=60, creationflags=H.CREATE_NO_WINDOW)
    took = time.monotonic() - t0
    assert r.returncode == 0
    assert drain.GRACE_S - 1 < took < 20
    assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1

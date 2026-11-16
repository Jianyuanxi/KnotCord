"""janitor：每一项在临时目录里真的生效。"""

import json
import os
import sqlite3
import time

import pytest

from knotcord import janitor, procs
from knotcord.core import config, db, paths

DAY = 86400


@pytest.fixture
def cfg():
    return config.load()


def touch(p, age_s, size=1, now=None):
    now = now or time.time()
    p.write_bytes(b"x" * size)
    os.utime(p, (now - age_s, now - age_s))


# ---------------------------------------------------------------- 每次都做

def test_sweep_tmp_removes_only_old(trace_home):
    now = time.time()
    t = paths.tmp_dir()
    touch(t / "old.txt", 7 * 3600)
    touch(t / "new.txt", 60)
    old_dir = t / "b1-111"
    old_dir.mkdir()
    touch(old_dir / "prompt.txt", 8 * 3600)
    os.utime(old_dir, (now - 8 * 3600, now - 8 * 3600))
    busy = t / "b2-222"  # 目录本身旧，但里面有新写的文件：还在用
    busy.mkdir()
    touch(busy / "out.txt", 30)
    os.utime(busy, (now - 8 * 3600, now - 8 * 3600))
    assert janitor.sweep_tmp(now) == 2
    assert sorted(p.name for p in t.iterdir()) == ["b2-222", "new.txt"]


def test_rotate_logs_by_days_and_size(trace_home):
    now = time.time()
    d = paths.logs_dir()

    def name(days_ago):
        return f"knotcord-{time.strftime('%Y-%m-%d', time.localtime(now - days_ago * DAY))}.log"

    touch(d / name(30), 0, 10)
    touch(d / name(15), 0, 10)
    touch(d / name(5), 0, 10)
    touch(d / name(0), 0, 10)
    assert janitor.rotate_logs(now, 14, 50) == 2
    assert sorted(p.name for p in d.iterdir()) == sorted([name(5), name(0)])
    # 按体积：每个 600 KB，上限 1 MB -> 从最旧的删，当天的不删
    touch(d / name(3), 0, 600 * 1024)
    touch(d / name(2), 0, 600 * 1024)
    touch(d / name(0), 0, 600 * 1024)
    janitor.rotate_logs(now, 14, 1)
    left = sorted(p.name for p in d.iterdir())
    assert name(0) in left and name(3) not in left


def test_truncate_wal(trace_home, conn):
    for i in range(200):
        conn.execute("INSERT INTO kv(key, value) VALUES(?,?)", (f"k{i}", "v" * 2000))
    wal = paths.home() / "knotcord.db-wal"
    assert wal.exists() and wal.stat().st_size > 0
    assert janitor.truncate_wal(conn) is True
    assert wal.stat().st_size == 0


def test_run_always_items_and_gating(trace_home, conn, cfg):
    now = time.time()
    touch(paths.tmp_dir() / "stale", 9 * 3600)
    conn.execute("INSERT INTO kv(key, value) VALUES('proc:4000000', ?)",
                 (json.dumps({"pid": 4000000, "role": "server", "started": 1, "beat": 1}),))
    assert janitor.due(conn, cfg, now)
    res = janitor.run(conn, cfg, now=now, memroot=str(trace_home / "nomem"))
    assert res["tmp_removed"] == 1 and res["procs_pruned"] == 1 and res["wal_truncated"] in (True, False)
    assert not (paths.tmp_dir() / "stale").exists()
    assert conn.execute("SELECT count(*) FROM kv WHERE key LIKE 'proc:%'").fetchone()[0] == 0
    assert db.kv_get(conn, janitor.KEY_LAST) == str(int(now))
    assert not janitor.due(conn, cfg, now + 3600)
    assert janitor.run(conn, cfg, now=now + 3600) == {}  # 不到间隔：什么都不做
    assert janitor.due(conn, cfg, now + 7 * 3600)
    assert janitor.run(conn, cfg, now=now + 3600, force=True, memroot=str(trace_home / "nomem"))  # force 无视间隔


# ---------------------------------------------------------------- 每周

def test_backup_keeps_only_latest_and_is_valid(trace_home, conn):
    conn.execute("INSERT INTO kv(key, value) VALUES('marker', 'one')")
    p1 = janitor.backup(conn)
    assert p1 == paths.backups_dir() / "knotcord.db"
    conn.execute("UPDATE kv SET value='two' WHERE key='marker'")
    p2 = janitor.backup(conn)
    assert p2 == p1
    assert [p.name for p in paths.backups_dir().iterdir()] == ["knotcord.db"]
    chk = sqlite3.connect(str(p1))
    try:
        assert chk.execute("SELECT value FROM kv WHERE key='marker'").fetchone()[0] == "two"
        assert chk.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        chk.close()
    assert not any(paths.tmp_dir().iterdir())  # 临时副本已删


def test_clear_old_event_bodies_only_tool_batched_old(trace_home, conn):
    now = time.time()
    old, new = int(now - 200 * DAY), int(now - 10 * DAY)
    conn.execute("INSERT INTO batches(id, session_id, created, status) VALUES(1,'s','1','done')")

    def ev(uid, kind, ts, body, batch):
        conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, tool, brief, body, src_line, batch_id) "
                     "VALUES('s','p',?,?,?,?,?,?,7,?)", (ts, uid, kind, "Bash" if kind == "tool" else None, "brief", body, batch))

    ev("a", "tool", old, "旧工具输出", 1)       # 清
    ev("b", "tool", new, "新工具输出", 1)       # 不够老
    ev("c", "tool", old, "还没提炼的", None)    # 没提炼过：留着
    ev("d", "prompt", old, "旧提示", 1)         # 提示不动
    ev("e", "tool", old * 1000, "毫秒时间戳的旧记录", 1)  # 毫秒制 ts 也要按秒换算：清
    ev("f", "tool", new * 1000, "毫秒时间戳的新记录", 1)  # 不够老
    assert janitor.clear_old_event_bodies(conn, now, 180, chunk=1) == 2  # chunk=1 走多轮
    rows = {r["uuid"]: r for r in conn.execute("SELECT uuid, body, brief, src_line FROM events")}
    assert rows["a"]["body"] == "" and rows["a"]["brief"] == "brief" and rows["a"]["src_line"] == 7
    assert rows["b"]["body"] == "新工具输出"
    assert rows["c"]["body"] == "还没提炼的"
    assert rows["d"]["body"] == "旧提示"
    assert rows["e"]["body"] == "" and rows["f"]["body"] == "毫秒时间戳的新记录"


def test_weekly_runs_all_items_once(trace_home, conn, cfg, tmp_path):
    now = time.time()
    root = tmp_path / "mem"
    mem = root / "C--work-Demo" / "memory"
    mem.mkdir(parents=True)
    (mem / "feedback_x.md").write_text("---\nname: 反馈x\ndescription: 一条测试记忆\n---\n正文 宝塔面板\n", encoding="utf-8")
    old = int(now - 200 * DAY)
    conn.execute("INSERT INTO batches(id, session_id, created, status) VALUES(1,'s','1','done')")
    conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, tool, brief, body, batch_id) "
                 "VALUES('s','p',?,'u1','tool','Bash','b','旧输出',1)", (old,))
    res = janitor.run(conn, cfg, now=now, memroot=str(root))
    assert res["quick_check"] == "ok"
    assert res["backup"] and os.path.exists(res["backup"])
    assert res["event_bodies_cleared"] == 1
    assert res["memfiles_updated"] == 1
    assert conn.execute("SELECT name FROM memfiles").fetchone()[0] == "反馈x"
    assert conn.execute("SELECT body FROM events WHERE uuid='u1'").fetchone()[0] == ""
    assert db.kv_get(conn, janitor.KEY_WEEKLY) == str(int(now))
    # 一周内再跑：每次都做的项目照常，每周项不重复
    res2 = janitor.run(conn, cfg, now=now + 7 * 3600, memroot=str(root))
    assert "backup" not in res2 and "tmp_removed" in res2
    # 满一周：再做
    res3 = janitor.run(conn, cfg, now=now + 8 * DAY, memroot=str(root))
    assert res3["quick_check"] == "ok" and res3["backup"]


def test_quick_check_failure_sets_corrupt_flag_and_skips(trace_home, conn, cfg, monkeypatch):
    real_quick_check = janitor.quick_check
    monkeypatch.setattr(janitor, "quick_check", lambda c: "*** in database main *** Page 5: btreeInitPage() error")
    now = time.time()
    res = janitor.run(conn, cfg, now=now, memroot=str(trace_home / "nomem"))
    assert res["quick_check"].startswith("***")
    assert "backup" not in res and not (paths.backups_dir() / "knotcord.db").exists()  # 不用坏库覆盖旧备份
    assert "已停止记录" in db.kv_get(conn, janitor.KEY_CORRUPT)
    assert db.kv_get(conn, janitor.KEY_WEEKLY) is None  # 下次清理还会再查
    # 之后库恢复正常：标记被清掉
    # 不能用 monkeypatch.undo()：它会连 KNOTCORD_HOME 一起撤掉，后面的清理和备份就写进真实的 ~/.knotcord
    monkeypatch.setattr(janitor, "quick_check", real_quick_check)
    res2 =janitor.run(conn, cfg, now=now + DAY, force=True, memroot=str(trace_home / "nomem"))
    assert res2["quick_check"] == "ok"
    assert db.kv_get(conn, janitor.KEY_CORRUPT) is None


def test_quick_check_transient_error_is_not_corruption(trace_home, conn, cfg, monkeypatch):
    def locked(c):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(janitor, "quick_check", locked)
    res = janitor.run(conn, cfg, now=time.time(), memroot=str(trace_home / "nomem"))
    assert "weekly_skipped" in res
    assert db.kv_get(conn, janitor.KEY_CORRUPT) is None


def test_fts_optimize_and_incremental_vacuum_run(trace_home, conn):
    from knotcord.core.index import index_doc
    for i in range(20):
        index_doc(conn, "note", i + 1, "p", 1, f"标题{i} 内容")
    janitor.fts_optimize(conn)
    janitor.incremental_vacuum(conn)
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 20

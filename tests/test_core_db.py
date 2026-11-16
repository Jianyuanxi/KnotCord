import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from knotcord.core import db, paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    from knotcord.core import db
    tag = sys.argv[2]
    conn = db.connect()
    for i in range(1000):
        with db.write_tx(conn):
            conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                         ("p", i, "change", f"{tag}-{i}", "x"))
    conn.close()
""")


def test_two_processes_write_1000_each(tmp_path, trace_home):
    script = tmp_path / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    # 库还不存在时同时启动，顺带测首次建库的竞争
    procs = [subprocess.Popen([sys.executable, str(script), ROOT, tag],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for tag in ("a", "b")]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode("utf-8", "replace")
        assert b"locked" not in err
    conn = db.connect()
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2000
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_pragmas_and_schema(conn):
    assert conn.isolation_level is None
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("sessions", "events", "notes", "state_cards", "mem_flags", "summaries", "docs",
              "docs_fts", "docs_vec", "batches", "pending", "kv"):
        assert t in names
    assert "ended" not in db.table_columns(conn, "sessions")
    assert "last_seen" in db.table_columns(conn, "sessions")
    for c in ("last_mentioned", "use_count", "last_used"):
        assert c in db.table_columns(conn, "notes")
    for c in ("status", "attempts", "next_try"):
        assert c in db.table_columns(conn, "batches")
    assert "use_count" in db.table_columns(conn, "docs")


def test_events_uuid_unique_and_batch_status_check(conn):
    with db.write_tx(conn):
        conn.execute("INSERT INTO events(uuid, kind) VALUES('u1','prompt')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO events(uuid, kind) VALUES('u1','prompt')")
    conn.execute("INSERT OR IGNORE INTO events(uuid, kind) VALUES('u1','prompt')")
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO batches(status) VALUES('weird')")
    for s in ("running", "done", "failed", "dead"):
        conn.execute("INSERT INTO batches(status) VALUES(?)", (s,))


def test_write_tx_rollback_and_nesting(conn):
    with pytest.raises(RuntimeError):
        with db.write_tx(conn):
            conn.execute("INSERT INTO kv(key,value) VALUES('a','1')")
            raise RuntimeError("boom")
    assert db.kv_get(conn, "a") is None
    assert not conn.in_transaction
    with db.write_tx(conn):
        with db.write_tx(conn):  # 并入外层
            conn.execute("INSERT INTO kv(key,value) VALUES('b','1')")
        assert conn.in_transaction
    assert not conn.in_transaction
    assert db.kv_get(conn, "b") == "1"


def test_kv(conn):
    assert db.kv_get(conn, "x") is None
    assert db.kv_get(conn, "x", "d") == "d"
    db.kv_set(conn, "x", "1")
    db.kv_set(conn, "x", "2")
    assert db.kv_get(conn, "x") == "2"
    db.kv_set(conn, "n", 5)
    assert db.kv_get(conn, "n") == "5"


def test_is_newer_than_code(conn):
    assert db.is_newer_than_code(conn) is False
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
    assert db.is_newer_than_code(conn) is True
    # 版本更高的库：新连接不升级、不改动
    c2 = db.connect()
    assert db.is_newer_than_code(c2)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION + 1
    c2.close()


def test_readonly_missing_db_raises(trace_home):
    assert not paths.db_path().exists()
    with pytest.raises(FileNotFoundError):
        db.connect(readonly=True)
    assert not paths.db_path().exists()  # 不建库
    assert not trace_home.exists()


def test_readonly_cannot_write(conn):
    conn.close()
    ro = db.connect(readonly=True, busy_timeout_ms=300)
    assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
    assert ro.execute("SELECT count(*) FROM kv").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO kv(key,value) VALUES('a','b')")
    ro.close()


def test_upgrade_old_db_adds_missing_columns(trace_home):
    """老库（缺列、无 user_version）：用 table_info 看真实列，补列后再建索引。"""
    trace_home.mkdir(parents=True)
    old = sqlite3.connect(str(paths.db_path()))
    old.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, client TEXT, project TEXT, cwd TEXT,
            transcript_path TEXT, read_offset INTEGER DEFAULT 0, started_at INTEGER,
            ended INTEGER DEFAULT 0);
        CREATE TABLE notes(id INTEGER PRIMARY KEY, project TEXT, session_id TEXT, ts INTEGER, type TEXT,
            title TEXT, body TEXT, facts TEXT, files TEXT, tags TEXT, slot TEXT,
            state TEXT DEFAULT 'active', superseded_by INTEGER, superseded_at INTEGER,
            source_events TEXT, model TEXT, origin TEXT, legacy_id INTEGER, content_hash TEXT);
        CREATE TABLE batches(id INTEGER PRIMARY KEY, session_id TEXT, created INTEGER, status TEXT,
            provider TEXT, chars_in INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_cny REAL, error TEXT);
        CREATE TABLE docs(id INTEGER PRIMARY KEY, src TEXT, src_id INTEGER, project TEXT, ts INTEGER,
            active INTEGER DEFAULT 1);
        INSERT INTO notes(project, title) VALUES('p', 'keep me');
    """)
    old.commit()
    old.close()
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "last_seen" in db.table_columns(conn, "sessions")
    assert {"last_mentioned", "use_count", "last_used"} <= set(db.table_columns(conn, "notes"))
    assert {"attempts", "next_try"} <= set(db.table_columns(conn, "batches"))
    assert "use_count" in db.table_columns(conn, "docs")
    assert conn.execute("SELECT title, use_count FROM notes").fetchone()[:] == ("keep me", 0)
    # docs 的 (src, src_id) 唯一
    conn.execute("INSERT INTO docs(src, src_id) VALUES('n', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO docs(src, src_id) VALUES('n', 1)")
    conn.close()

"""迁移测试：用结构和真实旧库一致的小库（只含迁移用到的列，外加同步列样例）。"""

import json
import sqlite3

import pytest

from knotcord.migrate import claude_mem
from knotcord.search import api

MS = 1_777_634_729_004  # 毫秒


def make_src(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE observations(id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, text TEXT, type TEXT,
        title TEXT, subtitle TEXT, facts TEXT, narrative TEXT, concepts TEXT, files_read TEXT, files_modified TEXT,
        prompt_number INTEGER, discovery_tokens INTEGER, created_at TEXT, created_at_epoch INTEGER,
        content_hash TEXT, generated_by_model TEXT, relevance_count INTEGER, synced_at INTEGER);
    CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, request TEXT,
        investigated TEXT, learned TEXT, completed TEXT, next_steps TEXT, files_read TEXT, files_edited TEXT,
        notes TEXT, prompt_number INTEGER, created_at TEXT, created_at_epoch INTEGER);
    CREATE TABLE user_prompts(id INTEGER PRIMARY KEY, session_db_id INTEGER, content_session_id TEXT,
        prompt_number INTEGER, prompt_text TEXT, created_at TEXT, created_at_epoch INTEGER);
    CREATE TABLE sdk_sessions(id INTEGER PRIMARY KEY, content_session_id TEXT, memory_session_id TEXT,
        project TEXT, platform_source TEXT, user_prompt TEXT, started_at TEXT, started_at_epoch INTEGER,
        completed_at TEXT, completed_at_epoch INTEGER, status TEXT, cwd TEXT);
    CREATE TABLE work_state_entries(id INTEGER PRIMARY KEY, project TEXT, list_name TEXT, fields TEXT,
        created_at TEXT, created_at_epoch INTEGER);
    """)
    c.execute("INSERT INTO sdk_sessions VALUES(1,'content-1','mem-1','Span','claude','hi','x',?,NULL,NULL,'active','E:/Span')", (MS,))
    c.execute("INSERT INTO sdk_sessions VALUES(2,'content-2',NULL,'Road','codex','hi','x',?,'y',?,'completed',NULL)", (MS, MS + 5000))
    types = ["discovery", "发现", "变更", "优化", "决策", "critical-fix", "security_note", None]
    for i, t in enumerate(types, start=1):
        c.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,100,'x',?,?,?,0,NULL)",
                  (i, "mem-1" if i % 2 else "mem-unknown", "Span", None, t, f"标题{i} 宝塔", f"副标题{i}",
                   json.dumps([f"事实{i}"], ensure_ascii=False), f"叙述{i}", '["how-it-works","gotcha"]',
                   '["r.py"]', '["m.py","r.py"]', MS + i * 1000, f"hash{i}", "gpt-6-luna"))
    c.execute("INSERT INTO session_summaries VALUES(1,'mem-1','Span','请求A','查了B','学到C','做了D','下一步E',NULL,NULL,'备注F',1,'x',?)", (MS,))
    c.execute("INSERT INTO session_summaries VALUES(2,'mem-1','Span','请求A2',NULL,NULL,NULL,NULL,NULL,NULL,NULL,2,'x',?)", (MS + 1000,))
    c.execute("INSERT INTO user_prompts VALUES(1,1,'content-1',1,'帮我配置宝塔面板','x',?)", (MS,))
    c.execute("INSERT INTO user_prompts VALUES(2,2,'content-2',1,'第二条提示 token=abcdef1234567890abcdef','x',?)", (MS + 1000,))
    c.execute("INSERT INTO user_prompts VALUES(3,9,'content-gone',1,'无会话的提示','x',?)", (MS + 2000,))
    c.execute("INSERT INTO work_state_entries VALUES(1,'system32','span-mvp',?,'x',?)",
              (json.dumps({"task": "写 ui 页面", "status": "todo"}, ensure_ascii=False), MS))
    c.execute("INSERT INTO work_state_entries VALUES(2,'system32','span-mvp','not json','x',?)", (MS,))
    c.commit()
    c.close()


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "claude-mem.db"
    make_src(str(p))
    return str(p)


def test_run_counts_and_mapping(conn, src, trace_home):
    quiet = []
    res = claude_mem.run(conn, src, snapshot=True, log=quiet.append)
    assert res == {
        "sdk_sessions": {"源条数": 2, "目标条数": 2, "一致": True},
        "observations": {"源条数": 8, "目标条数": 8, "一致": True},
        "session_summaries": {"源条数": 2, "目标条数": 2, "一致": True},
        "user_prompts": {"源条数": 3, "目标条数": 3, "一致": True},
        "work_state_entries": {"源条数": 2, "目标条数": 2, "一致": True},
    }
    # 快照存在且可打开
    snap = trace_home / "legacy" / "claude-mem.db"
    assert snap.exists()
    assert sqlite3.connect(str(snap)).execute("SELECT count(*) FROM observations").fetchone()[0] == 8

    # 会话
    s = conn.execute("SELECT * FROM sessions WHERE id='content-1'").fetchone()
    assert s["legacy_memory_session_id"] == "mem-1" and s["client"] == "claude" and s["cwd"] == "E:/Span"
    assert s["started_at"] == MS // 1000
    s2 = conn.execute("SELECT * FROM sessions WHERE id='content-2'").fetchone()
    assert s2["last_seen"] == (MS + 5000) // 1000 and s2["client"] == "codex"

    # 笔记：type 规范化、tags、正文、legacy_id/hash
    notes = {r["legacy_id"]: r for r in conn.execute("SELECT * FROM notes WHERE type!='legacy'")}
    assert {i: notes[i]["type"] for i in notes} == {
        1: "discovery", 2: "discovery", 3: "change", 4: "refactor", 5: "decision", 6: "change", 7: "change", 8: "change"}
    assert "orig:发现" in json.loads(notes[2]["tags"]) and "orig:critical-fix" in json.loads(notes[6]["tags"])
    assert "orig:discovery" not in json.loads(notes[1]["tags"])
    assert "gotcha" in json.loads(notes[1]["tags"])
    assert notes[1]["body"] == "副标题1\n叙述1"
    assert json.loads(notes[1]["facts"]) == ["事实1"]
    assert json.loads(notes[1]["files"]) == ["m.py", "r.py"]
    assert notes[1]["origin"] == "claude-mem" and notes[1]["content_hash"] == "hash1"
    assert notes[1]["ts"] == (MS + 1000) // 1000 and notes[1]["model"] == "gpt-6-luna"
    assert notes[1]["session_id"] == "content-1"          # 映射回 content_session_id
    assert notes[2]["session_id"] == "mem-unknown"         # 映射不到就保留原值
    assert notes[1]["project"] == "Span" and notes[1]["state"] == "active"

    # 总结
    sm = conn.execute("SELECT * FROM summaries WHERE legacy_id=1").fetchone()
    assert (sm["request"], sm["completed"], sm["learned"], sm["next_steps"]) == ("请求A", "做了D", "学到C", "下一步E")
    assert json.loads(sm["extra"]) == {"investigated": "查了B", "notes": "备注F"}
    assert sm["session_id"] == "content-1" and sm["origin"] == "claude-mem"

    # 提示 -> events
    ev = conn.execute("SELECT * FROM events WHERE uuid='legacy-prompt-1'").fetchone()
    assert ev["kind"] == "prompt" and ev["body"] == "帮我配置宝塔面板" and ev["session_id"] == "content-1"
    assert ev["project"] == "Span" and ev["origin"] == "claude-mem" and ev["legacy_id"] == 1
    assert ev["ts"] == MS // 1000
    ev2 = conn.execute("SELECT body FROM events WHERE uuid='legacy-prompt-2'").fetchone()
    assert "abcdef1234567890abcdef" not in ev2["body"]     # 脱敏
    assert conn.execute("SELECT project FROM events WHERE uuid='legacy-prompt-3'").fetchone()[0] == ""

    # work_state
    ws = {r["legacy_id"]: r for r in conn.execute("SELECT * FROM notes WHERE type='legacy'")}
    assert set(ws) == {1, 2} and "work_state" in json.loads(ws[1]["tags"])
    assert ws[1]["title"] == "span-mvp: 写 ui 页面" and "status: todo" in ws[1]["body"]
    assert ws[2]["body"] == "raw: not json"

    # 都建了索引、能搜到
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 8 + 2 + 3 + 2
    out = api.search(conn, "宝塔")
    assert "宝塔面板" in out and any(l.startswith("p") for l in out.splitlines())
    assert "写 ui 页面" in api.search(conn, "ui 页面")


def test_idempotent_rerun(conn, src):
    claude_mem.run(conn, src, snapshot=False, log=lambda *_: None)

    def snapshot():
        return [conn.execute(q).fetchone()[0] for q in (
            "SELECT count(*) FROM notes", "SELECT count(*) FROM summaries", "SELECT count(*) FROM events",
            "SELECT count(*) FROM sessions", "SELECT count(*) FROM docs", "SELECT count(*) FROM docs_fts")]

    first = snapshot()
    res = claude_mem.run(conn, src, snapshot=False, log=lambda *_: None)
    assert snapshot() == first
    assert all(v["一致"] for v in res.values())


def test_incremental_new_rows_imported(conn, src):
    claude_mem.run(conn, src, snapshot=False, log=lambda *_: None)
    c = sqlite3.connect(src)
    c.execute("INSERT INTO observations VALUES(99,'mem-1','Span',NULL,'bugfix','新增笔记',NULL,'[]','新叙述','[]','[]','[]',1,1,'x',?,'h99','m',0,NULL)", (MS + 99000,))
    c.commit()
    c.close()
    res = claude_mem.run(conn, src, snapshot=False, log=lambda *_: None)
    assert res["observations"] == {"源条数": 9, "目标条数": 9, "一致": True}
    assert conn.execute("SELECT body FROM notes WHERE legacy_id=99 AND type!='legacy'").fetchone()[0] == "新叙述"


def test_source_opened_readonly_and_unchanged(conn, src):
    before = open(src, "rb").read()
    claude_mem.run(conn, src, snapshot=False, log=lambda *_: None)
    assert open(src, "rb").read() == before


def test_seconds_epoch_kept(conn, src):
    c = sqlite3.connect(src)
    c.execute("UPDATE observations SET created_at_epoch=1777634729 WHERE id=1")
    c.commit()
    c.close()
    claude_mem.run(conn, src, snapshot=False, log=lambda *_: None)
    assert conn.execute("SELECT ts FROM notes WHERE legacy_id=1 AND type!='legacy'").fetchone()[0] == 1777634729


def test_not_a_claude_mem_db(conn, tmp_path):
    p = tmp_path / "x.db"
    sqlite3.connect(str(p)).close()
    with pytest.raises(ValueError):
        claude_mem.run(conn, str(p), snapshot=False, log=lambda *_: None)

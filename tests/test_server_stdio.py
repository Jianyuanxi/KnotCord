"""server：真起 boot.py serve 子进程，走 stdio 握手、列工具、调用工具，关 stdin 后限时退出并释放租约。"""

import json
import time

import pytest

import srvharness as H
from knotcord.core import db
from knotcord.core.index import index_doc

NOTE_TITLE = "宝塔面板反代配置"


@pytest.fixture
def home(trace_home):
    H.write_config(trace_home)
    return trace_home


def seed_note(conn):
    ts = int(time.time()) - 3600
    cur = conn.execute("INSERT INTO notes(project, session_id, ts, type, title, body, state) "
                       "VALUES('Span','s1',?,?,?,?, 'active')", (ts, "discovery", NOTE_TITLE, "nginx 反代到 8080"))
    nid = cur.lastrowid
    index_doc(conn, "note", nid, "Span", ts, f"{NOTE_TITLE}\nnginx 反代到 8080")
    return nid


@pytest.fixture
def server(home, tmp_path):
    s = H.Server(home, tmp_path / "server.err")
    yield s
    s.kill()


def test_handshake_and_tools(server):
    r = server.handshake()
    assert r["result"]["serverInfo"]["name"] == "knotcord"
    tools = server.rpc("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["search", "timeline", "get", "usage", "ingest"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    server.close()


def test_search_get_usage_and_docs_last_used(home, conn, server):
    nid = seed_note(conn)
    server.handshake()
    out = server.call("search", {"query": "宝塔面板"})
    assert out.split()[0] == f"n{nid}" and NOTE_TITLE in out
    assert server.call("search", {"query": "不存在的词汇xyz"}) == "无结果"
    text = server.call("get", {"ids": [f"n{nid}"]})
    assert NOTE_TITLE in text and "nginx 反代到 8080" in text
    row = conn.execute("SELECT use_count, last_used FROM docs WHERE src='note' AND src_id=?", (nid,)).fetchone()
    assert row["use_count"] == 1 and row["last_used"] >= int(time.time()) - 60
    assert "提炼" in server.call("usage")
    assert "无效编号" in server.call("get", {"ids": "zz9"})
    server.close()


def test_search_time_filter(home, conn, server):
    seed_note(conn)
    server.handshake()
    assert NOTE_TITLE in server.call("search", {"query": "宝塔面板", "since": "7d"})
    assert server.call("search", {"query": "宝塔面板", "until": "2000-01-01"}) == "无结果"
    server.close()


def test_stdout_only_protocol(home, conn, server):
    seed_note(conn)
    server.handshake()
    server.call("search", {"query": "宝塔"})
    server.close()
    assert server.raw  # rpc() 已逐行 json.loads；这里再确认每一行都是 JSON-RPC 消息
    for line in server.raw:
        assert json.loads(line)["jsonrpc"] == "2.0"


def test_ingest_enqueues_and_returns_empty(home, server, tmp_path):
    cwd = str(tmp_path / "proj")
    tp = tmp_path / "t.jsonl"
    H.make_transcript(tp, "sess-ing", cwd, turns=2)
    server.handshake()
    t0 = time.monotonic()
    assert server.call("ingest", {"session_id": "sess-ing", "transcript_path": str(tp), "cwd": cwd,
                                  "last": "收到"}) == ""
    assert time.monotonic() - t0 < 2  # 只入队，不等读文件

    def n_events():
        c = db.connect(readonly=True)
        try:
            return c.execute("SELECT count(*) FROM events WHERE session_id='sess-ing'").fetchone()[0]
        finally:
            c.close()

    assert H.wait_until(n_events, 15) >= 4
    server.close()


def test_ingest_ignores_unreplaced_placeholders(home, server):
    server.handshake()
    for args in ({"session_id": "${session_id}", "transcript_path": "${transcript_path}"},
                 {"session_id": "", "transcript_path": "x"}, {"session_id": "  "}):
        assert server.call("ingest", args) == ""
    time.sleep(1.5)
    c = db.connect(readonly=True)
    try:
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        c.close()
    server.close()


def test_stdin_close_exits_fast_and_releases_lease(home, server):
    server.handshake()
    pid = server.p.pid
    assert H.wait_until(lambda: H.lease_holder_pid(home) == pid, 15), "server 启动后应立刻持有租约"
    took = server.close()
    assert took < 5, f"关 stdin 后 {took:.1f} 秒才退出"
    assert server.p.returncode == 0
    assert H.lease_holder_pid(home) is None  # 租约已释放
    c = db.connect(readonly=True)
    try:
        assert c.execute("SELECT count(*) FROM kv WHERE key LIKE 'proc:%'").fetchone()[0] == 0
    finally:
        c.close()
    assert not H.pid_alive(pid)


def test_unknown_tool_is_error_not_crash(home, server):
    server.handshake()
    r = server.rpc("tools/call", {"name": "nope", "arguments": {}})
    assert r["result"].get("isError") or "error" in r
    assert server.call("usage")  # 还活着
    server.close()

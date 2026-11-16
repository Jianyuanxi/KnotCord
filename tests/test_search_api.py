"""检索 search / timeline / get 的测试。"""

import json
import sqlite3
import time

import pytest

from knotcord.core import db
from knotcord.core.index import index_doc
from knotcord.search import api

T0 = 1_762_700_000  # 2025-11 附近


def add_note(conn, title, body="", *, project="Span", ts=T0, type="discovery", state="active",
             session="s1", superseded_by=None, facts=None, source_events=None):
    cur = conn.execute(
        "INSERT INTO notes(project, session_id, ts, type, title, body, facts, files, tags, state, superseded_by, "
        "source_events) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (project, session, ts, type, title, body, json.dumps(facts or [], ensure_ascii=False),
         '["a.py"]', '["gotcha"]', state, superseded_by, source_events))
    nid = cur.lastrowid
    index_doc(conn, "note", nid, project, ts, f"{title}\n{body}", active=(state == "active"))
    return nid


def add_summary(conn, request, completed="", *, project="Span", ts=T0, session="s1"):
    cur = conn.execute("INSERT INTO summaries(session_id, project, ts, request, completed, learned, next_steps) "
                       "VALUES(?,?,?,?,?,?,?)", (session, project, ts, request, completed, "学到了", "下一步"))
    index_doc(conn, "summary", cur.lastrowid, project, ts, f"{request}\n{completed}")
    return cur.lastrowid


def add_event(conn, kind, body="", *, tool=None, brief=None, project="Span", ts=T0, session="s1",
              uuid=None, index=False, src_line=None):
    uuid = uuid or f"u-{kind}-{ts}-{time.time_ns()}"
    cur = conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, tool, brief, body, src_line) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (session, project, ts, uuid, kind, tool, brief, body, src_line))
    if index:
        index_doc(conn, "prompt", cur.lastrowid, project, ts, body)
    return cur.lastrowid


def test_search_prefix_formats(conn):
    n = add_note(conn, "宝塔面板反代配置", "nginx 反代", ts=T0)
    u = add_note(conn, "宝塔没做完的事", "宝塔面板待办", type="unfinished", ts=T0 + 1)
    s = add_summary(conn, "配置宝塔面板", "完成反代", ts=T0 + 2)
    p = add_event(conn, "prompt", "宝塔面板怎么装", ts=T0 + 3, index=True)
    conn.execute("INSERT INTO memfiles(path, project, mtime, name, description) VALUES('/x/a.md','Span',1,'bt','宝塔笔记')")
    mid = conn.execute("SELECT id FROM memfiles").fetchone()[0]
    index_doc(conn, "memfile", mid, "Span", 1, "bt 宝塔笔记 宝塔面板")
    out = api.search(conn, "宝塔面板")
    lines = out.splitlines()
    heads = {l.split()[0] for l in lines}
    assert heads == {f"n{n}", f"u{u}", f"s{s}", f"p{p}", f"m{mid}"}
    by = {l.split()[0]: l for l in lines}
    assert by[f"n{n}"].split()[2:4] == ["Span", "discovery"]
    assert by[f"n{n}"].endswith("宝塔面板反代配置")
    assert f"s{s}" in by and by[f"s{s}"].split()[3] == "summary"
    assert by[f"p{p}"].split()[3] == "prompt"
    assert by[f"m{mid}"] == f"m{mid} Span memfile bt：宝塔笔记"
    assert by[f"u{u}"].split()[3] == "unfinished"


def test_no_result(conn):
    assert api.search(conn, "不存在的词语") == "无结果"
    assert api.search(conn, "") == "无结果"


def test_superseded_hidden_by_default(conn):
    new = add_note(conn, "路由配置新版", "x", ts=T0 + 10)
    old = add_note(conn, "路由配置旧版", "x", ts=T0, state="superseded", superseded_by=new)
    assert f"n{old}" not in api.search(conn, "路由配置")
    out = api.search(conn, "路由配置", include_superseded=True)
    assert f"n{old} " in out and f"[被 n{new} 取代]" in out
    assert f"n{new} " in out


def test_project_boost_and_filter(conn):
    a = add_note(conn, "断流保护", "说明", project="A", ts=T0 + 5)
    b = add_note(conn, "断流保护", "说明", project="B", ts=T0 + 1)
    # 同样的文本：不加分时较新的 A 在前；给 B 加分后 B 在前
    first = api.search(conn, "断流保护").splitlines()[0]
    assert first.startswith(f"n{a} ") or first.startswith(f"n{b} ")
    out = api.search(conn, "断流保护", current_project="B").splitlines()
    assert out[0].startswith(f"n{b} ")
    assert len(out) == 2  # 只加分，不过滤
    only = api.search(conn, "断流保护", project="A").splitlines()
    assert len(only) == 1 and only[0].startswith(f"n{a} ")


def test_since_until(conn):
    old = add_note(conn, "Xray 配置", "x", ts=T0)
    new = add_note(conn, "Xray 配置", "x", ts=T0 + 1000)
    assert api.search(conn, "Xray", since=T0 + 500).splitlines()[0].startswith(f"n{new} ")
    assert len(api.search(conn, "Xray", since=T0 + 500).splitlines()) == 1
    assert api.search(conn, "Xray", until=T0 + 500).startswith(f"n{old} ")
    assert api.search(conn, "Xray", since=T0 + 5000) == "无结果"


def test_limit(conn):
    for i in range(6):
        add_note(conn, f"Xray 条目 {i}", "x", ts=T0 + i)
    assert len(api.search(conn, "Xray", limit=3).splitlines()) == 3


def test_single_han_like_fallback(conn):
    n = add_note(conn, "表格处理", "x")
    s = add_summary(conn, "做一张表", "")
    p = add_event(conn, "prompt", "这个表怎么办", index=False)
    other = add_note(conn, "无关", "没有那个字")
    out = api.search(conn, "表")
    heads = {l.split()[0] for l in out.splitlines()}
    assert heads == {f"n{n}", f"s{s}", f"p{p}"}
    assert f"n{other}" not in heads


def test_use_count_boosts_ranking(conn):
    a = add_note(conn, "路由规则", "x", ts=T0 + 10)
    b = add_note(conn, "路由规则", "x", ts=T0)
    api.get(conn, [f"n{b}"])
    assert api.search(conn, "路由规则").splitlines()[0].startswith(f"n{b} ")
    assert conn.execute("SELECT use_count FROM notes WHERE id=?", (a,)).fetchone()[0] == 0


def test_vector_hook_rrf(conn, monkeypatch):
    a = add_note(conn, "完全无关的标题", "x", ts=T0 - 100)
    b = add_note(conn, "路由规则", "y", ts=T0)
    doc_a = conn.execute("SELECT id FROM docs WHERE src='note' AND src_id=?", (a,)).fetchone()[0]
    monkeypatch.setattr(api, "_vector_candidates", lambda *aa, **kw: [(doc_a, 0.9)])
    out = api.search(conn, "路由规则").splitlines()
    assert {l.split()[0] for l in out} == {f"n{a}", f"n{b}"}
    assert out[0].startswith(f"n{b} ")  # 两路各一个第一名，同分，新的在前
    assert len(out) == 2


# ---------------------------------------------------------------- timeline

def test_timeline_window(conn):
    p1 = add_event(conn, "prompt", "第一个问题", ts=T0)
    t1 = add_event(conn, "tool", brief="Bash: ls", tool="Bash", ts=T0 + 1)
    t2 = add_event(conn, "tool", brief="Edit a.py", tool="Edit", ts=T0 + 2)
    n = add_note(conn, "关键发现", "x", ts=T0 + 3)
    t3 = add_event(conn, "tool", brief="Bash: pytest", tool="Bash", ts=T0 + 4)
    p2 = add_event(conn, "prompt", "第二个问题", ts=T0 + 5)
    add_event(conn, "prompt", "别的会话", ts=T0 + 3, session="other")
    out = api.timeline(conn, f"n{n}", before=2, after=2).splitlines()
    assert len(out) == 5
    assert out[2].startswith(f"> n{n} ")
    assert out[1].startswith(f"e{t2} ") and out[3].startswith(f"e{t3} ")
    assert out[0].startswith(f"e{t1} ") and "tool Bash: Bash: ls" in out[0]
    assert out[-1].startswith(f"p{p2} ")
    assert all("别的会话" not in l for l in out)
    # 从 prompt 出发
    out2 = api.timeline(conn, f"p{p1}", before=3, after=1).splitlines()
    assert out2[0].startswith(f"> p{p1} ") and len(out2) == 2


def test_timeline_edge_cases(conn):
    s = add_summary(conn, "做某事", ts=T0)
    assert api.timeline(conn, f"s{s}").startswith(f"> s{s} ")
    assert "找不到" in api.timeline(conn, "n999")
    assert "无效" in api.timeline(conn, "zzz")
    assert "没有时间线" in api.timeline(conn, "m1")


def test_timeline_same_second_ties(conn):
    ids = [add_event(conn, "tool", brief=f"op{i}", tool="Bash", ts=T0) for i in range(20)]
    out = api.timeline(conn, f"e{ids[10]}", before=2, after=2).splitlines()
    assert out[2].startswith(f"> e{ids[10]} ")
    assert out[0].startswith(f"e{ids[8]} ") and out[-1].startswith(f"e{ids[12]} ")


# ---------------------------------------------------------------- get

def test_get_note_full_and_counts(conn):
    old = add_note(conn, "旧", "b", state="superseded", superseded_by=2)
    n = add_note(conn, "标题", "正文内容", facts=["事实一", "事实二"], ts=T0)
    out = api.get(conn, [f"n{n}", f"n{old}"])
    assert "title: 标题" in out and "正文内容" in out and "- 事实一" in out and "files: a.py" in out
    assert "tags: gotcha" in out and "state: active" in out
    assert "被 n2 取代" in out
    row = conn.execute("SELECT use_count, last_used FROM notes WHERE id=?", (n,)).fetchone()
    assert row[0] == 1 and row[1] > 0
    assert conn.execute("SELECT use_count FROM docs WHERE src='note' AND src_id=?", (n,)).fetchone()[0] == 1
    api.get(conn, [f"n{n}"])
    assert conn.execute("SELECT use_count FROM notes WHERE id=?", (n,)).fetchone()[0] == 2


def test_get_u_and_n_both_accepted(conn):
    u = add_note(conn, "没做完", "x", type="unfinished")
    assert f"u{u} unfinished" in api.get(conn, [f"n{u}"])
    assert f"u{u} unfinished" in api.get(conn, [f"u{u}"])


def test_get_summary_prompt_event_memfile(conn):
    s = add_summary(conn, "请求内容", "做完了")
    p = add_event(conn, "prompt", "用户原话在这里", index=True)
    e = add_event(conn, "tool", "工具原文", tool="Bash", brief="Bash: ls")
    conn.execute("INSERT INTO memfiles(path, project, mtime, name, description) VALUES('/x/a.md','Span',1,'n','d')")
    out = api.get(conn, [f"s{s}", f"p{p}", f"e{e}", "m1", "n9999", "bad"])
    assert "request: 请求内容" in out and "completed: 做完了" in out and "learned: 学到了" in out
    assert "用户原话在这里" in out and "工具原文" in out
    assert "path: /x/a.md" in out and "正文请直接 Read 这个文件" in out
    assert "n9999: 找不到" in out and "bad: 无效编号" in out
    assert conn.execute("SELECT use_count FROM docs WHERE src='summary' AND src_id=?", (s,)).fetchone()[0] == 1


def test_get_with_source_transcript_missing_and_present(conn, tmp_path):
    e1 = add_event(conn, "tool", brief="Bash: ls", tool="Bash", src_line=12)
    e2 = add_event(conn, "prompt", "问题", src_line=3)
    n = add_note(conn, "有来源", "x", source_events=json.dumps([e1, e2]))
    tp = tmp_path / "t.jsonl"
    conn.execute("INSERT INTO sessions(id, transcript_path) VALUES('s1', ?)", (str(tp),))
    out = api.get(conn, [f"n{n}"], with_source=True)
    assert f"e{e1} tool Bash Bash: ls" in out and "原文已不在" in out
    tp.write_text("x", encoding="utf-8")
    out = api.get(conn, [f"n{n}"], with_source=True)
    assert f"transcript: {tp} 行 3,12" in out
    # 没有 sessions 记录也不报错
    n2 = add_note(conn, "无会话", "x", session="nosuch")
    assert "无记录" in api.get(conn, [f"n{n2}"], with_source=True)


def test_get_readonly_connection_does_not_fail(conn, trace_home):
    n = add_note(conn, "只读测试", "x")
    ro = db.connect(readonly=True)
    try:
        out = api.get(ro, [f"n{n}"])
        assert "title: 只读测试" in out
        assert "无结果" != api.search(ro, "只读测试")
    finally:
        ro.close()
    assert conn.execute("SELECT use_count FROM notes WHERE id=?", (n,)).fetchone()[0] == 0
